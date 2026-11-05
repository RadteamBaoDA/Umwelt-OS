"""Bounded bearer and response fencing for the inbound MCP transport."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from core.tools.schemas import ToolExecutionPrincipal
from modules.tools.mcp_admission import McpAdmission, McpInboundLease
from modules.tools.mcp_schemas import InboundBinding, InboundPrincipal

_CLEANUP_RESERVE_SECONDS = 2.0


@dataclass
class InboundResponseFence:
    """Hold server-created evidence for one response's final privacy check.

    A successful native call must set both the exact reviewed binding and the
    same sink populated by native owner contracts. Client input never controls
    either field.
    """

    binding: InboundBinding | None = None
    sink: object | None = None
    has_successful_native_result: bool = False
    catalog_bindings: tuple[InboundBinding, ...] = ()


@dataclass
class InboundRequestState:
    """Carry detached identity, expected scope, lease, and response fences per request."""

    identity: InboundPrincipal
    expected: ToolExecutionPrincipal
    lease: McpInboundLease
    response_fence: InboundResponseFence = field(default_factory=InboundResponseFence)
    native_task: asyncio.Task[Any] | None = None


class InboundReplayReceiver:
    """Replay one already-bounded request body while preserving disconnect observation."""

    def __init__(self, body: bytes, source_receive: Receive) -> None:
        """Keep the bounded body and original receive channel for later disconnects."""
        self._body = body
        self._source_receive = source_receive
        self._sent_body = False
        self._disconnect = asyncio.Event()

    async def __call__(self) -> Message:
        """Return the body once, then observe the original ASGI disconnect channel."""
        if not self._sent_body:
            self._sent_body = True
            return {"type": "http.request", "body": self._body, "more_body": False}
        await self._disconnect.wait()
        return {"type": "http.disconnect"}

    async def wait_for_disconnect(self) -> None:
        """Own the original post-body receive channel and signal its first client disconnect."""
        while True:
            message = await self._source_receive()
            if message.get("type") == "http.disconnect":
                self._disconnect.set()
                return


class InboundResponseBuffer:
    """Capture a single finite JSON response before any response bytes are sent."""

    def __init__(self, *, max_body_bytes: int, deadline: float) -> None:
        """Set response byte and monotonic-time limits for one transport request."""
        self._max_body_bytes = max_body_bytes
        self._deadline = deadline
        self._start: Message | None = None
        self._body = bytearray()
        self._complete = False

    async def __call__(self, message: Message) -> None:
        """Capture one start and bounded body; reject streaming or malformed ASGI output."""
        if asyncio.get_running_loop().time() > self._deadline:
            raise TimeoutError
        if message.get("type") == "http.response.start":
            if self._start is not None or self._complete:
                raise ValueError("Unexpected response start")
            content_types = [
                value
                for name, value in message.get("headers", [])
                if name.lower() == b"content-type"
            ]
            if (
                len(content_types) != 1
                or content_types[0].split(b";", 1)[0].strip().lower() != b"application/json"
            ):
                raise ValueError("Only JSON responses are supported")
            self._start = dict(message)
            return
        if message.get("type") != "http.response.body" or self._start is None or self._complete:
            raise ValueError("Unsupported response message")
        chunk = message.get("body", b"")
        if not isinstance(chunk, bytes) or len(self._body) + len(chunk) > self._max_body_bytes:
            raise ValueError("Response exceeds its byte limit")
        self._body.extend(chunk)
        if message.get("more_body", False):
            raise ValueError("Streaming responses are unavailable")
        self._complete = True

    def seal(self) -> tuple[Message, bytes]:
        """Return the complete captured response only after one finite body arrived."""
        if self._start is None or not self._complete:
            raise ValueError("Response is incomplete")
        return self._start, bytes(self._body)


@dataclass(frozen=True)
class _Reject:
    """A fixed, detail-free HTTP rejection decided before any owner callback runs."""

    status: int
    code: str
    bearer_challenge: bool = False


class InboundMcpGuard:
    """Authenticate and bound one canonical, stateless MCP JSON HTTP request."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        audience: str,
        expected_host: str,
        allowed_origins: frozenset[str],
        admission: McpAdmission,
        authenticate_inbound: Callable[[str], Awaitable[InboundPrincipal]],
        authorize_inbound: Callable[[InboundPrincipal], Awaitable[ToolExecutionPrincipal | None]],
        revalidate_inbound: Callable[[InboundPrincipal, ToolExecutionPrincipal], Awaitable[bool]],
        revalidate_inbound_output: Callable[..., Awaitable[bool]],
    ) -> None:
        """Bind the SDK child to exact deployment addressing, lease admission, and owner callbacks."""
        parsed = urlsplit(audience)
        if (
            not parsed.scheme
            or not parsed.netloc
            or parsed.path != "/api/v1/mcp/"
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Invalid inbound MCP audience")
        if (
            not expected_host
            or len(expected_host) > 512
            or parsed.netloc.lower() != expected_host.lower()
            or not allowed_origins
        ):
            raise ValueError("Invalid inbound MCP transport policy")
        self._app = app
        self._audience = audience
        self._canonical_path = parsed.path
        self._expected_host = expected_host.encode("ascii").lower()
        self._allowed_origins = frozenset(origin.encode("ascii") for origin in allowed_origins)
        self._admission = admission
        self._authenticate_inbound = authenticate_inbound
        self._authorize_inbound = authorize_inbound
        self._revalidate_inbound = revalidate_inbound
        self._revalidate_inbound_output = revalidate_inbound_output

    def _precheck_request(self, scope: Scope, headers: list[tuple[bytes, bytes]]) -> _Reject | None:
        """Reject oversize headers, non-canonical paths and wrong Host/Origin/Authorization counts.

        Starlette Mount keeps the full external path in ``path``; ``root_path`` is routing context.
        This endpoint deliberately does not accept deployment app_root_path prefixes. Pure; no I/O.
        """
        header_size = sum(len(name) + len(value) + 2 for name, value in headers)
        if len(headers) > 64 or header_size > 16_384:
            return _Reject(431, "invalid_headers")
        path = scope.get("path", "")
        if path != self._canonical_path or scope.get("query_string", b""):
            return _Reject(404, "not_found")
        hosts = [value for name, value in headers if name.lower() == b"host"]
        origins = [value for name, value in headers if name.lower() == b"origin"]
        auth_headers = [value for name, value in headers if name.lower() == b"authorization"]
        if (
            len(hosts) != 1
            or hosts[0].lower() != self._expected_host
            or len(origins) > 1
            or (origins and origins[0] not in self._allowed_origins)
            or len(auth_headers) != 1
        ):
            return _Reject(403, "request_rejected")
        return None

    @staticmethod
    def _bearer_token(headers: list[tuple[bytes, bytes]]) -> str | _Reject:
        """Extract the single ASCII bearer token (at most 512 chars, no whitespace) or reject as 401."""
        auth = next(value for name, value in headers if name.lower() == b"authorization")
        unauthorized = _Reject(401, "unauthorized", bearer_challenge=True)
        if not auth.startswith(b"Bearer ") or len(auth) > 519:
            return unauthorized
        try:
            raw_token = auth[7:].decode("ascii")
        except UnicodeDecodeError:
            return unauthorized
        if not raw_token or len(raw_token) > 512 or any(char.isspace() for char in raw_token):
            return unauthorized
        return raw_token

    @staticmethod
    def _check_framing(headers: list[tuple[bytes, bytes]], method: str) -> _Reject | int | None:
        """Validate method and body framing; return a rejection or the declared length (or ``None``).

        Only POST with identity encoding, exactly one JSON content type and at most one valid,
        non-negative ``Content-Length`` up to 128,000 bytes is accepted. Pure; no I/O.
        """
        if method in {"GET", "DELETE"} or method != "POST":
            return _Reject(405, "method_not_allowed")
        content_encodings = [
            value for name, value in headers if name.lower() == b"content-encoding"
        ]
        content_types = [value for name, value in headers if name.lower() == b"content-type"]
        content_lengths = [value for name, value in headers if name.lower() == b"content-length"]
        if content_encodings and content_encodings != [b"identity"]:
            return _Reject(415, "unsupported_encoding")
        if (
            len(content_types) != 1
            or not content_types[0].split(b";", 1)[0].strip().lower() == b"application/json"
        ):
            return _Reject(415, "unsupported_media_type")
        if len(content_lengths) > 1:
            return _Reject(400, "invalid_request")
        if not content_lengths:
            return None
        try:
            declared_length = int(content_lengths[0])
        except ValueError:
            return _Reject(400, "invalid_request")
        if declared_length < 0 or declared_length > 128_000:
            return _Reject(413, "request_too_large")
        return declared_length

    async def _send_rejection(self, send: Send, rejection: _Reject) -> None:
        """Emit the fixed error for a pre-computed rejection."""
        await self._send_fixed_error(
            send, rejection.status, rejection.code, bearer_challenge=rejection.bearer_challenge
        )

    async def _authenticate(
        self,
        scope: Scope,
        send: Send,
        token_box: list[str],
    ) -> InboundPrincipal | None:
        """Authenticate the bearer, then drop it and strip credentials from the forwarded scope.

        The token is popped from ``token_box`` so no reference survives this call. A failed
        authentication sends the uniform 401 and returns ``None``. The SDK request context needs
        Host/Origin but never the already-consumed bearer or cookies.
        """
        raw_token = token_box.pop()
        try:
            return await self._authenticate_inbound(raw_token)
        except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
            await self._send_fixed_error(send, 401, "unauthorized", bearer_challenge=True)
            return None
        finally:
            raw_token = ""
            scope["headers"] = [
                (name, value)
                for name, value in scope.get("headers", [])
                if name.lower() not in {b"authorization", b"cookie"}
            ]

    async def _read_bounded_body(
        self,
        receive: Receive,
        send: Send,
        declared_length: int | None,
        deadline: float,
    ) -> bytes | None:
        """Read the request body, answering 408/413/400 itself; ``None`` means a response was sent."""
        try:
            return await self._read_request_body(
                receive, declared_length=declared_length, deadline=deadline
            )
        except TimeoutError:
            await self._send_fixed_error(send, 408, "request_timeout")
        except OverflowError:
            await self._send_fixed_error(send, 413, "request_too_large")
        except ValueError:
            await self._send_fixed_error(send, 400, "invalid_request")
        return None

    async def _serve_admitted(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        lease: McpInboundLease,
        token_box: list[str],
        deadline: float,
    ) -> None:
        """Run an admitted request: authenticate, authorize, frame, read, fence, serve and emit.

        The owner scope is revalidated before the body is read and again after, so a revoked caller
        is rejected without an SDK call. Every rejection sends its fixed error and returns.
        """
        loop = asyncio.get_running_loop()
        identity = await self._authenticate(scope, send, token_box)
        if identity is None:
            return
        if not await lease.bind_client(identity.client_id):
            await self._send_fixed_error(send, 429, "client_capacity")
            return
        expected = await self._authorize_inbound(identity)
        if expected is None or not await self._revalidate_inbound(identity, expected):
            await self._send_fixed_error(send, 403, "forbidden")
            return
        state = InboundRequestState(identity=identity, expected=expected, lease=lease)
        framing = self._check_framing(scope.get("headers", []), scope.get("method", "").upper())
        if isinstance(framing, _Reject):
            await self._send_rejection(send, framing)
            return
        if not await self._revalidate_inbound(identity, expected):
            await self._send_fixed_error(send, 403, "forbidden")
            return
        body = await self._read_bounded_body(receive, send, framing, deadline)
        if body is None:
            return
        if not await self._revalidate_inbound(identity, expected):
            await self._send_fixed_error(send, 403, "forbidden")
            return
        if body.lstrip().startswith(b"["):
            await self._send_fixed_error(send, 400, "batch_unsupported")
            return
        state_dict = scope.setdefault("state", {})
        if not isinstance(state_dict, dict) or "bbd_mcp_authorization" in state_dict:
            await self._send_fixed_error(send, 403, "request_rejected")
            return
        state_dict["bbd_mcp_authorization"] = state
        start, response_body = await self._run_child(
            scope,
            body,
            receive,
            state,
            operation_deadline=max(
                loop.time(),
                deadline - _CLEANUP_RESERVE_SECONDS,
            ),
            cleanup_deadline=deadline,
        )
        try:
            await self._emit_response(send, start, response_body, state)
        except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
            # A failed client send cannot be repaired with a second response start.
            return

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Enforce canonical addressing, bounded admission/auth/body, then final buffered egress fences."""
        if scope.get("type") != "http":
            if scope.get("type") == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 60.0
        headers = scope.get("headers", [])
        rejection = self._precheck_request(scope, headers)
        token = self._bearer_token(headers) if rejection is None else None
        if rejection is None and isinstance(token, _Reject):
            rejection = token
        if rejection is not None:
            await self._send_rejection(send, rejection)
            return
        token_box = [cast(str, token)]
        token = None
        try:
            async with self._admission.inbound_slot(deadline) as lease:
                async with asyncio.timeout_at(deadline):
                    await self._serve_admitted(scope, receive, send, lease, token_box, deadline)
        except TimeoutError:
            await self._send_fixed_error(send, 503, "request_timeout")
        except RuntimeError:
            await self._send_fixed_error(send, 503, "admission_unavailable")
        except (ValueError, TypeError):
            await self._send_fixed_error(send, 500, "request_failed")
        except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
            await self._send_fixed_error(send, 500, "request_failed")

    async def _read_request_body(
        self,
        receive: Receive,
        *,
        declared_length: int | None,
        deadline: float,
    ) -> bytes:
        """Read at most 128KB of actual ASGI request bytes within a five-second body window."""
        loop = asyncio.get_running_loop()
        body_deadline = min(deadline, loop.time() + 5.0)
        chunks = bytearray()
        async with asyncio.timeout_at(body_deadline):
            while True:
                message = await receive()
                if message.get("type") == "http.disconnect":
                    raise asyncio.CancelledError
                if message.get("type") != "http.request":
                    raise ValueError("Invalid request stream")
                chunk = message.get("body", b"")
                if not isinstance(chunk, bytes):
                    raise ValueError("Invalid request body")
                if len(chunks) + len(chunk) > 128_000:
                    raise OverflowError("Request exceeds its byte limit")
                chunks.extend(chunk)
                if not message.get("more_body", False):
                    break
        if declared_length is not None and len(chunks) != declared_length:
            raise ValueError("Content-Length does not match the request body")
        return bytes(chunks)

    async def _run_child(
        self,
        scope: Scope,
        body: bytes,
        source_receive: Receive,
        state: InboundRequestState,
        *,
        operation_deadline: float,
        cleanup_deadline: float,
    ) -> tuple[Message, bytes]:
        """Run one SDK JSON request and always cancel/join owned tasks within the reserved cleanup window.

        The operation stops before the request deadline to reserve bounded cleanup time. Disconnect,
        timeout, and external cancellation all cancel the SDK child, native handler when captured,
        and original-receive watcher. If cancellation does not settle by the cleanup deadline, the
        admission lease is retained until its existing TTL expires. Every join exit performs one
        final synchronous native-task recapture before propagating cancellation or timeout.
        """
        receiver = InboundReplayReceiver(body, source_receive)
        response = InboundResponseBuffer(max_body_bytes=1_048_576, deadline=cleanup_deadline)
        # ASGI apps are coroutine functions at runtime; typing exposes them as generic Awaitables.
        child_task = asyncio.create_task(
            cast("Coroutine[Any, Any, None]", self._app(scope, receiver, response))
        )
        disconnect_task = asyncio.create_task(receiver.wait_for_disconnect())
        completed = False
        try:
            remaining = max(0.0, operation_deadline - asyncio.get_running_loop().time())
            done, _ = await asyncio.wait(
                {child_task, disconnect_task},
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if disconnect_task in done:
                raise asyncio.CancelledError
            if child_task not in done:
                raise TimeoutError
            child_task.result()
            sealed = response.seal()
            completed = True
            return sealed
        finally:
            # Capture again after child cancellation: middleware registers synchronously before its
            # first authorization await, but a just-started SDK child may not have run yet.
            owned = {child_task, disconnect_task}
            if state.native_task is not None:
                owned.add(state.native_task)
            if not completed:
                for task in owned:
                    if not task.done():
                        task.cancel()
            elif not disconnect_task.done():
                disconnect_task.cancel()

            pending = {task for task in owned if not task.done()}
            join_cancellation: asyncio.CancelledError | None = None
            if pending:
                # asyncio.wait does not cancel inputs; shield its waiter so cancellation can be
                # recorded as lease uncertainty before propagating to the caller.
                join_task = asyncio.create_task(
                    asyncio.wait(
                        pending,
                        timeout=max(0.0, cleanup_deadline - asyncio.get_running_loop().time()),
                    )
                )
                try:
                    await asyncio.shield(join_task)
                except asyncio.CancelledError as exc:
                    join_cancellation = exc
                    join_task.cancel()

            # Reconcile both exits together: cancellation may have let the manager register its
            # independent SDK handler after the original pending snapshot was captured.
            if state.native_task is not None:
                owned.add(state.native_task)
            unsettled = {task for task in owned if not task.done()}
            if unsettled:
                for task in unsettled:
                    task.cancel()
                state.lease.retain_until_expiry()
            if join_cancellation is not None:
                raise join_cancellation
            if completed and unsettled:
                raise TimeoutError

    async def _emit_response(
        self,
        send: Send,
        start: Message,
        body: bytes,
        state: InboundRequestState,
    ) -> None:
        """Revalidate the same principal, lease, binding, and sink before the first response byte."""
        try:
            current = await self._revalidate_inbound(state.identity, state.expected)
            fence = state.response_fence
            if current and fence.has_successful_native_result:
                current = fence.binding is not None and fence.sink is not None
            if current:
                current = await self._revalidate_inbound_output(
                    state.identity,
                    state.expected,
                    binding=fence.binding if fence.has_successful_native_result else None,
                    sink=fence.sink if fence.has_successful_native_result else None,
                )
            # The explicit lease/TTL check is last so no authorization await separates it from response.start.
            current = current and await self._admission.lease_current(inbound=state.lease)
        except Exception:  # noqa: BLE001  # fail-closed boundary: any failure denies/degrades
            current = False
        if not current:
            await self._send_fixed_error(send, 409, "authorization_changed")
            return

        headers = [
            (name, value)
            for name, value in start.get("headers", [])
            if name.lower() not in {b"cache-control", b"pragma", b"expires"}
        ]
        headers.append((b"cache-control", b"no-store"))
        clean_start = dict(start)
        clean_start["headers"] = headers
        # No await occurs between the last successful authorization check and response.start.
        await send(clean_start)
        await send({"type": "http.response.body", "body": body, "more_body": False})

    async def _send_fixed_error(
        self,
        send: Send,
        status: int,
        code: str,
        *,
        bearer_challenge: bool = False,
    ) -> None:
        """Emit a fixed no-store JSON error without request, token, provider, or database details."""
        body = json.dumps({"error": code}, separators=(",", ":")).encode("ascii")
        headers = [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]
        if bearer_challenge:
            headers.append((b"www-authenticate", b"Bearer"))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body, "more_body": False})
