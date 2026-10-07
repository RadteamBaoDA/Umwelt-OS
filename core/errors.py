import logging
import time
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from core.telemetry import bind_trace, count, observe_ms

logger = logging.getLogger("bbd.api")


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
        request_id_value = str(uuid4())
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
                    no_transform = any(value.strip().lower() == "no-transform"
                                       for value in headers.get("Cache-Control", "").split(","))
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
        structured: dict[str, Any] = exc.detail if isinstance(exc.detail, dict) else {}
        return JSONResponse(
            status_code=exc.status_code,
            headers=exc.headers,
            content={
                "error": {
                    "code": structured.get("code", f"HTTP_{exc.status_code}"),
                    "message": structured.get("message", str(exc.detail)),
                    "details": structured.get("details", {}),
                    "requestId": getattr(request.state, "request_id", ""),
                }
            },
        )

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

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception) -> JSONResponse:
        """Log unexpected request failures by request ID and return a generic 500 response without exception details."""
        request_id_value = getattr(request.state, "request_id", "")
        logger.error("Unhandled request failure; request_id=%s", request_id_value)
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
