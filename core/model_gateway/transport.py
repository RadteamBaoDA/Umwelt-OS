from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextvars import ContextVar

import httpx

logger = logging.getLogger(__name__)

# Set by the gateway around one SDK call only when the caller passed ``after_send``; read by
# ApprovedEndpointTransport in the caller's task (httpx/httpcore never hop tasks).
body_sent: ContextVar[Callable[[], Awaitable[None]] | None] = ContextVar("body_sent", default=None)


class EndpointNetworkPolicyError(RuntimeError):
    """A gateway destination could not be approved by deployment policy."""


def _networks(values: Sequence[str]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse configured CIDRs and fail closed for malformed or empty deployment allowlists."""
    try:
        networks = tuple(ipaddress.ip_network(value, strict=False) for value in values)
    except ValueError as exc:
        raise EndpointNetworkPolicyError("Gateway network policy is invalid") from exc
    if not networks:
        raise EndpointNetworkPolicyError("Gateway network policy is unavailable")
    return networks


def _address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Parse an IP result, rejecting scoped IPv6 and normalizing IPv4-mapped IPv6 addresses."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise EndpointNetworkPolicyError("Gateway address resolution failed") from exc
    if isinstance(address, ipaddress.IPv6Address):
        if address.scope_id is not None:
            raise EndpointNetworkPolicyError("Scoped IPv6 gateway addresses are not allowed")
        if address.ipv4_mapped is not None:
            return address.ipv4_mapped
    return address


class _NotifyOnEnd(httpx.AsyncByteStream):
    """Yield the request body unchanged, then await ``callback`` once when it is exhausted.

    httpcore's HTTP/1.1 path awaits ``network_stream.write`` for each chunk before pulling the next,
    so exhaustion means the last body byte was handed to the transport (``transport.write``) and
    response headers have not been read yet. Only valid for HTTP/1.1 (``approved_http_client``).
    """

    def __init__(self, inner: httpx.AsyncByteStream, callback: Callable[[], Awaitable[None]]) -> None:
        self._inner = inner
        self._callback: Callable[[], Awaitable[None]] | None = callback

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            yield chunk
        callback, self._callback = self._callback, None
        if callback is not None:
            try:
                await callback()
            except Exception as exc:  # noqa: BLE001  # P2-2: must never raise into httpcore
                # Never raise into httpcore: the SDK would map it to APIConnectionError and retry,
                # re-sending the body. The caller's idempotent ``after_send`` fallback runs again; lock release
                # relies on the session's close/connection invalidation, not on that retry.
                # Type only: messages can carry SQL parameters or prompt text.
                logger.warning("Model request body-sent callback failed (%s)", type(exc).__name__)

    async def aclose(self) -> None:
        await self._inner.aclose()


class ApprovedEndpointTransport(httpx.AsyncBaseTransport):
    """Pin one approved HTTP origin to checked IPs without losing Host or TLS SNI."""

    def __init__(
        self,
        origin: httpx.URL,
        approved_cidrs: Sequence[str],
        delegate: httpx.AsyncBaseTransport,
    ) -> None:
        """Retain the HTTP origin, parsed non-empty CIDR allowlist, and delegate; malformed or empty CIDRs raise EndpointNetworkPolicyError."""
        self._scheme = origin.scheme
        self._host = origin.raw_host.decode("ascii").lower()
        self._port = origin.port
        self._networks = _networks(approved_cidrs)
        self._delegate = delegate

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Validate the request origin, resolve every address, require all addresses to be allowlisted, and delegate to the selected pinned IP."""
        request_host = request.url.raw_host.decode("ascii").lower()
        if (request.url.scheme, request_host, request.url.port) != (self._scheme, self._host, self._port):
            raise EndpointNetworkPolicyError("Gateway request origin changed")

        if _is_ip(request_host):
            addresses = [_address(request_host)]
        else:
            try:
                answers = await asyncio.get_running_loop().getaddrinfo(
                    request_host,
                    self._port,
                    family=socket.AF_UNSPEC,
                    type=socket.SOCK_STREAM,
                    proto=socket.IPPROTO_TCP,
                )
            except OSError as exc:
                raise EndpointNetworkPolicyError("Gateway address resolution failed") from exc
            addresses = []
            for answer in answers:
                sockaddr = answer[4]
                if not sockaddr or (answer[0] == socket.AF_INET6 and len(sockaddr) > 3 and sockaddr[3]):
                    raise EndpointNetworkPolicyError("Scoped IPv6 gateway addresses are not allowed")
                addresses.append(_address(sockaddr[0]))

        unique: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        for address in addresses:
            if address not in unique:
                unique.append(address)
        # Deny the whole DNS answer set so a mixed safe/unsafe response cannot route to a forbidden address.
        if not unique or any(not any(address in network for network in self._networks) for address in unique):
            raise EndpointNetworkPolicyError("Gateway address is denied by deployment policy")

        selected = unique[0]
        headers = request.headers.copy()
        # Pin the network connection to the checked IP while preserving the original HTTP Host and TLS identity.
        headers["Host"] = request.url.netloc.decode("ascii")
        extensions = dict(request.extensions)
        if self._scheme == "https":
            extensions["sni_hostname"] = self._host
        else:
            extensions.pop("sni_hostname", None)
        callback = body_sent.get()
        stream = request.stream
        if callback is not None and isinstance(stream, httpx.AsyncByteStream):
            stream = _NotifyOnEnd(stream, callback)
        pinned_request = httpx.Request(
            method=request.method,
            url=request.url.copy_with(host=str(selected)),
            headers=headers,
            stream=stream,
            extensions=extensions,
        )
        return await self._delegate.handle_async_request(pinned_request)

    async def aclose(self) -> None:
        """Close the wrapped HTTP transport."""
        await self._delegate.aclose()


def _is_ip(host: str) -> bool:
    """Return whether a hostname is a literal IP address."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def approved_http_client(base_url: str, approved_cidrs: Sequence[str]) -> httpx.AsyncClient:
    """Create a no-proxy, no-redirect HTTP client using the approved-address transport."""
    origin = httpx.URL(base_url)
    # HTTP/1.1 only: _NotifyOnEnd relies on httpcore's HTTP/1.1 write-then-pull body loop.
    delegate = httpx.AsyncHTTPTransport(
        verify=True,
        trust_env=False,
        proxy=None,
        retries=0,
        http1=True,
        http2=False,
    )
    transport = ApprovedEndpointTransport(origin, approved_cidrs, delegate)
    return httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False)
