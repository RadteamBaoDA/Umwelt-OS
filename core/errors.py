import logging
import time
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from core.telemetry import bind_trace, count, observe_ms

logger = logging.getLogger("bbd.api")
_TIMEOUT_SQLSTATES = {"57014", "55P03"}

class RequestContextMiddleware:
    """Pure-ASGI request ID, private cache headers and request metrics.

    Pure ASGI on purpose: Starlette's BaseHTTPMiddleware relays body chunks through a task and a
    memory stream, drops empty chunks, and returns the app's `send` before the bytes reach the
    transport. Chat SSE relies on `send` reaching uvicorn directly (empty chunk = drain outside its
    locked transaction; batch chunk = transport.write while the locks are held; chat/routes.py).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid4())
        Request(scope).state.request_id = request_id
        path: str = scope["path"]
        cache = (
            "no-store" if path.startswith("/api/v1/auth/")
            else "private, no-store" if path.startswith("/api/v1/") else None
        )
        started = time.perf_counter()
        recorded = False

        def record(status: int) -> None:
            nonlocal recorded
            if recorded:
                return
            recorded = True
            # Label is the matched route template (e.g. /api/v1/sources/{source_id}), never the raw
            # path, so cardinality is bounded by the route table; unmatched/mounted paths fold to one value.
            template = getattr(scope.get("route"), "path", None) or "unmatched"
            labels = {"method": scope["method"], "route": template}
            observe_ms("api_request_ms", started, **labels)
            count("api_requests_total", **labels, status_class=f"{status // 100}xx")

        async def send_with_context(message: Message) -> None:
            if message["type"] == "http.response.start":
                record(int(message["status"]))  # time to response start, as before
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                if cache is not None:
                    headers["Cache-Control"] = cache
            await send(message)

        try:
            with bind_trace(request_id=request_id):
                await self.app(scope, receive, send_with_context)
        finally:
            record(500)


def install_error_handling(app: FastAPI) -> None:
    """Install request ID, private cache headers, and structured HTTP, validation, and generic error responses."""

    app.add_middleware(RequestContextMiddleware)

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

    @app.exception_handler(DBAPIError)
    async def database_error(request: Request, exc: DBAPIError) -> JSONResponse:
        """Map only statement/lock timeouts (SQLSTATE 57014/55P03, e.g. privacy-lock waits) to retryable 503; others stay 500."""
        orig = exc.orig
        code = getattr(orig, "sqlstate", None) or getattr(getattr(orig, "__cause__", None), "sqlstate", None)
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
