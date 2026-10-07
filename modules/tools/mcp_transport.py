"""Pinned, bounded outbound MCP HTTP transport built on public httpx2 APIs."""

import asyncio
import ipaddress
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from types import TracebackType
from typing import Any, cast

from httpx2 import (
    URL,
    AsyncBaseTransport,
    AsyncByteStream,
    AsyncClient,
    AsyncHTTPTransport,
    Limits,
    Request,
    Response,
    Timeout,
)

from core.mcp_endpoint import normalize_mcp_url


class McpTransportError(RuntimeError):
    """Describe a bounded transport rejection without exposing request credentials or body data."""


class McpOperationNetworkBudget:
    """Share one deadline and finite byte/request quotas across an SDK operation's HTTP streams."""

    def __init__(
        self,
        *,
        deadline: float,
        max_requests: int,
        max_request_bytes: int,
        max_response_bytes: int,
        max_inflight_requests: int = 4,
        max_teardown_requests: int = 1,
        max_teardown_bytes: int = 8_192,
        before_request: Callable[[], Awaitable[bool]] | None,
    ) -> None:
        """Create an operation budget whose deadline includes caller-side permit queueing.

        Args:
            deadline: Absolute `time.monotonic()` deadline supplied before operation admission.
            max_requests: Cumulative ordinary requests, including redirects and reconnects.
            max_request_bytes: Cumulative ordinary request body bytes including SDK framing.
            max_response_bytes: Cumulative ordinary response body bytes across JSON and SSE.
            max_inflight_requests: Bound on simultaneously open HTTP request streams.
            max_teardown_requests: Separate small allowance for SDK session termination.
            max_teardown_bytes: Separate body allowance for SDK session termination.
            before_request: Server-owned durable fence check called immediately before each socket send.
        Raises:
            ValueError: If a configured operation bound is not positive.
        """
        if min(deadline, max_requests, max_request_bytes, max_response_bytes,
               max_inflight_requests, max_teardown_requests, max_teardown_bytes) <= 0:
            raise ValueError("MCP network limits and deadline must be positive")
        self.deadline = deadline
        self.max_requests = max_requests
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes
        self.max_inflight_requests = max_inflight_requests
        self.max_teardown_requests = max_teardown_requests
        self.max_teardown_bytes = max_teardown_bytes
        self.before_request = before_request
        self._request_count = 0
        self._request_bytes = 0
        self._response_bytes = 0
        self._teardown_requests = 0
        self._teardown_bytes = 0
        self._inflight_requests = 0
        self._lock = asyncio.Lock()

    def remaining_seconds(self) -> float:
        """Return positive time remaining or fail closed after the shared operation deadline."""
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("MCP operation deadline expired")
        return remaining

    async def claim_request(self, *, teardown: bool = False) -> None:
        """Reserve one bounded request slot, counting redirects and reconnects as real requests."""
        self.remaining_seconds()
        async with self._lock:
            if self._inflight_requests >= self.max_inflight_requests:
                raise McpTransportError("MCP operation has too many open HTTP streams")
            if teardown:
                if self._teardown_requests >= self.max_teardown_requests:
                    raise McpTransportError("MCP session teardown request limit exceeded")
                self._teardown_requests += 1
            else:
                if self._request_count >= self.max_requests:
                    raise McpTransportError("MCP operation request limit exceeded")
                self._request_count += 1
            self._inflight_requests += 1

    async def claim_request_bytes(self, amount: int, *, teardown: bool = False) -> None:
        """Count outgoing stream bytes before yielding them to the network delegate."""
        if amount < 0:
            raise McpTransportError("MCP request stream reported an invalid byte count")
        self.remaining_seconds()
        async with self._lock:
            if teardown:
                if self._teardown_bytes + amount > self.max_teardown_bytes:
                    raise McpTransportError("MCP session teardown byte limit exceeded")
                self._teardown_bytes += amount
            else:
                if self._request_bytes + amount > self.max_request_bytes:
                    raise McpTransportError("MCP operation request byte limit exceeded")
                self._request_bytes += amount

    async def claim_response_bytes(self, amount: int, *, teardown: bool = False) -> None:
        """Count incoming stream bytes before SDK JSON or SSE code can buffer or parse them."""
        if amount < 0:
            raise McpTransportError("MCP response stream reported an invalid byte count")
        self.remaining_seconds()
        async with self._lock:
            if teardown:
                if self._teardown_bytes + amount > self.max_teardown_bytes:
                    raise McpTransportError("MCP session teardown byte limit exceeded")
                self._teardown_bytes += amount
            else:
                if self._response_bytes + amount > self.max_response_bytes:
                    raise McpTransportError("MCP operation response byte limit exceeded")
                self._response_bytes += amount

    async def finish_request(self) -> None:
        """Release one live stream slot after its response body closes or a send fails."""
        async with self._lock:
            if self._inflight_requests > 0:
                self._inflight_requests -= 1


class _BoundedRequestByteStream(AsyncByteStream):
    """Count each outbound body chunk before the pinned transport delegates it."""

    def __init__(self, stream: AsyncByteStream, budget: McpOperationNetworkBudget, *, teardown: bool) -> None:
        """Wrap the SDK-owned request stream without materializing its full body."""
        self._stream = stream
        self._budget = budget
        self._teardown = teardown
        self._closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield only chunks that fit the operation's request-body allowance."""
        try:
            async for chunk in self._stream:
                await self._budget.claim_request_bytes(len(chunk), teardown=self._teardown)
                yield chunk
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        """Close the SDK request stream once after EOF, cancellation, or quota rejection."""
        if not self._closed:
            self._closed = True
            await self._stream.aclose()


class _BoundedResponseByteStream(AsyncByteStream):
    """Count response bytes before SDK JSON/SSE consumers see any chunk."""

    def __init__(self, stream: AsyncByteStream, budget: McpOperationNetworkBudget, *, teardown: bool) -> None:
        """Wrap the delegate response and own the operation's open-stream slot."""
        self._stream = stream
        self._budget = budget
        self._teardown = teardown
        self._closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield only response chunks that fit the shared operation byte allowance."""
        try:
            async for chunk in self._stream:
                await self._budget.claim_response_bytes(len(chunk), teardown=self._teardown)
                yield chunk
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        """Close the delegate and release its active request slot exactly once."""
        if not self._closed:
            self._closed = True
            try:
                await self._stream.aclose()
            finally:
                await self._budget.finish_request()


class McpPinnedHttpTransport(AsyncBaseTransport):
    """Enforce a configured MCP HTTP origin's resolved destination at the socket boundary."""

    def __init__(
        self,
        endpoint: str,
        budget: McpOperationNetworkBudget,
        *,
        approved_destination_cidrs: Mapping[tuple[str, str, int], tuple[str, ...]] | None = None,
    ) -> None:
        """Create one-origin transport with an optional server-composed exact-origin CIDR exception.

        Args:
            endpoint: Credential-free configured HTTP or HTTPS MCP endpoint.
            budget: Shared operation request, response, deadline and revalidation state.
            approved_destination_cidrs: Deployment-owned exceptions keyed by normalized
                `(scheme, host, effective_port)`; browser or connection-draft input must never supply these.
        Raises:
            ValueError: If the configured endpoint is not a valid HTTP(S) URL.
        """
        self._endpoint_url = URL(endpoint)
        self._budget = budget
        self._approved_destination_cidrs = approved_destination_cidrs or {}
        self._delegate = AsyncHTTPTransport(
            verify=True,
            trust_env=False,
            proxy=None,
            retries=0,
            http1=True,
            http2=False,
            limits=Limits(max_connections=4, max_keepalive_connections=4),
        )
        self._configured_origin = self._validate_origin(self._endpoint_url)

    def _validate_origin(self, url: URL) -> tuple[str, str, int]:
        """Normalize an HTTP(S) URL origin through the shared parser and reject unsafe URL components."""
        try:
            scheme, host, port, _path = normalize_mcp_url(str(url))
        except ValueError as exc:
            raise McpTransportError("MCP request URL is outside the configured HTTP policy") from exc
        return scheme, host, port

    async def _resolve_destination(self, origin: tuple[str, str, int]) -> str:
        """Resolve all answers by the shared deadline; HTTP needs exact private/loopback CIDR approval."""
        scheme, host, port = origin
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is None:
            try:
                async with asyncio.timeout(self._budget.remaining_seconds()):
                    rows = await asyncio.get_running_loop().getaddrinfo(
                        host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM,
                        proto=socket.IPPROTO_TCP,
                    )
            except OSError as exc:
                raise McpTransportError("MCP destination resolution failed") from exc
            addresses = []
            for family, _kind, _protocol, _canonical_name, sockaddr in rows:
                if family not in {socket.AF_INET, socket.AF_INET6}:
                    raise McpTransportError("MCP destination resolver returned an unsupported address family")
                addresses.append(sockaddr[0])
        else:
            addresses = [host]
        if not addresses:
            raise McpTransportError("MCP destination did not resolve")

        exception_cidrs = self._approved_destination_cidrs.get((scheme, host, port), ())
        try:
            networks = tuple(ipaddress.ip_network(value, strict=False) for value in exception_cidrs)
        except ValueError as exc:
            raise McpTransportError("Deployment MCP destination exception is invalid") from exc
        normalized: list[str] = []
        for address_text in addresses:
            try:
                address = ipaddress.ip_address(address_text)
            except ValueError as exc:
                raise McpTransportError("MCP destination resolver returned an invalid address") from exc
            if isinstance(address, ipaddress.IPv6Address) and address.scope_id is not None:
                raise McpTransportError("Scoped IP destinations are not supported")
            if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
                address = address.ipv4_mapped
            exception_allowed = any(
                address.version == network.version and address in network for network in networks
            )
            # IPv6 loopback overlaps ipaddress's reserved classification; allow only its exact HTTP CIDR grant.
            approved_http_loopback = scheme == "http" and address.is_loopback and exception_allowed
            if (address.is_link_local or address.is_multicast or address.is_unspecified
                    or address.is_reserved and not approved_http_loopback
                    or address.is_loopback and scheme != "http"):
                raise McpTransportError("MCP destination resolved to a special-use address")
            if scheme == "http" and (not exception_allowed or not (address.is_private or address.is_loopback)):
                raise McpTransportError("Plain HTTP MCP destinations require an approved private or loopback CIDR")
            if not address.is_global and not exception_allowed:
                raise McpTransportError("MCP destination resolved to a non-public address")
            normalized.append(address.compressed)
        return normalized[0]

    async def handle_async_request(self, request: Request) -> Response:
        """Reject origin changes and plaintext credentials, then pin to a checked IP while preserving TLS identity."""
        teardown = request.method == "DELETE"
        await self._budget.claim_request(teardown=teardown)
        delegated_response = False
        request_stream = _BoundedRequestByteStream(cast(AsyncByteStream, request.stream), self._budget, teardown=teardown)
        try:
            origin = self._validate_origin(request.url)
            if origin != self._configured_origin:
                raise McpTransportError("MCP redirect changed the configured origin")
            if origin[0] == "http" and any(
                request.headers.get(name) for name in ("authorization", "cookie", "proxy-authorization")
            ):
                raise McpTransportError("Plain HTTP MCP requests cannot carry credentials")
            pinned_host = await self._resolve_destination(origin)
            remaining = self._budget.remaining_seconds()
            if self._budget.before_request is None or not await self._budget.before_request():
                raise McpTransportError("MCP durable execution fence is no longer current")
            self._budget.remaining_seconds()

            headers: list[tuple[str, str | bytes]] = [
                (name, value) for name, value in request.headers.multi_items()
                if name.lower() not in {"host", "accept-encoding"}
            ]
            headers.extend((("host", request.url.netloc), ("accept-encoding", "identity")))
            extensions = dict(request.extensions)
            timeout = dict(extensions.get("timeout", {}) or {})
            for phase in ("connect", "read", "write", "pool"):
                value = timeout.get(phase)
                timeout[phase] = min(value, remaining) if isinstance(value, (int, float)) and value > 0 else remaining
            extensions["timeout"] = timeout
            extensions["sni_hostname"] = origin[1]
            pinned_request = Request(
                request.method,
                request.url.copy_with(host=pinned_host),
                headers=cast(Any, headers),  # mixed str/bytes items are normalised per header by httpx
                stream=request_stream,
                extensions=extensions,
            )
            response = await self._delegate.handle_async_request(pinned_request)
            if response.status_code in {401, 403}:
                await response.aclose()
                raise PermissionError("MCP endpoint rejected its configured authentication")
            content_encoding = response.headers.get("content-encoding", "identity").strip().lower()
            if content_encoding not in {"", "identity"}:
                await response.aclose()
                raise McpTransportError("Compressed MCP responses are not supported by the bounded reader")
            content_length = response.headers.get("content-length")
            if content_length is not None:
                try:
                    if int(content_length) > (self._budget.max_teardown_bytes if teardown else self._budget.max_response_bytes):
                        await response.aclose()
                        raise McpTransportError("MCP response exceeds the configured operation byte limit")
                except ValueError:
                    # Invalid lengths are still bounded by the response stream wrapper.
                    pass
            bounded_response = Response(
                status_code=response.status_code,
                headers=response.headers,
                stream=_BoundedResponseByteStream(cast(AsyncByteStream, response.stream), self._budget, teardown=teardown),
                extensions=response.extensions,
            )
            delegated_response = True
            return bounded_response
        finally:
            if not delegated_response:
                try:
                    await request_stream.aclose()
                finally:
                    await self._budget.finish_request()

    async def aclose(self) -> None:
        """Close the owned direct HTTP transport and its bounded connection pool."""
        await self._delegate.aclose()

    async def __aenter__(self) -> "McpPinnedHttpTransport":  # noqa: PYI034  # async context-manager signature kept; typing-only
        """Enter the delegated HTTP transport so httpx2 clients can manage it as a context."""
        await self._delegate.__aenter__()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None = None, exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        """Close the delegated connection pool when the caller-owned client exits."""
        await self.aclose()


def create_mcp_http_client(
    endpoint: str,
    bearer_token: str | None,
    budget: McpOperationNetworkBudget,
    *,
    timeout_seconds: float,
    approved_destination_cidrs: Mapping[tuple[str, str, int], tuple[str, ...]] | None = None,
) -> AsyncClient:
    """Build a caller-owned client that pins one MCP HTTP(S) origin under deployment egress policy.

    Public HTTPS remains eligible by default; private HTTPS and private or loopback HTTP
    require exact-origin deployment CIDRs. HTTP requests carrying authorization, cookie,
    or proxy-authorization headers are rejected before the socket delegate. Redirects and
    environment proxies are disabled. Bearer authentication is usable only with HTTPS.
    The caller closes the SDK client and transport context before closing this HTTP client.
    """
    headers = {"Accept-Encoding": "identity"}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    transport = McpPinnedHttpTransport(
        endpoint, budget, approved_destination_cidrs=approved_destination_cidrs,
    )
    timeout = max(0.1, min(timeout_seconds, budget.remaining_seconds()))
    return AsyncClient(
        transport=transport,
        trust_env=False,
        follow_redirects=False,
        max_redirects=3,
        timeout=Timeout(connect=timeout, read=timeout, write=timeout, pool=timeout),
        headers=headers,
    )
