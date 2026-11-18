"""Offline contract checks for the AniList GraphQL collector."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from modules.connectors.providers.anilist import (
    AIRING_WINDOW,
    MAX_PAGES_PER_RUN,
    MAX_RESULTS,
    anilist_query_fingerprint,
    build_anilist_request,
    collect_anilist,
    map_anilist_response,
    parse_anilist_cursor,
)
from modules.connectors.providers.rest import Fetched


def _media(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": 123,
        "idMal": 456,
        "type": "MANGA",
        "format": "MANGA",
        "status": "RELEASING",
        "seasonYear": 2024,
        "season": "FALL",
        "countryOfOrigin": "KR",
        "isAdult": False,
        "averageScore": 87,
        "genres": ["Action", "Comedy"],
        "tags": [],
        "updatedAt": 1767225600,
        "description": "A test title",
        "siteUrl": "https://anilist.co/manga/123",
        "title": {"romaji": "Title", "english": None},
        "startDate": {"year": 2024, "month": 2, "day": 3},
        "coverImage": {"large": None},
        "airingSchedule": {"nodes": []},
    }
    row.update(overrides)
    return row


def _payload(rows: list[dict[str, object]], *, has_next_page: bool = False) -> dict[str, object]:
    return {"data": {"Page": {"media": rows, "pageInfo": {"hasNextPage": has_next_page}}}}


def test_request_pushes_filters_and_excludes_adult_by_default() -> None:
    query = json.loads(
        build_anilist_request(
            "anilist_anime",
            {
                "media_country": "JP",
                "media_genres": ["action", "sci_fi"],
                "media_formats": ["tv", "movie"],
                "anime_season": "winter",
                "release_year": 2025,
                "media_statuses": ["releasing"],
                "min_rating": 80,
                "media_tags": ["Space Opera"],
                "media_ids": [7, 8],
            },
        )
    )
    assert query["variables"] == {
        "page": 1,
        "perPage": MAX_RESULTS,
        "type": "ANIME",
        "ids": [7, 8],
        "country": "JP",
        "genres": ["Action", "Sci-Fi"],
        "formats": ["TV", "MOVIE"],
        "season": "WINTER",
        "tags": ["Space Opera"],
        "seasonYear": 2025,
        "status": ["RELEASING"],
        "isAdult": False,
        "score": 80,
        "sort": ["ID"],
        "updatedAfter": None,
    }


def test_mode_sort_adult_opt_in_and_tag_pushdown() -> None:
    trending = json.loads(
        build_anilist_request(
            "anilist_manga",
            {
                "anilist_mode": "trending",
                "country_of_origin": "ALL",
                "exclude_adult": False,
            },
            page=3,
        )
    )["variables"]
    assert trending["sort"] == ["TRENDING_DESC"]
    assert trending["page"] == 3
    assert trending["country"] is None
    assert trending["isAdult"] is None
    airing = json.loads(build_anilist_request("anilist_anime", {"anilist_mode": "airing"}))[
        "variables"
    ]
    assert airing["sort"] == ["ID"]
    assert airing["status"] == ["RELEASING"]
    assert "airingSchedule" in json.loads(build_anilist_request("anilist_anime", {}))["query"]
    assert (
        "updatedAt_greater"
        in json.loads(build_anilist_request("anilist_anime", {}, updated_after=100))["query"]
    )
    assert (
        "updatedAt_greater"
        in json.loads(build_anilist_request("anilist_anime", {}, updated_after=100))["query"]
    )


def test_map_manga_normalizes_attribution_identity_and_dimensions() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    rows = map_anilist_response("anilist_manga", _payload([_media()]), collected_at=now, config={})
    record = rows[0]
    fields = record.metadata["provider_record"]["source_fields"]
    assert record.provider_id == "anilist_manga:123"
    assert fields["media_type"] == "manhwa"
    assert fields["country"] == "KR"
    assert fields["release_date"] == "2024-02-03"
    assert fields["release_season"] == "fall"
    assert fields["rating"] == 87.0
    assert fields["content_rating"] == "clean"
    assert fields["external_ids"] == {"anilist": "123", "mal": "456"}
    assert fields["attribution"] == "Data via AniList"


def test_map_applies_post_fetch_country_and_rating_filters() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    payload = _payload([_media(), _media(id=124, countryOfOrigin="JP", averageScore=60)])
    records = map_anilist_response(
        "anilist_manga",
        payload,
        collected_at=now,
        config={"media_countries": ["KR"], "min_rating": 80},
    )
    assert [record.provider_id for record in records] == ["anilist_manga:123"]


def test_map_postfilters_tag_and_adult_opt_in() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    adult = _media(id=129, isAdult=True, tags=[{"name": "Space Opera"}])
    other = _media(id=130, tags=[{"name": "Comedy"}])
    rows = map_anilist_response(
        "anilist_manga",
        _payload([adult, other]),
        collected_at=now,
        config={"media_tags": ["space opera"], "exclude_adult": False},
    )
    assert [record.provider_id for record in rows] == ["anilist_manga:129"]
    assert rows[0].metadata["provider_record"]["source_fields"]["content_rating"] == "explicit"


@pytest.mark.asyncio
async def test_collector_uses_gated_json_post_and_bounded_response() -> None:
    sent: dict[str, object] = {}

    async def fetch_bytes(url: str, **kwargs: object) -> Fetched:
        sent.update(url=url, **kwargs)
        return Fetched(json.dumps(_payload([_media()], has_next_page=True)).encode())

    page = await collect_anilist(
        fetch_bytes, "anilist_manga", config={}, collected_at=datetime.now(UTC), page=1
    )
    assert sent["url"] == "https://graphql.anilist.co"
    assert json.loads(sent["json_body"])["variables"]["isAdult"] is False
    assert sent["max_bytes"] == 2 * 1024 * 1024
    assert sent["timeout"] == 6
    assert page.records[0].provider_id == "anilist_manga:123"
    assert page.has_next_page


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": {"Page": {"media": "bad"}}},
        {"data": {"Page": {"media": [{}] * (MAX_RESULTS + 1)}}},
    ],
)
def test_map_rejects_invalid_or_oversized_pages(payload: object) -> None:
    with pytest.raises(ValueError, match="anilist_response_invalid"):
        map_anilist_response("anilist_anime", payload, collected_at=datetime.now(UTC), config={})


def test_airing_emits_stable_episode_identity_with_bounded_date() -> None:
    now = datetime.now(UTC)
    airing_at = int((now + AIRING_WINDOW / 2).timestamp())
    row = _media(
        id=777,
        title={"english": "Example"},
        airingSchedule={"nodes": [{"airingAt": airing_at, "episode": 3}]},
    )
    record = map_anilist_response(
        "anilist_anime", _payload([row]), collected_at=now, config={"anilist_mode": "airing"}
    )[0]
    source_fields = record.metadata["provider_record"]["source_fields"]
    assert record.provider_id == "anilist_anime:episode:3:777"
    assert source_fields["title"] == "Example — Episode 3"
    assert source_fields["episode"] == "3"
    assert "release_status" not in source_fields


def test_cursor_and_native_query_fingerprint_reset_and_advance() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    config = {"media_genres": ["action"], "anilist_mode": "updated"}
    fingerprint = anilist_query_fingerprint("anilist_anime", config)
    page, watermark = parse_anilist_cursor(
        "11:1767225600", fingerprint_matches=True, collected_at=now
    )
    assert (page, watermark) == (11, 1767225600)
    assert (
        parse_anilist_cursor("11:1767225600", fingerprint_matches=False, collected_at=now)[0] == 1
    )
    assert (
        anilist_query_fingerprint("anilist_anime", {**config, "media_tags": ["mystery"]})
        != fingerprint
    )
    assert MAX_PAGES_PER_RUN == 10


def test_media_ids_are_optional_provider_scope_fields() -> None:
    from uuid import uuid4

    from modules.connectors.backends import PROVIDER_SCOPE_FIELDS
    from modules.connectors.provider_specs import BUILTIN_PROVIDERS
    from modules.connectors.registry import _check_provider_keys
    from modules.connectors.service.scope import OPTIONAL_SCOPE_FIELDS
    from modules.sources.schemas import ConnectorSource

    assert PROVIDER_SCOPE_FIELDS["anilist_anime"] == frozenset({"media_ids"})
    assert OPTIONAL_SCOPE_FIELDS["anilist_anime"] == frozenset({"media_ids"})
    assert "anilist_anime" not in BUILTIN_PROVIDERS
    source = ConnectorSource(
        id=uuid4(),
        workspace_id=uuid4(),
        type="api",
        status="active",
        generation=1,
        configuration={"history_mode": "returned_snapshot", "media_ids": [1, 2]},
        local_only=False,
        provider="anilist_anime",
    )
    _check_provider_keys(source, "anilist_anime")


@pytest.mark.asyncio
async def test_runner_accepts_bounded_pages_before_advancing_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.connectors._collection import providers as collection_providers
    from modules.connectors.providers import anilist
    from modules.connectors.providers.anilist import AniListPage

    config = {"anilist_mode": "updated"}
    fingerprint = anilist_query_fingerprint("anilist_anime", config)
    run = SimpleNamespace(
        attempt=SimpleNamespace(
            source=SimpleNamespace(provider="anilist_anime", configuration=config)
        ),
        extras={},
        gate=SimpleNamespace(fetch_bytes=object()),
    )
    lease = SimpleNamespace(cursor_before="1:1767225600")
    calls: list[tuple[int, int | None]] = []
    accepted: dict[str, object] = {}

    class NoWait:
        async def pace(self, key: str, interval: float) -> float:
            return 0.0

    async def collect(
        _fetch: object, _provider: str, *, page: int, updated_after: int | None, **_kwargs: object
    ) -> AniListPage:
        calls.append((page, updated_after))
        return AniListPage((), page == 1, MAX_RESULTS if page == 1 else 2)

    async def accept(*_args: object, **kwargs: object) -> None:
        accepted.update(kwargs)

    async def state(_run: object) -> object:
        return SimpleNamespace(continuation_state=fingerprint)

    monkeypatch.setattr(collection_providers, "_ANILIST_PACER", NoWait())
    monkeypatch.setattr(collection_providers, "_lease", lambda _run: _resolved(lease))
    monkeypatch.setattr(collection_providers, "_state", state)
    monkeypatch.setattr(collection_providers, "_accept_native", accept)
    monkeypatch.setattr(anilist, "collect_anilist", collect)
    await collection_providers._run_anilist(run)
    assert calls == [(1, 1767225600), (2, 1767225600)]
    assert accepted["cursor_after"] == "1:0"


async def _run_with_pages(
    monkeypatch: pytest.MonkeyPatch, collect: object
) -> tuple[dict[str, object], list[int]]:
    """Drive ``_run_anilist`` from page 1 with a stub collector; return accepted kwargs and pages seen."""
    from modules.connectors._collection import providers as collection_providers
    from modules.connectors.providers import anilist

    config = {"anilist_mode": "updated"}
    fingerprint = anilist_query_fingerprint("anilist_anime", config)
    run = SimpleNamespace(
        attempt=SimpleNamespace(
            source=SimpleNamespace(provider="anilist_anime", configuration=config)
        ),
        extras={},
        gate=SimpleNamespace(fetch_bytes=object()),
    )
    accepted: dict[str, object] = {}
    pages: list[int] = []

    class NoWait:
        async def pace(self, key: str, interval: float) -> float:
            return 0.0

    async def wrapped(_fetch: object, _provider: str, *, page: int, **kwargs: object) -> object:
        pages.append(page)
        return await collect(page)  # type: ignore[operator]

    async def accept(*_args: object, **kwargs: object) -> None:
        accepted.update(kwargs)

    async def state(_run: object) -> object:
        return SimpleNamespace(continuation_state=fingerprint)

    lease = SimpleNamespace(cursor_before="1:1767225600")
    monkeypatch.setattr(collection_providers, "_ANILIST_PACER", NoWait())
    monkeypatch.setattr(collection_providers, "_lease", lambda _run: _resolved(lease))
    monkeypatch.setattr(collection_providers, "_state", state)
    monkeypatch.setattr(collection_providers, "_accept_native", accept)
    monkeypatch.setattr(anilist, "collect_anilist", wrapped)
    await collection_providers._run_anilist(run)
    return accepted, pages


@pytest.mark.asyncio
async def test_runner_stops_and_checkpoints_when_rate_limit_budget_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.connectors.providers.anilist import AniListPage

    async def collect(page: int) -> AniListPage:
        return AniListPage((), True, MAX_RESULTS, rate_limit_remaining=0)

    accepted, pages = await _run_with_pages(monkeypatch, collect)
    assert pages == [1]
    assert accepted["cursor_after"] == "2:1767225600"


@pytest.mark.asyncio
async def test_runner_keeps_paging_while_rate_limit_budget_remains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.connectors.providers.anilist import AniListPage

    async def collect(page: int) -> AniListPage:
        return AniListPage((), page == 1, MAX_RESULTS if page == 1 else 1, rate_limit_remaining=5)

    accepted, pages = await _run_with_pages(monkeypatch, collect)
    assert pages == [1, 2]
    assert accepted["cursor_after"] == "1:0"


@pytest.mark.asyncio
async def test_runner_propagates_429_retry_after_without_accepting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.connectors.public import ProviderRateLimited

    retry_at = datetime(2026, 1, 1, tzinfo=UTC)

    async def collect(page: int) -> object:
        raise ProviderRateLimited(retry_at)

    accepted: dict[str, object] = {}
    with pytest.raises(ProviderRateLimited) as caught:
        accepted, _ = await _run_with_pages(monkeypatch, collect)
    assert caught.value.next_eligible_at == retry_at
    assert accepted == {}


@pytest.mark.asyncio
async def test_collector_exposes_bounded_rate_limit_remaining_header() -> None:
    async def fetch_bytes(url: str, **kwargs: object) -> Fetched:
        return Fetched(json.dumps(_payload([_media()])).encode(), rate_limit_remaining=0)

    page = await collect_anilist(
        fetch_bytes, "anilist_manga", config={}, collected_at=datetime.now(UTC), page=1
    )
    assert page.rate_limit_remaining == 0


def test_rate_limit_header_parser_rejects_malformed_values() -> None:
    import httpx

    from modules.connectors.providers.rest import _rate_limit_remaining

    parse = lambda value: _rate_limit_remaining(httpx.Headers({"X-RateLimit-Remaining": value}))  # noqa: E731
    assert (parse("0"), parse(" 27 "), parse("-1"), parse("abc"), parse("1" * 7)) == (
        0,
        27,
        None,
        None,
        None,
    )
    assert _rate_limit_remaining(httpx.Headers()) is None


async def _resolved(value: object) -> object:
    """Return a value through an awaitable lambda used to replace the lease helper."""
    return value
