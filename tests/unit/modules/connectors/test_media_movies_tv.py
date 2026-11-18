"""TVMaze schedule mapping and upstream scope tests without network access."""

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest

from modules.connectors.provider_options import native_fingerprint, validate_options
from modules.connectors.providers.keyed import KEYED
from modules.connectors.providers.media_movies_tv import (
    build_omdb_request,
    build_trakt_request,
    collect_tvmaze_followed_shows,
    collect_tvmaze_schedule,
    map_omdb,
    map_tmdb,
    map_trakt,
    map_tvmaze_schedule,
    tvmaze_schedule_url,
    tvmaze_show_url,
)
from modules.connectors.providers.rest import Fetched, ProviderHttpError


def test_tvmaze_url_preserves_global_web_scope_and_rejects_uk() -> None:
    url = tvmaze_schedule_url(country="", date="2026-11-18", web=True)
    assert urlsplit(url).path == "/schedule/web"
    assert parse_qs(urlsplit(url).query, keep_blank_values=True)["country"] == [""]
    assert "country=GB" in tvmaze_schedule_url(country="GB", date="2026-11-18")
    with pytest.raises(ValueError):
        tvmaze_schedule_url(country="UK", date="2026-11-18")


def test_tvmaze_maps_future_episode_and_applies_filters() -> None:
    row = {
        "id": 123,
        "name": "Pilot",
        "season": 1,
        "number": 2,
        "airstamp": "2026-11-18T20:00:00+00:00",
        "summary": "<p>Hello</p>",
        "show": {
            "id": 99,
            "name": "Series",
            "type": "Scripted",
            "language": "English",
            "genres": ["Drama"],
            "rating": {"average": 8.5},
            "network": {"country": {"code": "GB"}},
        },
    }
    now = datetime(2026, 11, 17, tzinfo=UTC)
    records = map_tvmaze_schedule(
        [row], collected_at=now, config={"media_genres": ["drama"], "min_rating": 80}
    )
    assert len(records) == 1
    record = records[0]
    fields = record.metadata["provider_record"]["source_fields"]
    assert record.provider_id == "tvmaze:123" and record.observed_at == now
    assert fields["country"] == "GB" and fields["language"] == record.metadata["language"] == "en"
    assert fields["rating"] == 85 and fields["format"] == "scripted"
    assert fields["release_status"] == "upcoming" and "TVMaze" in fields["attribution"]
    assert fields["release_date"] == "2026-11-18T20:00:00Z"
    assert "<p>" not in record.content
    assert map_tvmaze_schedule([row], collected_at=now, config={"media_languages": ["ja"]}) == []
    assert len(map_tvmaze_schedule([row, row], collected_at=now, config={})) == 1
    with pytest.raises(ValueError):
        map_tvmaze_schedule([row] * 501, collected_at=now, config={})


@pytest.mark.asyncio
async def test_tvmaze_uses_injected_transport_and_conditional_304() -> None:
    fetch = AsyncMock(return_value=Fetched(body=None))
    params = dict(
        country="GB",
        date="2026-11-18",
        web=False,
        config={},
        collected_at=datetime(2026, 11, 17, tzinfo=UTC),
    )
    page = await collect_tvmaze_schedule(fetch, **params, etag='"v1"')
    assert page.not_modified and page.records == () and page.etag == '"v1"'
    assert fetch.await_args.kwargs["headers"]["If-None-Match"] == '"v1"'
    assert fetch.await_args.kwargs["max_bytes"] == 2 * 1024 * 1024
    with pytest.raises(ProviderHttpError):
        await collect_tvmaze_schedule(fetch, **params)
    with pytest.raises(ProviderHttpError):
        await collect_tvmaze_schedule(fetch, **params, etag="unsafe\r\nheader")
    assert "If-None-Match" not in fetch.await_args.kwargs["headers"]


@pytest.mark.asyncio
async def test_tvmaze_success_captures_only_safe_response_validators() -> None:
    body = json.dumps(
        [{"id": 1, "airdate": "2026-11-18", "show": {"id": 2, "name": "Show"}}]
    ).encode()
    fetch = AsyncMock(return_value=Fetched(body=body, etag='"v2"', last_modified="bad\r\nvalue"))
    page = await collect_tvmaze_schedule(
        fetch,
        country=None,
        date="2026-11-18",
        web=True,
        config={},
        collected_at=datetime(2026, 11, 17, tzinfo=UTC),
    )
    assert not page.not_modified and len(page.records) == 1
    assert page.etag == '"v2"' and page.last_modified is None


@pytest.mark.asyncio
async def test_runner_bounds_validators_to_url_and_clears_them_for_backlog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.connectors._collection import providers

    url = tvmaze_schedule_url(country="GB", date=datetime.now(UTC).date().isoformat())
    fingerprint = sha256(url.encode()).hexdigest()
    state = SimpleNamespace(
        continuation_state=fingerprint, validators_revision=3, etag='"old"', last_modified=None
    )
    fetch = AsyncMock(return_value=Fetched(body=b"[]", etag='"new"'))
    run = SimpleNamespace(
        attempt=SimpleNamespace(
            source=SimpleNamespace(configuration={"media_country": "GB"}), connector_revision=3
        ),
        gate=SimpleNamespace(fetch_bytes=fetch),
        extras={},
    )
    accept = AsyncMock()
    monkeypatch.setattr(providers, "_state", AsyncMock(return_value=state))
    monkeypatch.setattr(providers, "_lease", AsyncMock(return_value=object()))
    monkeypatch.setattr(providers, "_accept_native", accept)

    async def select_with_backlog(*args: object) -> tuple[list[object], None]:
        run.extras["selection"] = {"backlog": 1}
        return [], None

    monkeypatch.setattr(providers, "_select", select_with_backlog)
    await providers._run_tvmaze(run)
    assert fetch.await_args.kwargs["headers"]["If-None-Match"] == '"old"'
    assert accept.await_args.args[5].update_validators and accept.await_args.args[5].etag is None
    assert run.extras["native_fp"] == fingerprint

    state.continuation_state = "different-url"
    await providers._run_tvmaze(run)
    assert "If-None-Match" not in fetch.await_args.kwargs["headers"]


def test_tmdb_keyed_movie_and_tv_urls_keep_provider_genre_ids_separate() -> None:
    today = datetime.now(UTC).date()
    year_end = datetime(today.year, 12, 31, tzinfo=UTC).date()
    cfg = {
        "tmdb_mode": "upcoming",
        "media_genres": ["action", "sci_fi"],
        "media_country": "US",
        "tmdb_language": "fr",
        "media_languages": ["ja"],
        "release_year": today.year,
        "min_rating": 80,
        "tmdb_certification": "PG-13",
        "tmdb_certification_country": "US",
        "exclude_adult": True,
    }
    movie = KEYED["tmdb_movies"].build("private-key", cfg)
    movie_url = urlsplit(movie.url)
    params = parse_qs(movie_url.query)
    assert movie_url.scheme == "https" and movie_url.netloc == "api.themoviedb.org"
    assert movie_url.path == "/3/discover/movie"
    assert params["api_key"] == ["private-key"]
    assert params["primary_release_date.gte"] == [today.isoformat()]
    assert params["primary_release_date.lte"] == [
        min(today + timedelta(days=180), year_end).isoformat()
    ]
    assert params["sort_by"] == ["primary_release_date.asc"]
    assert params["with_release_type"] == ["2|3"]
    assert params["with_genres"] == ["28|878"]
    assert params["with_origin_country"] == ["US"]
    assert params["with_original_language"] == ["ja"]
    assert params["language"] == ["fr"] and params["region"] == ["US"]
    assert params["certification.lte"] == ["PG-13"] and params["certification_country"] == ["US"]
    assert params["vote_average.gte"] == ["8"] and params["include_adult"] == ["false"]
    assert "private-key" not in repr(movie)

    tv = KEYED["tmdb_tv"].build("private-key", {**cfg, "tmdb_mode": "airing_today"})
    tv_params = parse_qs(urlsplit(tv.url).query)
    assert urlsplit(tv.url).path == "/3/discover/tv"
    assert tv_params["air_date.gte"] == tv_params["air_date.lte"] == [today.isoformat()]
    assert tv_params["with_genres"] == ["10759|10765"]
    assert "28" not in tv_params["with_genres"][0] and "878" not in tv_params["with_genres"][0]
    assert KEYED["tmdb_movies"].placement == KEYED["tmdb_tv"].placement == "query:api_key"


def test_tmdb_certification_uses_one_discover_request_and_rejects_changes_mode() -> None:
    today = datetime.now(UTC).date()
    request = KEYED["tmdb_movies"].build(
        "private-key",
        {
            "tmdb_mode": "now_playing",
            "tmdb_certification": "PG-13",
            "tmdb_certification_country": "US",
        },
    )
    parsed = urlsplit(request.url)
    params = parse_qs(parsed.query)
    assert parsed.path == "/3/discover/movie"
    assert params["certification.lte"] == ["PG-13"]
    assert params["certification_country"] == ["US"]
    assert params["with_release_type"] == ["2|3"]
    assert params["primary_release_date.lte"] == [today.isoformat()]
    assert (
        today - timedelta(days=60)
        <= datetime.fromisoformat(params["primary_release_date.gte"][0]).date()
        <= today
    )
    assert params["sort_by"] == ["primary_release_date.desc"]

    with pytest.raises(ValueError, match="certification is unavailable in changes mode"):
        KEYED["tmdb_movies"].build(
            "private-key",
            {
                "tmdb_mode": "changes",
                "tmdb_certification": "PG-13",
                "tmdb_certification_country": "US",
            },
        )


def test_tmdb_normalizes_typed_media_and_rating() -> None:
    now = datetime(2026, 11, 18, tzinfo=UTC)
    row = {
        "id": 42,
        "title": "Example",
        "overview": "Summary",
        "release_date": "2026-12-01",
        "genre_ids": [28, 878],
        "origin_country": ["US"],
        "original_language": "en",
        "vote_average": 8.4,
        "poster_path": "/poster.jpg",
    }
    records = map_tmdb(
        {"results": [row]},
        now,
        {"media_genres": ["action"], "media_languages": ["en"], "min_rating": 80},
        "tmdb_movies",
    )
    assert len(records) == 1
    record = records[0]
    fields = record.metadata["provider_record"]["source_fields"]
    assert record.provider_id == "tmdb:42"
    assert fields["rating"] == 84 and fields["genres"] == ["action", "sci_fi"]
    assert fields["country"] == "US" and fields["language"] == "en"
    assert fields["release_year"] == 2026 and fields["release_status"] == "upcoming"
    assert fields["image_url"] == "https://image.tmdb.org/t/p/w500/poster.jpg"
    assert fields["attribution"] == "Metadata by TMDB"


def test_trakt_header_query_mapping_and_science_fiction_slug() -> None:
    cfg = {
        "trakt_mode": "movies_search",
        "trakt_query": "space opera",
        "media_countries": ["US", "GB"],
        "media_genres": ["sci_fi", "drama"],
        "media_languages": ["en"],
        "release_year": 2026,
        "min_rating": 80,
        "trakt_certifications": ["PG-13"],
    }
    request = build_trakt_request("private-key", cfg)
    parsed = urlsplit(request.url)
    params = parse_qs(parsed.query)
    assert parsed.path == "/search/movie"
    assert params["query"] == ["space opera"] and params["countries"] == ["GB,US"]
    assert params["genres"] == ["drama,science-fiction"]
    assert params["languages"] == ["en"] and params["years"] == ["2026"]
    assert params["ratings"] == ["80-100"] and params["certifications"] == ["PG-13"]
    assert request.headers["trakt-api-key"] == "private-key"
    assert request.headers["trakt-api-version"] == "2"
    assert "private-key" not in repr(request)

    record = map_trakt(
        [
            {
                "movie": {
                    "title": "Space",
                    "year": 2026,
                    "ids": {"trakt": 7, "tmdb": 42, "imdb": "tt1234567"},
                    "rating": 8.5,
                    "genres": ["science-fiction", "drama"],
                    "country": "US",
                    "language": "English",
                    "released": "2026-12-01",
                    "certification": "PG-13",
                }
            }
        ],
        datetime(2026, 11, 18, tzinfo=UTC),
        {"media_genres": ["sci_fi"], "min_rating": 80},
    )[0]
    fields = record.metadata["provider_record"]["source_fields"]
    assert record.provider_id == "trakt:7" and fields["rating"] == 85
    assert fields["genres"] == ["sci_fi", "drama"] and fields["content_rating"] == "pg_13"
    assert fields["external_ids"] == {"trakt": "7", "tmdb": "42", "imdb": "tt1234567"}


def test_omdb_key_placement_and_filters_are_applied_after_fetch() -> None:
    cfg = {
        "omdb_mode": "imdb_id",
        "omdb_imdb_id": "tt1234567",
        "omdb_type": "movie",
        "release_year": 2026,
    }
    request = build_omdb_request("private-key", cfg)
    params = parse_qs(urlsplit(request.url).query)
    assert urlsplit(request.url).netloc == "www.omdbapi.com"
    assert params == {
        "r": ["json"],
        "i": ["tt1234567"],
        "type": ["movie"],
        "y": ["2026"],
        "apikey": ["private-key"],
    }
    assert KEYED["omdb_lookup"].placement == "query:apikey"
    assert "private-key" not in repr(request)
    payload = {
        "Response": "True",
        "Title": "Example",
        "Year": "2026",
        "imdbID": "tt1234567",
        "Type": "movie",
        "Country": "USA, UK",
        "Language": "English, French",
        "Genre": "Drama, Comedy",
        "imdbRating": "8.4",
        "Rated": "PG-13",
    }
    records = map_omdb(
        payload,
        datetime(2026, 11, 18, tzinfo=UTC),
        {
            "media_country": "US",
            "media_genres": ["drama"],
            "media_languages": ["en"],
            "min_rating": 80,
        },
    )
    assert len(records) == 1
    fields = records[0].metadata["provider_record"]["source_fields"]
    assert records[0].provider_id == "imdb:tt1234567"
    assert fields["country"] == "US" and fields["language"] == "en"
    assert fields["genres"] == ["drama", "comedy"] and fields["rating"] == 84
    assert fields["content_rating"] == "pg_13" and fields["attribution"] == "Data via OMDb API"
    assert map_omdb(payload, datetime(2026, 11, 18, tzinfo=UTC), {"media_genres": ["horror"]}) == []


def test_media_provider_scope_validation_and_option_fingerprints() -> None:
    validate_options("tmdb_movies", {"tmdb_mode": "changes"})
    validate_options("tmdb_tv", {"tmdb_mode": "on_the_air"})
    with pytest.raises(ValueError, match="TMDB mode is unsupported"):
        validate_options("tmdb_movies", {"tmdb_mode": "airing_today"})
    with pytest.raises(ValueError, match="TMDB mode is unsupported"):
        validate_options("tmdb_tv", {"tmdb_mode": "upcoming"})
    with pytest.raises(ValueError, match="certification requires"):
        validate_options("tmdb_movies", {"tmdb_certification": "PG"})
    with pytest.raises(ValueError, match="certification is unavailable in changes mode"):
        validate_options(
            "tmdb_movies",
            {
                "tmdb_mode": "changes",
                "tmdb_certification": "PG",
                "tmdb_certification_country": "US",
            },
        )
    with pytest.raises(ValueError, match="Trakt search requires"):
        validate_options("trakt_media", {"trakt_mode": "movies_search"})
    with pytest.raises(ValueError, match="IMDb ID is invalid"):
        validate_options("omdb_lookup", {"omdb_mode": "imdb_id", "omdb_imdb_id": "invalid"})
    assert native_fingerprint("tmdb_movies", {"media_genres": ["action"]}) != native_fingerprint(
        "tmdb_movies", {"media_genres": ["drama"]}
    )
    assert native_fingerprint("omdb_lookup", {"omdb_title": "A"}) != native_fingerprint(
        "omdb_lookup", {"omdb_title": "B"}
    )


def test_tmdb_tv_uses_airing_today_when_the_mode_is_omitted():
    from urllib.parse import urlsplit

    from modules.connectors.providers.media_movies_tv import _tmdb_url

    assert urlsplit(_tmdb_url("tmdb_tv", {})).path == "/3/tv/airing_today"


def test_tmdb_filtered_noncertified_movies_use_discover_and_changes_has_a_14_day_window():
    query = KEYED["tmdb_movies"].build(
        "private-key", {"tmdb_mode": "now_playing", "min_rating": 70}
    )
    parts = urlsplit(query.url)
    assert parts.path == "/3/discover/movie"
    assert parse_qs(parts.query)["vote_average.gte"] == ["7"]
    outside = KEYED["tmdb_movies"].build(
        "private-key", {"tmdb_mode": "upcoming", "release_year": 1900}
    )
    params = parse_qs(urlsplit(outside.url).query)
    assert params["primary_release_year"] == ["1900"]
    assert params["primary_release_date.gte"] <= params["primary_release_date.lte"]
    changes = urlsplit(KEYED["tmdb_movies"].build("private-key", {"tmdb_mode": "changes"}).url)
    params = parse_qs(changes.query)
    assert changes.path == "/3/movie/changes"
    assert set(params) == {"start_date", "end_date", "page", "api_key"}
    assert datetime.fromisoformat(params["end_date"][0]).date() == datetime.now(UTC).date()
    assert (
        datetime.fromisoformat(params["end_date"][0])
        - datetime.fromisoformat(params["start_date"][0])
    ).days == 14
    with pytest.raises(ValueError, match="changes"):
        validate_options("tmdb_movies", {"tmdb_mode": "changes", "min_rating": 70})


def test_trakt_show_envelope_and_calendar_episodes_keep_distinct_ids_and_air_dates():
    show = {
        "title": "Show",
        "year": 2025,
        "ids": {"trakt": 10, "slug": "show"},
        "first_aired": "2025-01-01",
    }
    now = datetime(2026, 11, 18, tzinfo=UTC)
    assert (
        urlsplit(
            build_trakt_request(
                "private-key", {"trakt_mode": "shows_search", "trakt_query": "show"}
            ).url
        ).path
        == "/search/show"
    )
    (one,) = map_trakt([{"show": show}], now, {})
    assert one.metadata["provider_record"]["source_fields"]["media_type"] == "tv"
    rows = [
        {
            "show": show,
            "first_aired": date,
            "episode": {"ids": {"trakt": num}, "season": 2, "number": num, "title": f"Part {num}"},
        }
        for num, date in ((1, "2026-11-20"), (2, "2026-11-27"))
    ]
    records = map_trakt(rows, now, {"trakt_mode": "shows_calendar"})
    assert [item.provider_id for item in records] == ["trakt:episode:1", "trakt:episode:2"]
    for item, row in zip(records, rows, strict=True):
        fields = item.metadata["provider_record"]["source_fields"]
        assert fields["media_type"] == "tv"
        assert fields["release_date"] == row["first_aired"]
        assert fields["season"] == "2" and fields["episode"] == str(row["episode"]["number"])


def test_omdb_episode_is_tv_and_search_disallows_detail_only_filters():
    mode = {
        "omdb_mode": "search",
        "omdb_search": "Example",
        "omdb_type": "episode",
        "release_year": 2026,
    }
    row = {"Title": "Episode", "Year": "2026", "imdbID": "tt1234567", "Type": "episode"}
    (record,) = map_omdb(
        {"Response": "True", "Search": [row]}, datetime(2026, 11, 18, tzinfo=UTC), mode
    )
    fields = record.metadata["provider_record"]["source_fields"]
    assert fields["media_type"] == "tv" and fields["format"] == "episodic"
    for field, value in (
        ("media_country", "US"),
        ("media_countries", ["US"]),
        ("media_genres", ["drama"]),
        ("media_languages", ["en"]),
        ("min_rating", 80),
    ):
        with pytest.raises(ValueError, match="search"):
            validate_options("omdb_lookup", mode | {field: value})
        with pytest.raises(ValueError, match="search"):
            build_omdb_request("private-key", mode | {field: value})


@pytest.mark.asyncio
async def test_tmdb_changes_runner_drains_pinned_pages_and_uses_one_request_per_run(monkeypatch):
    from modules.connectors._collection import providers

    now = datetime(2026, 11, 18, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(providers, "datetime", Clock)
    lease = SimpleNamespace(cursor_before=None)
    run = SimpleNamespace(
        attempt=SimpleNamespace(
            source=SimpleNamespace(provider="tmdb_movies", configuration={"tmdb_mode": "changes"})
        ),
        extras={},
    )
    queries = []
    accepted = []

    async def fetch(_run, request, **kwargs):
        query = parse_qs(urlsplit(request.url).query)
        queries.append(query)
        page = int(query["page"][0])
        rows = [{"id": 1}, {"id": 2}] if page == 1 else [{"id": 3}]
        return Fetched(json.dumps({"page": page, "total_pages": 2, "results": rows}).encode())

    async def select(_run, _lease, records, collected_at):
        if not accepted:
            run.extras["selection"] = {"backlog": 1}
            return records[:1], None
        run.extras["selection"] = {"backlog": 0}
        return records[1:] if len(records) == 2 else records, None

    async def accept(_run, _lease, records, coverage, collected_at, **kwargs):
        accepted.append((records, kwargs["cursor_after"]))
        lease.cursor_before = kwargs["cursor_after"]

    monkeypatch.setattr(providers, "_keyed_secret", AsyncMock(return_value="private-key"))
    monkeypatch.setattr(providers, "_lease", AsyncMock(return_value=lease))
    monkeypatch.setattr(
        providers, "_resume_cursor", AsyncMock(side_effect=lambda *_: lease.cursor_before)
    )
    monkeypatch.setattr(providers, "_get_body", fetch)
    monkeypatch.setattr(providers, "_select", select)
    monkeypatch.setattr(providers, "_accept_native", accept)

    for _ in range(3):
        await providers._run_keyed(run)
        now += timedelta(days=1)
    assert [query["page"][0] for query in queries] == ["1", "1", "2"]
    assert {query["start_date"][0] for query in queries} == {"2026-11-04"}
    assert {query["end_date"][0] for query in queries} == {"2026-11-18"}
    assert [records[0].provider_id for records, _ in accepted] == ["tmdb:1", "tmdb:2", "tmdb:3"]

    await providers._run_keyed(run)
    assert queries[-1]["page"] == ["1"]
    assert queries[-1]["start_date"] == ["2026-11-18"]
    assert queries[-1]["end_date"] == ["2026-11-21"]


def test_tmdb_discover_retains_abbreviated_country_matches_without_synthesizing_country():
    opts = {"media_countries": ["VN"], "min_rating": 70}
    req = KEYED["tmdb_movies"].build("private-key", opts)
    assert parse_qs(urlsplit(req.url).query)["with_origin_country"] == ["VN"]
    records = map_tmdb(
        {
            "results": [
                {"id": 1, "title": "Abbreviated", "vote_average": 7.5},
                {"id": 2, "title": "Too low", "vote_average": 6.0},
                {
                    "id": 3,
                    "title": "Different origin",
                    "vote_average": 8.0,
                    "origin_country": ["US"],
                },
            ]
        },
        datetime(2026, 11, 18, tzinfo=UTC),
        opts,
        "tmdb_movies",
    )
    (record,) = records
    assert record.provider_id == "tmdb:1"
    assert "country" not in record.metadata["provider_record"]["source_fields"]


def _followed_body(show_id: int, prev: str | None, nxt: str | None) -> bytes:
    """Fixture: TVMaze show with embedded previous/next episodes (airstamps given or absent)."""
    embedded = {}
    if prev:
        embedded["previousepisode"] = {"id": show_id * 10, "name": "Prev", "airstamp": prev}
    if nxt:
        embedded["nextepisode"] = {"id": show_id * 10 + 1, "name": "Next", "airstamp": nxt}
    return json.dumps({"id": show_id, "name": f"Show {show_id}", "_embedded": embedded}).encode()


@pytest.mark.asyncio
async def test_tvmaze_followed_shows_one_gated_call_each_window_and_404_skip() -> None:
    now = datetime(2026, 11, 18, tzinfo=UTC)
    bodies = {
        1: _followed_body(1, "2026-11-10T20:00:00+00:00", "2026-11-25T20:00:00+00:00"),
        2: _followed_body(2, "2026-01-01T20:00:00+00:00", "2027-06-01T20:00:00+00:00"),
    }

    async def fetch(url: str, **_: object) -> Fetched:
        show_id = int(urlsplit(url).path.rsplit("/", 1)[1])
        if show_id == 3:
            raise ProviderHttpError(404)
        return Fetched(body=bodies[show_id])

    gate = AsyncMock(side_effect=fetch)
    records = await collect_tvmaze_followed_shows(
        gate, show_ids=(2, 1, 3, 1), config={}, collected_at=now
    )
    assert gate.await_count == 3  # duplicates collapse; every distinct id is one gated call
    assert sorted(r.provider_id for r in records) == ["tvmaze:10", "tvmaze:11"]
    assert (
        next(r for r in records if r.provider_id == "tvmaze:11").metadata["provider_record"][
            "source_fields"
        ]["release_status"]
        == "upcoming"
    )
    gate.side_effect = ProviderHttpError(429)
    with pytest.raises(ProviderHttpError):
        await collect_tvmaze_followed_shows(gate, show_ids=(1,), config={}, collected_at=now)
    with pytest.raises(ValueError):
        await collect_tvmaze_followed_shows(
            gate, show_ids=tuple(range(1, 22)), config={}, collected_at=now
        )
    assert "shows/7?" in tvmaze_show_url(7)
    with pytest.raises(ValueError):
        tvmaze_show_url(0)


def test_tvmaze_followed_mode_validation() -> None:
    validate_options(
        "tvmaze_schedule", {"tvmaze_mode": "followed_shows", "tvmaze_show_ids": (1, 2)}
    )
    for config in (
        {"tvmaze_mode": "followed_shows"},
        {"tvmaze_mode": "schedule_web", "tvmaze_show_ids": (1,)},
        {"tvmaze_mode": "followed_shows", "tvmaze_show_ids": (1,), "tvmaze_global": True},
    ):
        with pytest.raises(ValueError):
            validate_options("tvmaze_schedule", config)


@pytest.mark.asyncio
async def test_runner_followed_shows_accepts_union_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.connectors._collection import providers

    soon = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    fetch = AsyncMock(return_value=Fetched(body=_followed_body(5, None, soon)))
    cfg = {"tvmaze_mode": "followed_shows", "tvmaze_show_ids": [5]}
    run = SimpleNamespace(
        attempt=SimpleNamespace(source=SimpleNamespace(configuration=cfg), connector_revision=1),
        gate=SimpleNamespace(fetch_bytes=fetch),
        extras={},
    )
    accept = AsyncMock()
    monkeypatch.setattr(providers, "_lease", AsyncMock(return_value=object()))
    monkeypatch.setattr(providers, "_accept_native", accept)
    monkeypatch.setattr(
        providers, "_select", AsyncMock(side_effect=lambda r, lease, recs, now: (list(recs), None))
    )
    await providers._run_tvmaze(run)
    assert fetch.await_count == 1 and len(accept.await_args.args[2]) == 1
    assert run.extras["native_fp"].startswith("media-v1:")
