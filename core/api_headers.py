"""Default non-document security headers for API responses."""

from typing import Final

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Keep equal to API_FALLBACK_CSP in apps/web/src/core/csp.ts. API bodies are never documents.
API_DEFAULT_CSP: Final = "default-src 'none'; frame-ancestors 'none'; sandbox"


class ApiDefaultHeadersMiddleware:
    """Pure ASGI middleware adding a restrictive CSP and nosniff to API responses that do not set their own.

    Needed because Next ``headers()`` are not applied to responses proxied through the ``/api`` rewrite
    (verified with ``next start``), so the API itself is the only layer every deployment path shares.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Bind the downstream ASGI application."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Set missing ``Content-Security-Policy``/``X-Content-Type-Options`` on ``http.response.start`` only.

        Existing values (e.g. the media route's own CSP) are never overridden; bodies and streaming are untouched.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_headers(message: Message) -> None:
            """Add the defaults to the response start message."""
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if "content-security-policy" not in headers:
                    headers["Content-Security-Policy"] = API_DEFAULT_CSP
                if "x-content-type-options" not in headers:
                    headers["X-Content-Type-Options"] = "nosniff"
            await send(message)

        await self.app(scope, receive, with_headers)
