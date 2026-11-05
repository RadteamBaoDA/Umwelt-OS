"""F2.7 regression tests: RSS reads and crawls fetch from the validated address only; dead MCP stub stays deleted."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from modules.connectors import n8n
from modules.connectors.providers.rest import UnsafeDestination

_FEED = b"<rss><channel><item><guid>1</guid><title>t</title><description>d</description></item></channel></rss>"


def _client_factory(seen: list[httpx.Request], kwargs_log: list[dict[str, Any]]) -> Any:
    """Return an AsyncClient replacement that records constructor kwargs and every request it serves."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=_FEED)

    real = httpx.AsyncClient

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs_log.append(kwargs)
        return real(transport=httpx.MockTransport(handler), **kwargs)

    return factory


@pytest.mark.asyncio
async def test_read_rss_connects_to_pinned_ip_and_ignores_environment_proxies() -> None:
    seen: list[httpx.Request] = []
    kwargs_log: list[dict[str, Any]] = []
    with (
        patch.object(n8n, "validate_public_url", AsyncMock()),
        patch("modules.connectors.providers.rest.resolve_public", AsyncMock(return_value=["93.184.216.34"])),
        patch.object(n8n.httpx, "AsyncClient", _client_factory(seen, kwargs_log)),
    ):
        out = await n8n.read_rss("https://feeds.example.com/rss", None)
    assert out["records"]
    assert kwargs_log[0]["trust_env"] is False
    request = seen[0]
    assert request.url.host == "93.184.216.34"  # not the hostname: DNS cannot be re-resolved to a private address
    assert request.headers["host"] == "feeds.example.com"
    assert request.extensions["sni_hostname"] == "feeds.example.com"


@pytest.mark.asyncio
async def test_read_rss_fails_closed_when_host_resolves_to_private_address() -> None:
    seen: list[httpx.Request] = []
    with (
        patch.object(n8n, "validate_public_url", AsyncMock()),
        patch(
            "modules.connectors.providers.rest.resolve_public",
            AsyncMock(side_effect=UnsafeDestination("non_public_destination")),
        ),
        patch.object(n8n.httpx, "AsyncClient", _client_factory(seen, [])),
        pytest.raises(UnsafeDestination),
    ):
        await n8n.read_rss("https://rebind.example.com/rss", None)
    assert seen == []


def test_crawl_has_no_unpinned_fallback() -> None:
    # Read the file: importing crawl needs the optional bs4/crawlee extras.
    source = (Path(__file__).resolve().parents[4] / "modules" / "connectors" / "crawl.py").read_text(encoding="utf-8")
    assert "fall back to validated URL" not in source
    assert "except Exception:  # noqa: BLE001 - fall back" not in source
    assert "resolve_public(parts.hostname, port)" in source


def _import_crawl() -> Any:
    """Import modules.connectors.crawl, stubbing the optional bs4/crawlee extras when they are absent."""
    import sys
    from unittest.mock import MagicMock

    for name in ("bs4", "crawlee", "crawlee.crawlers"):
        try:
            __import__(name)
        except ImportError:
            sys.modules.setdefault(name, MagicMock())
    from modules.connectors import crawl as crawl_module

    return crawl_module


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unsafe", "empty"])
async def test_crawl_fails_closed_when_pinning_cannot_resolve(failure: str) -> None:
    """A resolve_public failure or empty address list aborts the crawl with no request sent (no unpinned fallback)."""
    from uuid import uuid4

    from modules.connectors.public import CrawlRequest

    crawl_module = _import_crawl()
    resolver = AsyncMock(side_effect=UnsafeDestination("non_public_destination")) if failure == "unsafe" else AsyncMock(return_value=[])
    sent: list[Any] = []

    async def _send(self: Any, request: httpx.Request, **_: Any) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, content=b"<html></html>")

    req = CrawlRequest(
        source_id=uuid4(), source_generation=1, connector_revision=1, url="https://rebind.example.com/",
        mode="http", max_pages=1, max_depth=0, timeout_seconds=10,
    )
    expected = UnsafeDestination if failure == "unsafe" else ValueError
    with (
        patch.object(crawl_module, "validate_public_url", AsyncMock()),
        patch("modules.connectors.providers.rest.resolve_public", resolver),
        patch.object(httpx.AsyncClient, "send", _send),
        pytest.raises(expected),
    ):
        await crawl_module.crawl(req)
    assert sent == []


def test_dead_mcp_stub_that_forwarded_os_environ_is_deleted() -> None:
    assert not (Path(__file__).resolve().parents[4] / "modules" / "tools" / "mcp.py").exists()
