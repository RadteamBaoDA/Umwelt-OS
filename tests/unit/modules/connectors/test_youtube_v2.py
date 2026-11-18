"""YouTube most-popular chart mode, Atom star-rating filter and their scope/validation contracts."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from modules.connectors.backends import PROVIDER_SCOPE_FIELDS
from modules.connectors.provider_options import validate_options
from modules.connectors.providers import rest
from modules.connectors.providers.feed_catalog import _records_from_feed, youtube_query_fingerprint
from modules.connectors.providers.media_native import collect_youtube_api
from modules.connectors.service.schemas import ConnectorConfig
from modules.connectors.service.scope import OPTIONAL_SCOPE_FIELDS, _scope_values

NOW = datetime(2026, 11, 18, tzinfo=UTC)
CHANNEL = "UC" + "a" * 22
VIDEO = "v" * 11


def _chart_body(count: int = 1) -> bytes:
    items = [
        {
            "id": f"{VIDEO[:-1]}{index}",
            "snippet": {"channelId": CHANNEL, "title": f"Hit {index}", "channelTitle": "Ch"},
            "contentDetails": {"duration": "PT2M"},
            "statistics": {"viewCount": "10"},
        }
        for index in range(count)
    ]
    return json.dumps({"items": items}).encode()


@pytest.mark.asyncio
async def test_most_popular_sends_one_chart_request_with_region_and_category() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def fetch(url: str, **kwargs: object) -> rest.Fetched:
        calls.append((url, kwargs))
        return rest.Fetched(_chart_body(2), etag='"chart"')

    cfg = ConnectorConfig(
        youtube_api_mode="most_popular", media_country="VN", youtube_video_category_id="10"
    )
    page = await collect_youtube_api(cfg, "mock-key", NOW, fetch, None)
    assert len(calls) == 1
    split = urlsplit(calls[0][0])
    assert split.path.endswith("/videos")
    query = parse_qs(split.query)
    assert query["chart"] == ["mostPopular"]
    assert query["regionCode"] == ["VN"]
    assert query["videoCategoryId"] == ["10"]
    assert query["part"] == ["snippet,contentDetails,statistics"]
    assert int(query["maxResults"][0]) <= 50
    assert "mock-key" not in calls[0][0]
    assert calls[0][1]["headers"]["X-Goog-Api-Key"] == "mock-key"  # type: ignore[index]
    # media_country is the request region, so it must not filter the chart's records away.
    assert len(page.records) == 2
    assert page.coverage == "returned_snapshot"


@pytest.mark.asyncio
async def test_most_popular_omits_unset_region_and_uses_etag_for_304() -> None:
    seen: list[dict[str, object]] = []

    async def first(url: str, **kwargs: object) -> rest.Fetched:
        assert "regionCode" not in parse_qs(urlsplit(url).query)
        assert "videoCategoryId" not in parse_qs(urlsplit(url).query)
        return rest.Fetched(_chart_body(), etag='"chart"')

    cfg = ConnectorConfig(youtube_api_mode="most_popular")
    page = await collect_youtube_api(cfg, "mock-key", NOW, first, None)

    async def conditional(url: str, **kwargs: object) -> rest.Fetched:
        seen.append(kwargs["headers"])  # type: ignore[arg-type]
        return rest.Fetched(None)

    unchanged = await collect_youtube_api(cfg, "mock-key", NOW, conditional, page.cursor_after)
    assert seen[0]["If-None-Match"] == '"chart"'  # type: ignore[index]
    assert not unchanged.records
    assert unchanged.cursor_after == page.cursor_after


@pytest.mark.asyncio
async def test_most_popular_cursor_does_not_cross_region_change() -> None:
    async def fetch(url: str, **kwargs: object) -> rest.Fetched:
        return rest.Fetched(_chart_body(), etag='"chart"')

    us = ConnectorConfig(youtube_api_mode="most_popular", media_country="US")
    page = await collect_youtube_api(us, "mock-key", NOW, fetch, None)
    heads: list[object] = []

    async def spy(url: str, **kwargs: object) -> rest.Fetched:
        heads.append(kwargs["headers"].get("If-None-Match"))  # type: ignore[attr-defined]
        return rest.Fetched(_chart_body(), etag='"other"')

    gb = ConnectorConfig(youtube_api_mode="most_popular", media_country="GB")
    await collect_youtube_api(gb, "mock-key", NOW, spy, page.cursor_after)
    assert heads == [None]


@pytest.mark.parametrize(
    "options",
    [
        {"youtube_api_mode": "most_popular", "youtube_handles": ["@example"]},
        {"youtube_api_mode": "most_popular", "youtube_filter": "shorts"},
        {"youtube_api_mode": "most_popular", "media_countries": ["VN"]},
        {"youtube_video_category_id": "10", "youtube_handles": ["@example"]},
        {"media_country": "VN", "youtube_handles": ["@example"]},
        {"youtube_api_mode": "uploads", "media_country": "VN", "youtube_handles": ["@example"]},
    ],
)
def test_youtube_api_mode_rejects_incompatible_options(options: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        validate_options("youtube_api", options)


def test_youtube_api_mode_accepts_chart_and_legacy_uploads() -> None:
    validate_options(
        "youtube_api",
        {
            "youtube_api_mode": "most_popular",
            "media_country": "VN",
            "youtube_video_category_id": "1",
        },
    )
    validate_options("youtube_api", {"youtube_handles": ["@example"]})


@pytest.mark.parametrize("bad", ["", "abc", "12345", "-1"])
def test_category_id_pattern_is_bounded_digits(bad: str) -> None:
    with pytest.raises(ValueError):
        ConnectorConfig(youtube_video_category_id=bad)


def test_mode_category_and_region_are_optional_identity_scope() -> None:
    scope = PROVIDER_SCOPE_FIELDS["youtube_api"]
    chart = {"youtube_api_mode", "youtube_video_category_id", "media_country"}
    assert chart <= scope
    assert chart <= OPTIONAL_SCOPE_FIELDS["youtube_api"]
    assert not chart & PROVIDER_SCOPE_FIELDS["youtube"]
    base = {"youtube_api_mode": "most_popular", "media_country": "VN"}
    assert _scope_values("youtube_api", sorted(scope), base) is not None
    assert _scope_values("youtube_api", sorted(scope), base) != _scope_values(
        "youtube_api", sorted(scope), {**base, "media_country": "US"}
    )
    # A legacy uploads source with only a target keeps its scope; with nothing at all it fails closed.
    assert _scope_values("youtube_api", sorted(scope), {"youtube_handles": ["@example"]}) == {
        "youtube_handles": ["@example"]
    }
    assert _scope_values("youtube_api", sorted(scope), {}) is None


def test_registry_scope_rule_requires_target_only_in_uploads_mode() -> None:
    from modules.connectors.registry import _PROVIDER_SCOPE_RULES

    incomplete, _ = _PROVIDER_SCOPE_RULES["youtube_api"]
    assert incomplete(ConnectorConfig()) is True
    assert incomplete(ConnectorConfig(youtube_handles=("@example",))) is False
    assert incomplete(ConnectorConfig(youtube_api_mode="most_popular")) is False


def test_chart_and_rating_change_the_collector_fingerprint() -> None:
    base = ConnectorConfig(youtube_api_mode="most_popular", media_country="US")
    assert youtube_query_fingerprint(base) != youtube_query_fingerprint(
        ConnectorConfig(youtube_api_mode="most_popular", media_country="GB")
    )
    assert youtube_query_fingerprint(
        ConnectorConfig(youtube_handles=("@example",))
    ) != youtube_query_fingerprint(ConnectorConfig(youtube_handles=("@example",), min_rating=80))


def _atom(rating: str | None) -> bytes:
    media = (
        "<media:group><media:community>"
        f'<media:starRating count="9" average="{rating}" min="1" max="5"/>'
        "</media:community></media:group>"
        if rating is not None
        else ""
    )
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
        'xmlns:media="http://search.yahoo.com/mrss/"><entry>'
        f"<yt:videoId>{VIDEO}</yt:videoId><yt:channelId>{CHANNEL}</yt:channelId><title>T</title>"
        f"<published>2026-01-01T00:00:00+00:00</published>{media}</entry></feed>"
    ).encode()


def _atom_records(rating: str | None, min_rating: float | None) -> list:
    config: dict[str, object] = {"youtube_channel_id": CHANNEL}
    if min_rating is not None:
        config["min_rating"] = min_rating
    source = SimpleNamespace(provider="youtube", configuration=config)
    return list(_records_from_feed(source, _atom(rating), NOW).records)  # type: ignore[arg-type]


def test_atom_star_rating_is_scaled_to_percent_in_source_fields() -> None:
    (record,) = _atom_records("4.5", None)
    assert record.metadata["provider_record"]["source_fields"]["rating"] == 90.0


@pytest.mark.parametrize(
    ("rating", "min_rating", "kept"),
    [("4.5", 80, 1), ("3.0", 80, 0), (None, 80, 1), ("bogus", 80, 1), ("4.5", None, 1)],
)
def test_atom_min_rating_filters_known_low_ratings_and_keeps_missing(
    rating: str | None, min_rating: float | None, kept: int
) -> None:
    assert len(_atom_records(rating, min_rating)) == kept


def test_youtube_min_rating_is_an_atom_option_field() -> None:
    from modules.connectors.provider_options import OPTION_FIELDS

    assert OPTION_FIELDS["youtube"] == ("min_rating",)
    validate_options("youtube", {"min_rating": 80})
    with pytest.raises(ValueError):
        validate_options("youtube", {"min_rating": 101})
