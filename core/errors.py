import logging
import time
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.telemetry import bind_trace, count, observe_ms

logger = logging.getLogger("bbd.api")
_TIMEOUT_SQLSTATES = {"57014", "55P03"}

def install_error_handling(app: FastAPI) -> None:
    """Install request ID, private cache headers, and structured HTTP, validation, and generic error responses."""

    @app.middleware("http")
    async def request_id(request: Request, call_next: Any) -> Any:
        """Attach a fresh request ID and apply no-store cache policy to API responses."""
        request.state.request_id = str(uuid4())
        started = time.perf_counter()
        status = 500
        try:
            with bind_trace(request_id=request.state.request_id):
                response = await call_next(request)
            status = response.status_code
        finally:
            # Label is the matched route template (e.g. /api/v1/sources/{source_id}), never the raw
            # path, so cardinality is bounded by the route table; unmatched/mounted paths fold to one value.
            route = request.scope.get("route")
            template = getattr(route, "path", None) or "unmatched"
            labels = {"method": request.method, "route": template}
            observe_ms("api_request_ms", started, **labels)
            count("api_requests_total", **labels, status_class=f"{status // 100}xx")
        response.headers["X-Request-ID"] = request.state.request_id
        if request.url.path.startswith("/api/v1/auth/"):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path.startswith("/api/v1/"):
            response.headers["Cache-Control"] = "private, no-store"
        return response

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
