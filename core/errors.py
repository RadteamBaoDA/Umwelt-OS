import logging
import time
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from sqlalchemy.exc import DBAPIError
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from core.ids import uuid7
from core.telemetry import bind_trace, count, observe_ms

logger = logging.getLogger("bbd.api")
_TIMEOUT_SQLSTATES = {"57014", "55P03"}


class DomainError(Exception):
    """Framework-independent failure that the API renders exactly like an ``HTTPException``.

    Module and worker code may raise a subclass without importing FastAPI/Starlette. The
    handler installed by ``install_error_handling`` produces the same status, headers and
    ``{"error": {...}}`` envelope as the HTTP path, so migrating a raise site is behaviour
    neutral for API clients. ``detail`` is either a string or a structured mapping carrying
    ``code``/``message``/``details``; ``None`` falls back to the standard HTTP reason phrase
    (matching Starlette). Callers that must treat both exception families uniformly use
    ``error_status``.
    """

    status_code: int = 500

    def __init__(
        self,
        detail: Any = None,
        *,
        headers: dict[str, str] | None = None,
        status_code: int | None = None,
    ) -> None:
        """Capture the client-visible detail/headers; ``status_code`` overrides the class default.

        Args:
            detail: String or structured mapping; ``None`` selects the HTTP phrase for the status.
            headers: Optional response headers (for example ``Retry-After``).
            status_code: Rare per-instance override for codes without a dedicated subclass.
        """
        if status_code is not None:
            self.status_code = status_code
        if detail is None:
            detail = HTTPStatus(self.status_code).phrase
        self.detail = detail
        self.headers = headers
        super().__init__(detail)

    def __str__(self) -> str:
        """Return ``<status>: <detail>`` mirroring ``starlette.exceptions.HTTPException``."""
        return f"{self.status_code}: {self.detail}"


class BadRequestError(DomainError):
    """HTTP 400 equivalent."""

    status_code = 400


class UnauthorizedError(DomainError):
    """HTTP 401 equivalent."""

    status_code = 401


class ForbiddenError(DomainError):
    """HTTP 403 equivalent."""

    status_code = 403


class NotFoundError(DomainError):
    """HTTP 404 equivalent."""

    status_code = 404


class ConflictError(DomainError):
    """HTTP 409 equivalent."""

    status_code = 409


class GoneError(DomainError):
    """HTTP 410 equivalent."""

    status_code = 410


class PayloadTooLargeError(DomainError):
    """HTTP 413 equivalent."""

    status_code = 413


class UnsupportedMediaTypeError(DomainError):
    """HTTP 415 equivalent."""

    status_code = 415


class ValidationFailedError(DomainError):
    """HTTP 422 equivalent."""

    status_code = 422


class PreconditionRequiredError(DomainError):
    """HTTP 428 equivalent."""

    status_code = 428


class RateLimitedError(DomainError):
    """HTTP 429 equivalent."""

    status_code = 429


class BadGatewayError(DomainError):
    """HTTP 502 equivalent."""

    status_code = 502


class UnavailableError(DomainError):
    """HTTP 503 equivalent."""

    status_code = 503


class GatewayTimeoutError(DomainError):
    """HTTP 504 equivalent."""

    status_code = 504


def error_status(exc: BaseException) -> int | None:
    """Return the HTTP status carried by a ``DomainError`` or Starlette/FastAPI ``HTTPException``.

    Worker and background code uses this to branch on failure class without caring which
    family a collaborator raised. Any other exception returns ``None``.
    """
    if isinstance(exc, (DomainError, StarletteHTTPException)):
        return int(exc.status_code)
    return None


def is_http_error(exc: BaseException) -> bool:
    """Return whether ``exc`` is either HTTP-shaped family (``error_status`` is not ``None``)."""
    return error_status(exc) is not None


def error_detail(exc: BaseException) -> Any:
    """Return the ``detail`` of either HTTP-shaped family, or ``None`` for other exceptions."""
    if isinstance(exc, (DomainError, StarletteHTTPException)):
        return exc.detail
    return None


def render_http_error(
    request: Request,
    status_code: int,
    detail: Any,
    headers: Any,
) -> JSONResponse:
    """Build the stable API error envelope shared by ``HTTPException`` and ``DomainError``.

    Structured (mapping) details may supply ``code``/``message``/``details``; otherwise the
    code is ``HTTP_<status>`` and the message is ``str(detail)``. The request ID is read from
    request state; headers are forwarded unchanged.
    """
    structured: dict[str, Any] = detail if isinstance(detail, dict) else {}
    return JSONResponse(
        status_code=status_code,
        headers=headers,
        content={
            "error": {
                "code": structured.get("code", f"HTTP_{status_code}"),
                "message": structured.get("message", str(detail)),
                "details": structured.get("details", {}),
                "requestId": getattr(request.state, "request_id", ""),
            }
        },
    )


def _record_request_start(scope: Scope, started: float, status: int) -> None:
    """Record response-start latency/status or a pre-start failure, using bounded route labels.

    The caller invokes this once per HTTP request; stream lifetime and downstream send
    backpressure are not api_request_ms. Route templates replace raw paths/identities.
    Telemetry's own isolation preserves the request outcome if recording fails.
    """
    route = scope.get("route")
    template = getattr(route, "path", None) or "unmatched"
    labels = {"method": scope["method"], "route": template}
    observe_ms("api_request_ms", started, **labels)
    count("api_requests_total", **labels, status_class=f"{status // 100}xx")


class _RequestIdMiddleware:
    """Attach request correlation/private headers while directly forwarding actual ASGI sends.

    No call_next, response/body copy, task, queue or stream relay intervenes between the
    realtime publication fence and downstream transport. Exception envelopes stay owned
    by the existing handlers; this adapter adds headers and start-timing metrics only.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Capture the downstream application at the existing request-ID registration position."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Bind one request ID and emit one metric at start or pre-start failure/cancellation.

        HTTP state is shared with Request/error handlers. Every send directly awaits the
        passed callback; private API/auth cache policy preserves existing no-transform
        and all other headers, including Vary/cookies. Non-HTTP scopes pass unchanged.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id_value = str(uuid7())
        scope.setdefault("state", {})["request_id"] = request_id_value
        started = time.perf_counter()
        recorded = False

        async def send_with_request_id(message: Message) -> None:
            """Decorate response-start headers/metrics, then await the actual downstream callback."""
            nonlocal recorded
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id_value
                path = scope.get("path", "")
                if path.startswith("/api/v1/"):
                    # Preserve the SSE no-transform directive while retaining the
                    # existing auth/API no-store override of cacheable responses.
                    no_transform = any(
                        value.strip().lower() == "no-transform"
                        for value in headers.get("Cache-Control", "").split(",")
                    )
                    policy = "no-store" if path.startswith("/api/v1/auth/") else "private, no-store"
                    headers["Cache-Control"] = policy + (", no-transform" if no_transform else "")
                if not recorded:
                    recorded = True
                    _record_request_start(scope, started, int(message["status"]))
            await send(message)

        with bind_trace(request_id=request_id_value):
            try:
                await self.app(scope, receive, send_with_request_id)
            finally:
                # Mark before recording so a later send/error cannot double-count.
                # A failed/cancelled request before start retains the original500 metric.
                if not recorded:
                    recorded = True
                    _record_request_start(scope, started, 500)


def install_error_handling(app: FastAPI) -> None:
    """Install direct request-ID/header/metric forwarding and unchanged structured error handlers.

    Registration order is unchanged; pure ASGI forwarding preserves realtime's actual-send
    fence. HTTP status/header details, validation redaction and generic500 envelopes below
    retain their existing contracts; this installs no broader authorization middleware.
    """
    app.add_middleware(_RequestIdMiddleware)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Serialize framework HTTP errors using the stable API error envelope and preserve status and headers."""
        return render_http_error(request, exc.status_code, exc.detail, exc.headers)

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
        """Serialize framework-independent domain errors with the identical HTTP error envelope."""
        return render_http_error(request, exc.status_code, exc.detail, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Serialize request validation failures without echoing submitted values or validation context."""
        details = [
            {key: value for key, value in item.items() if key not in {"input", "ctx"}}
            for item in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Request validation failed",
                    "details": details,
                    "requestId": getattr(request.state, "request_id", ""),
                }
            },
        )

    @app.exception_handler(DBAPIError)
    async def database_error(request: Request, exc: DBAPIError) -> JSONResponse:
        """Map only statement/lock timeouts (SQLSTATE 57014/55P03, e.g. privacy-lock waits) to retryable 503; others stay 500."""
        orig = exc.orig
        code = getattr(orig, "sqlstate", None) or getattr(
            getattr(orig, "__cause__", None), "sqlstate", None
        )
        if code not in _TIMEOUT_SQLSTATES:
            return await internal_error(request, exc)
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": "5"},
            content={
                "error": {
                    "code": "TEMPORARILY_UNAVAILABLE",
                    "message": "The service is busy; retry shortly",
                    "details": {},
                    "requestId": getattr(request.state, "request_id", ""),
                }
            },
        )

    @app.exception_handler(RedisError)
    async def queue_unavailable(request: Request, exc: RedisError) -> JSONResponse:
        """Map an unhandled Redis failure (down, failing over, or ``OOM`` under ``noeviction``) to a retryable 503.

        Enqueues and rate-limit/session writes fail loudly rather than being dropped; the client is told to retry.
        Only the exception class is logged: Redis messages can carry key names or URLs.
        """
        logger.warning(
            "Redis unavailable; request_id=%s error=%s",
            getattr(request.state, "request_id", ""),
            type(exc).__name__,
        )
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": "5"},
            content={
                "error": {
                    "code": "QUEUE_UNAVAILABLE",
                    "message": "The job queue is temporarily unavailable; retry shortly",
                    "details": {},
                    "requestId": getattr(request.state, "request_id", ""),
                }
            },
        )

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception) -> JSONResponse:
        """Log unexpected request failures by request ID and return a generic 500 response without exception details."""
        request_id_value = getattr(request.state, "request_id", "")
        logger.error("Unhandled request failure; request_id=%s", request_id_value, exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "The request could not be completed",
                    "details": {},
                    "requestId": request_id_value,
                }
            },
        )
