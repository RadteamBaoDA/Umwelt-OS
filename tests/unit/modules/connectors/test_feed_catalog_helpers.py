"""Pure-helper tests for provider feed parsing and rate-limit deadline derivation."""

from datetime import UTC, datetime, timedelta
from xml.etree import ElementTree

import httpx
import pytest

from modules.connectors.providers import feed_catalog as fc

NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
INVALID = "provider_rate_deadline_invalid"


def test_plain_text_strips_markup_and_active_content() -> None:
    html = "<p>Hello <b>world</b></p><script>alert(1)</script><style>x{}</style><div>Next &amp; last</div>"
    assert fc._plain_text(html) == "Hello world Next & last"


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ("", None),
        ("2026-01-01T00:00:00Z", datetime(2026, 1, 1, tzinfo=UTC)),
        ("2026-01-01T02:00:00+02:00", datetime(2026, 1, 1, tzinfo=UTC)),
        ("Thu, 01 Jan 2026 00:00:00 GMT", datetime(2026, 1, 1, tzinfo=UTC)),
        ("2026-01-01T00:00:00", None),
        ("garbage", None),
    ],
)
def test_parse_time_returns_aware_utc_or_none(value: str | None, expected: datetime | None) -> None:
    assert fc._parse_time(value) == expected


def test_structured_api_reset_picks_api_bucket() -> None:
    header = '"pages";r=10;t=5, "api";r=3;t=42, "resolvers";r=1;t=9'
    assert fc._structured_api_reset(header) == 42
    assert fc._structured_api_reset('"pages";r=10;t=5') is None


@pytest.mark.parametrize(
    "header",
    ['"api";r=1;t=2, "api";r=1;t=3', "nonsense", '"other";r=1;t=1', '"api";r=1;t=' + "9" * 5000],
)
def test_structured_api_reset_rejects_bad_or_duplicate_buckets(header: str) -> None:
    with pytest.raises(ValueError, match=INVALID):
        fc._structured_api_reset(header)


def test_retry_after_seconds_and_http_date() -> None:
    assert fc._retry_after_deadline("30", NOW) == NOW + timedelta(seconds=30)
    future = "Fri, 01 May 2026 12:05:00 GMT"
    assert fc._retry_after_deadline(future, NOW) == datetime(2026, 5, 1, 12, 5, tzinfo=UTC)
    past = "Fri, 01 May 2026 11:00:00 GMT"
    assert fc._retry_after_deadline(past, NOW) == NOW + timedelta(seconds=1)


@pytest.mark.parametrize("value", ["0", "-5", "soon", str(10**18)])
def test_retry_after_rejects_invalid(value: str) -> None:
    with pytest.raises(ValueError, match=INVALID):
        fc._retry_after_deadline(value, NOW)


def test_epoch_reset_must_be_future() -> None:
    future = NOW + timedelta(minutes=2)
    assert fc._epoch_reset_deadline(str(future.timestamp()), NOW) == future
    for bad in (str((NOW - timedelta(seconds=1)).timestamp()), "x", "1e999"):
        with pytest.raises(ValueError, match=INVALID):
            fc._epoch_reset_deadline(bad, NOW)


def test_standard_reset_accepts_fractional_positive_seconds() -> None:
    assert fc._standard_reset_deadline("1.5", NOW) == NOW + timedelta(seconds=1.5)
    for bad in ("0", "-1", "abc", "1e999"):
        with pytest.raises(ValueError, match=INVALID):
            fc._standard_reset_deadline(bad, NOW)


def test_retry_deadline_defaults_to_one_minute_without_evidence() -> None:
    assert fc._retry_deadline(httpx.Headers({}), NOW) == NOW + timedelta(minutes=1)


def test_retry_deadline_uses_the_latest_of_all_supplied_waits() -> None:
    headers = httpx.Headers(
        {
            "retry-after": "10",
            "ratelimit": '"api";r=0;t=90',
            "ratelimit-reset": "30",
            "x-ratelimit-reset": str((NOW + timedelta(seconds=45)).timestamp()),
        }
    )
    assert fc._retry_deadline(headers, NOW) == NOW + timedelta(seconds=90)


def test_retry_deadline_fails_closed_on_any_malformed_header() -> None:
    with pytest.raises(ValueError, match=INVALID):
        fc._retry_deadline(httpx.Headers({"retry-after": "10", "ratelimit-reset": "bogus"}), NOW)


def test_selected_hash_is_order_independent_and_value_sensitive() -> None:
    assert fc._selected_hash({"a": 1, "b": "x"}) == fc._selected_hash({"b": "x", "a": 1})
    assert fc._selected_hash({"a": 1}) != fc._selected_hash({"a": 2})


def _entry(href: str | None) -> ElementTree.Element:
    link = (
        f'<link rel="alternate" href="{href.replace("&", "&amp;") if href else ""}"/>'
        if href is not None
        else ""
    )
    return ElementTree.fromstring(f'<entry xmlns="http://www.w3.org/2005/Atom">{link}</entry>')  # noqa: S314 - fixed local test XML


@pytest.mark.parametrize(
    "provider,identity,href,kept",
    [
        ("youtube", "yt:video:abc", "https://www.youtube.com/watch?v=abc", True),
        ("youtube", "yt:video:abc", "https://www.youtube.com/watch?v=other", False),
        ("youtube", "yt:video:abc", "http://www.youtube.com/watch?v=abc", False),
        ("youtube", "yt:video:abc", "https://www.youtube.com/watch?v=abc&x=1", False),
        ("youtube", "yt:video:abc", "https://evil.example/watch?v=abc", False),
        ("arxiv", "oai:arXiv.org:1", "https://arxiv.org/abs/2401.00001", True),
        ("arxiv", "oai:arXiv.org:1", "https://arxiv.org/pdf/2401.00001", False),
        ("arxiv", "oai:arXiv.org:1", "https://user:pw@arxiv.org/abs/1", False),
        ("arxiv", "oai:arXiv.org:1", "https://arxiv.org/abs/1#frag", False),
        ("arxiv", "oai:arXiv.org:1", None, False),
        ("other", "x", "https://arxiv.org/abs/1", False),
    ],
)
def test_feed_canonical_keeps_only_matching_provider_links(
    provider: str, identity: str, href: str | None, kept: bool
) -> None:
    assert fc._feed_canonical(_entry(href), provider, identity) == (href if kept else None)


@pytest.mark.parametrize(
    "feed_id,configured,same",
    [
        ("UC" + "a" * 22, "a" * 22, True),
        ("a" * 22, "UC" + "a" * 22, True),
        ("UC" + "a" * 22, "UC" + "b" * 22, False),
        (None, "a" * 22, False),
    ],
)
def test_same_youtube_channel_normalizes_uc_prefix(
    feed_id: str | None, configured: str, same: bool
) -> None:
    assert fc._same_youtube_channel(feed_id, configured) is same


@pytest.mark.asyncio
async def test_send_fence_trace_uses_explicit_recheck_for_wrapped_gates():
    from modules.connectors.providers.feed_catalog import send_fence_trace

    calls = []

    async def wrapped_gate():  # debits: must never run at header time
        calls.append("debit")

    async def recheck():
        calls.append("recheck")

    hook = send_fence_trace(wrapped_gate, recheck)
    await hook("http11.send_request_headers.started", {})
    await hook("http11.send_request_body.started", {})
    assert calls == ["recheck"]
