"""ASGI middleware that records HTTP request metrics and serves the protected ``GET /metrics`` scrape endpoint.

``/metrics`` is answered here (outermost middleware) instead of as a FastAPI route, so it never
appears in the OpenAPI schema or the route table, is not subject to session/CSRF handling, and
cannot be shadowed by a module router.

Access policy (``metrics_access``):
* ``METRICS_TOKEN`` set: ``Authorization: Bearer <token>`` is required (constant-time compare);
  network location is irrelevant, so a reverse proxy cannot accidentally widen access.
* No token, non-production mode: only a direct (no ``X-Forwarded-*``/``Forwarded`` header) client
  with a loopback or private address may scrape.
* No token, production: the endpoint is disabled and answers 404.
"""

from __future__ import annotations

import hmac
import ipaddress
import time
from typing import Literal

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from core.config import Settings
from core.metrics import REGISTRY, record_http

METRICS_PATH = "/metrics"
_PROXY_HEADERS = (b"x-forwarded-for", b"x-forwarded-host", b"x-real-ip", b"forwarded")

Access = Literal["allowed", "unauthorized", "disabled"]


def _is_internal(host: str | None) -> bool:
    """Return whether ``host`` is a loopback or private-range literal address (hostnames are not internal)."""
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


def metrics_access(
    settings: Settings, headers: dict[bytes, bytes], client_host: str | None
) -> Access:
    """Decide whether a scrape request is allowed under the policy in this module's docstring.

    Args:
        settings: Process settings (``metrics_token`` and the production/dev mode).
        headers: Lower-cased request header names to values.
        client_host: Direct peer address from the ASGI scope (never a forwarded value).
    Returns:
        ``allowed``, ``unauthorized`` (token configured but missing/wrong; answer 401) or
        ``disabled`` (no token and not an internal development caller; answer 404).
    """
    token = settings.metrics_token.get_secret_value()
    if token:
        supplied = headers.get(b"authorization", b"").decode("latin-1")
        scheme, _, value = supplied.partition(" ")
        if scheme.lower() == "bearer" and hmac.compare_digest(
            value.strip().encode(), token.encode()
        ):
            return "allowed"
        return "unauthorized"
    if (
        settings.is_dev
        and not any(name in headers for name in _PROXY_HEADERS)
        and _is_internal(client_host)
    ):
        return "allowed"
    return "disabled"


def metrics_enabled(settings: Settings) -> bool:
    """Return whether any caller could ever be admitted (token set, or non-production mode)."""
    return bool(settings.metrics_token.get_secret_value()) or settings.is_dev


class MetricsMiddleware:
    """Pure-ASGI middleware: time every HTTP request into Prometheus and answer ``GET /metrics``."""

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        """Keep the downstream app and the settings used for the scrape access policy."""
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the scrape endpoint or forward the request while recording method, route template, status, latency.

        Metrics recording is failure-isolated and never alters the response. The route template is
        read from ``scope["route"]`` after the downstream app has run (Starlette sets it on match).
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["path"] == METRICS_PATH and scope["method"] in {"GET", "HEAD"}:
            await self._scrape(scope, send)
            return
        started = time.perf_counter()
        status = 500  # an exception before response start is reported as a server error

        async def capture(message: Message) -> None:
            """Remember the response status while passing every message through unchanged."""
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            route = getattr(scope.get("route"), "path", None)
            record_http(scope["method"], route, status, time.perf_counter() - started)

    async def _scrape(self, scope: Scope, send: Send) -> None:
        """Answer a scrape: 404 when disabled, 401 on a bad token, otherwise the text exposition."""
        headers = {key.lower(): value for key, value in scope["headers"]}
        client = scope.get("client")
        verdict = metrics_access(self.settings, headers, client[0] if client else None)
        if verdict == "allowed":
            status, body, extra = (
                200,
                generate_latest(REGISTRY),
                [(b"content-type", CONTENT_TYPE_LATEST.encode())],
            )
        elif verdict == "unauthorized":
            status, body, extra = 401, b"unauthorized", [(b"www-authenticate", b"Bearer")]
        else:
            status, body, extra = 404, b"not found", []
        body_out = b"" if scope["method"] == "HEAD" else body
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    *extra,
                    (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body_out})
