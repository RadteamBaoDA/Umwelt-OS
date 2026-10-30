"""Feed fetch failures keep their HTTP/transport identity so ``_classify`` can retry or reject correctly."""
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from modules.connectors import collection
from modules.connectors.providers import feed_catalog


async def _allow() -> None:
    """Fence callback that always admits the send."""


def _serve(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    """Route ``_read_feed``'s private httpx client through an in-memory transport."""
    real = httpx.AsyncClient
    monkeypatch.setattr(
        feed_catalog.httpx, "AsyncClient",
        lambda **kw: real(**{**kw, "transport": httpx.MockTransport(handler)}),
    )


async def _classified(monkeypatch: pytest.MonkeyPatch, handler: Any) -> dict[str, Any]:
    """Run ``_read_feed`` against ``handler`` and classify the keyless failure it raises."""
    _serve(monkeypatch, handler)
    with pytest.raises(BaseException) as caught:
        await feed_catalog._read_feed("https://www.youtube.com/feeds/videos.xml?channel_id=x", _allow)
    keyless = collection._sends_no_credential(SimpleNamespace(provider="youtube", type="rss"))
    return collection._classify(caught.value, keyless=keyless)


@pytest.mark.parametrize(("status", "code", "retryable"), [
    (404, "provider_rejected", False),
    (403, "provider_rejected", False),
    (401, "provider_rejected", False),
    (500, "provider_unavailable", True),
    (503, "provider_unavailable", True),
    (408, "provider_unavailable", True),
    (302, "provider_rejected", False),
])
async def test_http_status_maps_to_code(monkeypatch: pytest.MonkeyPatch, status: int, code: str, retryable: bool) -> None:
    """Non-2xx statuses become ``ProviderHttpError`` so 5xx retries and 4xx is a provider rejection."""
    got = await _classified(monkeypatch, lambda _r: httpx.Response(status))
    assert got["error_code"] == code and got.get("retryable", False) is retryable


@pytest.mark.parametrize("exc", [httpx.ConnectError("boom"), httpx.ReadTimeout("slow")])
async def test_transport_errors_are_retryable_timeouts(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    """Connect/read failures propagate unchanged and classify as retryable ``provider_timeout``."""
    def handler(_r: httpx.Request) -> httpx.Response:
        raise exc

    got = await _classified(monkeypatch, handler)
    assert got == {"outcome": "failed", "error_code": "provider_timeout", "retryable": True}


def test_malformed_xml_is_response_invalid() -> None:
    """A body that is not XML stays a non-retryable ``provider_response_invalid`` ValueError."""
    source = SimpleNamespace(provider="youtube", configuration={"youtube_channel_id": "UC" + "a" * 22})
    with pytest.raises(ValueError) as caught:
        feed_catalog._records_from_feed(source, b"<feed><unclosed>", datetime.now(UTC))  # type: ignore[arg-type]
    assert collection._classify(caught.value, keyless=True) == {
        "outcome": "failed", "error_code": "provider_response_invalid"}
