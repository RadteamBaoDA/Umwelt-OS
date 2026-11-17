"""Pure-helper tests for provider feed parsing and rate-limit deadline derivation."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from xml.etree import ElementTree

import httpx
import pytest

from modules.connectors.providers import feed_catalog as fc
from modules.connectors.providers.rest import Fetched

NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
INVALID = "provider_rate_deadline_invalid"


@pytest.mark.asyncio
async def test_youtube_feed_uses_bounded_transport_with_send_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    from modules.connectors.providers import rest

    fetch = AsyncMock(return_value=Fetched(b'<feed xmlns="http://www.w3.org/2005/Atom"/>'))
    monkeypatch.setattr(rest, "fetch_bounded", fetch)
    source = SimpleNamespace(
        provider="youtube",
        type="rss",
        configuration={"youtube_channel_id": "UC" + "A" * 22, "history_mode": "returned_snapshot"},
    )
    gate = AsyncMock()
    page = await fc.collect_provider_feed(
        source,  # type: ignore[arg-type]
        collected_at=NOW,
        session_factory=AsyncMock(),
        before_request=gate,
    )
    assert page.records == ()
    assert fetch.await_args.kwargs["before_send"] is gate
    assert fetch.await_args.kwargs["max_bytes"] == fc._MAX_PAGE_BYTES


@pytest.mark.asyncio
async def test_youtube_snapshot_reuses_and_refreshes_url_scoped_validators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.connectors.providers import rest

    channel = "UC" + "A" * 22
    feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel}"
    fetch = AsyncMock(
        return_value=Fetched(
            b'<feed xmlns="http://www.w3.org/2005/Atom"/>',
            etag='"next"',
            last_modified="Sat, 01 May 2026 12:00:00 GMT",
        )
    )
    monkeypatch.setattr(rest, "fetch_bounded", fetch)
    source = SimpleNamespace(
        provider="youtube",
        type="rss",
        configuration={"youtube_channel_id": channel, "history_mode": "returned_snapshot"},
    )

    page = await fc.collect_youtube_snapshot(
        source,  # type: ignore[arg-type]
        collected_at=NOW,
        before_request=AsyncMock(),
        validators={feed_url: {"etag": '"old"', "last_modified": "Fri, 01 May 2026 12:00:00 GMT"}},
    )

    assert fetch.await_args.kwargs["headers"]["If-None-Match"] == '"old"'
    assert fetch.await_args.kwargs["headers"]["If-Modified-Since"] == "Fri, 01 May 2026 12:00:00 GMT"
    assert page.validators == {
        feed_url: {"etag": '"next"', "last_modified": "Sat, 01 May 2026 12:00:00 GMT"}
    }


@pytest.mark.asyncio
async def test_youtube_snapshot_retains_validators_on_not_modified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.connectors.providers import rest

    channel = "UC" + "A" * 22
    feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel}"
    fetch = AsyncMock(return_value=Fetched(None))
    monkeypatch.setattr(rest, "fetch_bounded", fetch)
    source = SimpleNamespace(
        provider="youtube",
        type="rss",
        configuration={"youtube_channel_id": channel, "history_mode": "returned_snapshot"},
    )
    saved = {feed_url: {"etag": '"stable"'}}

    page = await fc.collect_youtube_snapshot(
        source,  # type: ignore[arg-type]
        collected_at=NOW,
        before_request=AsyncMock(),
        validators=saved,
    )

    assert page.records == ()
    assert page.validators == saved


def test_youtube_validator_cursor_is_bound_to_query_scope() -> None:
    state = {"https://www.youtube.com/feeds/videos.xml?channel_id=UC" + "A" * 22: {"etag": '"v1"'}}
    cursor = fc.encode_youtube_validator_cursor("a" * 24, state)

    assert fc.decode_youtube_validator_cursor(cursor, "a" * 24) == state
    assert fc.decode_youtube_validator_cursor(cursor, "b" * 24) == {}


def test_youtube_targets_build_bounded_channel_and_playlist_scopes() -> None:
    from modules.connectors.public import ConnectorConfig

    channel = "UC" + "A" * 22
    config = ConnectorConfig.model_validate(
        {"youtube_channels": [channel], "youtube_filter": "shorts"}
    )
    assert fc._youtube_targets(config) == (
        fc._YoutubeTarget(
            f"https://www.youtube.com/feeds/videos.xml?playlist_id=UUSH{'A' * 22}",
            None,
            channel,
        ),
    )
    handle = ConnectorConfig.model_validate({"youtube_handles": ["@Example_Channel"]})
    assert fc._youtube_targets(handle) == (fc._YoutubeTarget(None, "example_channel", None),)
    assert fc._youtube_channel_id_from_page(
        f'<link rel="canonical" href="https://www.youtube.com/channel/{channel}">'.encode()
    ) == channel
    legacy_source = SimpleNamespace(
        provider="youtube", type="rss",
        configuration={"youtube_channel_id": channel, "history_mode": "returned_snapshot"},
    )
    assert fc.provider_feed_url(legacy_source) == (
        f"https://www.youtube.com/feeds/videos.xml?channel_id={channel}"
    )
    multi_source = SimpleNamespace(
        provider="youtube", type="rss",
        configuration={
            "youtube_channels": [channel, "UC" + "B" * 22],
            "history_mode": "returned_snapshot",
        },
    )
    with pytest.raises(ValueError, match="youtube_multiple_targets"):
        fc.provider_feed_url(multi_source)
    with pytest.raises(ValueError, match="provider_scope_invalid"):
        fc._youtube_targets(
            ConnectorConfig.model_validate(
                {"youtube_channel_id": channel, "youtube_handles": ["example_channel"]}
            )
        )
    with pytest.raises(ValueError, match="youtube_filter_requires_channel_scope"):
        fc._youtube_targets(
            ConnectorConfig.model_validate(
                {"youtube_playlist_ids": ["PL" + "A" * 20], "youtube_filter": "shorts"}
            )
        )
    with pytest.raises(ValueError, match="provider_scope_invalid"):
        fc._youtube_targets(ConnectorConfig.model_validate({}))


@pytest.mark.parametrize(
    ("youtube_filter", "feed_prefix"),
    [
        ("all", None),
        ("long_form", "playlist_id=UULF"),
        ("shorts", "playlist_id=UUSH"),
    ],
)
@pytest.mark.asyncio
async def test_youtube_handle_resolution_and_feed_are_both_send_gated(
    monkeypatch: pytest.MonkeyPatch, youtube_filter: str, feed_prefix: str | None,
) -> None:
    from modules.connectors.providers import rest

    channel = "UC" + "A" * 22
    feed = (
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:yt="http://www.youtube.com/xml/schemas/2015"><entry>'
        f"<yt:videoId>{'v' * 11}</yt:videoId><yt:channelId>{channel}</yt:channelId>"
        "<title>Example video</title><published>2026-01-01T00:00:00+00:00</published></entry></feed>"
    ).encode()
    handle_page = f'<meta itemprop="channelId" content="{channel}">'.encode()
    calls: list[tuple[str, object]] = []
    gate = AsyncMock()

    async def fake_fetch(url: str, **kwargs: object) -> Fetched:
        calls.append((url, kwargs.get("before_send")))
        return Fetched(handle_page if "/@" in url else feed)

    monkeypatch.setattr(rest, "fetch_bounded", fake_fetch)
    source = SimpleNamespace(
        provider="youtube",
        type="rss",
        configuration={
            "youtube_handles": ["@Example_Channel"], "youtube_filter": youtube_filter,
            "history_mode": "returned_snapshot",
        },
    )
    page = await fc.collect_provider_feed(
        source,  # type: ignore[arg-type]
        collected_at=NOW,
        session_factory=AsyncMock(),
        before_request=gate,
    )
    expected_scope = (
        f"channel_id={channel}" if feed_prefix is None else f"{feed_prefix}{channel[2:]}"
    )
    assert [url for url, _ in calls] == [
        "https://www.youtube.com/@example_channel",
        f"https://www.youtube.com/feeds/videos.xml?{expected_scope}",
    ]
    assert all(callback is gate for _, callback in calls)
    assert [record.provider_id for record in page.records] == [f"youtube:{channel}:{'v' * 11}"]


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
