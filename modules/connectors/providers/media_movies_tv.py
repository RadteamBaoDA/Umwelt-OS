"""Bounded movie and television provider mappers plus TVMaze schedule collection."""

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from modules.connectors.provider_options import (
    TMDB_DISCOVER_OPTIONS,
    filter_media_records,
    validate_omdb_search_filters,
    validate_tmdb_options,
)
from modules.connectors.providers._media_common import (
    build_external_ids,
    build_media_identity,
    normalize_content_rating,
    normalize_country,
    normalize_cover_url,
    normalize_genres,
    normalize_language,
    normalize_partial_date,
    normalize_rating,
)
from modules.connectors.providers.feed_catalog import _plain_text
from modules.connectors.providers.macro import ProviderRequest
from modules.connectors.providers.media_movie_sources import _movie_record
from modules.connectors.providers.news import _header_value
from modules.connectors.providers.rest import Fetched, ProviderHttpError
from modules.ingestion.public import IngestionRecord
from modules.knowledge.documents.public import ProviderRecordMetadata

TMDB_BASE = "https://api.themoviedb.org/3"
TRAKT_BASE = "https://api.trakt.tv"
OMDB_BASE = "https://www.omdbapi.com/"


def _trakt_options(config: Mapping[str, Any], params: dict[str, str]) -> None:
    """Add Trakt's optional certification selector to the query parameter map."""
    certifications = config.get("trakt_certifications")
    if certifications:
        params["certifications"] = ",".join(sorted(certifications))


def _media_options(config: Mapping[str, Any], provider: str) -> dict[str, str]:
    """Map canonical media choices to provider query parameters.

    TMDB discover endpoints consume these filters. Trakt uses its own list and certification parameters.
    """
    params: dict[str, str] = {}
    countries = config.get("media_countries") or (
        () if not config.get("media_country") else (config["media_country"],)
    )
    if countries:
        params["with_origin_country" if provider.startswith("tmdb_") else "countries"] = (
            "|" if provider.startswith("tmdb_") else ","
        ).join(sorted(countries))
    if config.get("media_genres"):
        if provider.startswith("tmdb_"):
            from modules.connectors.providers._media_common import _PROVIDER_GENRE_IDS

            genre_map = _PROVIDER_GENRE_IDS[provider]
            ids = sorted(
                {
                    genre_id
                    for canonical in config["media_genres"]
                    for genre_id, mapped in genre_map.items()
                    if canonical in mapped
                },
                key=int,
            )
            if ids:
                params["with_genres"] = "|".join(ids)
        else:
            slugs = {"sci_fi": "science-fiction"}
            params["genres"] = ",".join(
                sorted(slugs.get(value, value) for value in config["media_genres"])
            )
    if config.get("media_languages"):
        key = "with_original_language" if provider.startswith("tmdb_") else "languages"
        params[key] = ("|" if provider.startswith("tmdb_") else ",").join(
            sorted(config["media_languages"])
        )
    if config.get("release_year") is not None:
        params[
            "primary_release_year"
            if provider == "tmdb_movies"
            else "first_air_date_year"
            if provider == "tmdb_tv"
            else "years"
        ] = str(config["release_year"])
        if provider == "tmdb_movies":
            params.update(
                {
                    "primary_release_date.gte": f"{config['release_year']}-01-01",
                    "primary_release_date.lte": f"{config['release_year']}-12-31",
                }
            )
        elif provider == "tmdb_tv":
            params.update(
                {
                    "first_air_date.gte": f"{config['release_year']}-01-01",
                    "first_air_date.lte": f"{config['release_year']}-12-31",
                }
            )
    if config.get("min_rating") is not None:
        params["vote_average.gte" if provider.startswith("tmdb_") else "ratings"] = (
            f"{float(config['min_rating']) / 10:g}"
            if provider.startswith("tmdb_")
            else f"{float(config['min_rating']):g}-100"
        )
    if provider == "tmdb_movies" and config.get("media_country"):
        params["region"] = config["media_country"]
    if provider.startswith("tmdb_") and config.get("tmdb_language"):
        params["language"] = config["tmdb_language"]
    if (
        provider == "tmdb_movies"
        and config.get("tmdb_certification")
        and config.get("tmdb_certification_country")
    ):
        params["certification.lte"] = config["tmdb_certification"]
        params["certification_country"] = config["tmdb_certification_country"]
    if provider.startswith("tmdb_"):
        params["include_adult"] = "true" if config.get("exclude_adult") is False else "false"
    if provider == "trakt_media":
        _trakt_options(config, params)
    return params


@dataclass(frozen=True)
class TmdbChangesWindow:
    """One bounded date interval and unread TMDB change-list page."""

    start: date
    end: date
    page: int


def tmdb_changes_window(cursor: str | None, now: datetime) -> TmdbChangesWindow:
    """Restore one unfinished window or start from the last completed end, with an inclusive overlap."""
    today = now.astimezone(UTC).date()
    fresh = TmdbChangesWindow(today - timedelta(days=14), today, 1)
    if not cursor or len(cursor) > 256:
        return fresh
    try:
        state = json.loads(cursor)
        if not isinstance(state, dict) or state.get("kind") != "tmdb_changes":
            return fresh
        start, end = date.fromisoformat(state["start"]), date.fromisoformat(state["end"])
        page = state["page"]
    except (KeyError, TypeError, ValueError):
        return fresh
    if not start <= end <= today or (end - start).days > 14:
        return fresh
    if page is None:
        # Stay anchored even after a lengthy backlog or downtime. Each upstream request still spans at most 14 days.
        return TmdbChangesWindow(end, min(end + timedelta(days=14), today), 1)
    return TmdbChangesWindow(start, end, page) if type(page) is int and 1 <= page <= 500 else fresh


def tmdb_changes_cursor(payload: object, window: TmdbChangesWindow, *, backlog: bool) -> str:
    """Checkpoint the next unread page, or retain this page while local record selection has a backlog."""
    if (
        not isinstance(payload, Mapping)
        or type(payload.get("page")) is not int
        or payload["page"] != window.page
    ):
        raise ValueError("tmdb_changes_page_invalid")
    total = payload.get("total_pages")
    if type(total) is not int or not 0 <= total <= 500:
        raise ValueError("tmdb_changes_page_invalid")
    if window.page > total and (window.page != 1 or total != 0 or payload.get("results") != []):
        raise ValueError("tmdb_changes_page_invalid")
    # Only a completely accepted page advances the remote page pointer.
    next_page = window.page if backlog else (window.page + 1 if window.page < total else None)
    return json.dumps(
        {
            "kind": "tmdb_changes",
            "start": window.start.isoformat(),
            "end": window.end.isoformat(),
            "page": next_page,
        },
        separators=(",", ":"),
    )


def _tmdb_url(
    provider: str,
    config: Mapping[str, Any],
    changes_window: TmdbChangesWindow | None = None,
) -> str:
    """Build one TMDB request with mode-appropriate filters and bounded, resumable UTC dates."""
    validate_tmdb_options(provider, config)
    is_movie = provider == "tmdb_movies"
    mode = config.get("tmdb_mode") or ("now_playing" if is_movie else "airing_today")
    today = datetime.now(UTC).date()
    if mode == "changes":
        window = changes_window or tmdb_changes_window(None, datetime.now(UTC))
        params = {
            "start_date": window.start.isoformat(),
            "end_date": window.end.isoformat(),
            "page": str(window.page),
        }
        return f"{TMDB_BASE}/movie/changes?{urlencode(params)}"
    if not any(config.get(name) is not None for name in TMDB_DISCOVER_OPTIONS):
        # Native lists implement the unfiltered preset. Only localization belongs on those endpoints.
        path = f"/{'movie' if is_movie else 'tv'}/{mode}"
        params = {"page": "1"}
        if language := config.get("tmdb_language"):
            params["language"] = language
        return f"{TMDB_BASE}{path}?{urlencode(params)}"

    params = _media_options(config, provider)
    if is_movie:
        params.update(_tmdb_movie_date_options(mode, config, today))
        if config.get("tmdb_certification"):
            params.setdefault("region", str(config["tmdb_certification_country"]))
    else:
        # Schedule by episode air date, while release_year remains the show's first-air year.
        end = today if mode == "airing_today" else today + timedelta(days=7)
        params.update({"air_date.gte": today.isoformat(), "air_date.lte": end.isoformat()})
    params["page"] = "1"
    return f"{TMDB_BASE}/discover/{'movie' if is_movie else 'tv'}?{urlencode(params)}"


def _tmdb_movie_date_options(
    mode: str,
    config: Mapping[str, Any],
    today: date,
) -> dict[str, str]:
    """Select a theatrical date range and intersect it with an optional primary-release year."""
    upcoming = mode == "upcoming"
    lower = today if upcoming else today - timedelta(days=60)
    upper = today + timedelta(days=180) if upcoming else today
    if (year := config.get("release_year")) is not None:
        earliest, latest = date(int(year), 1, 1), date(int(year), 12, 31)
        # A nonoverlapping primary_release_year is already sent separately. Keep valid date bounds.
        if earliest <= upper and latest >= lower:
            lower = max(lower, earliest)
            upper = min(upper, latest)
    return {
        "primary_release_date.gte": lower.isoformat(),
        "primary_release_date.lte": upper.isoformat(),
        "sort_by": "primary_release_date.asc" if upcoming else "primary_release_date.desc",
        "with_release_type": "2|3",
    }


def build_tmdb_request(
    provider: str,
    secret: str,
    config: Mapping[str, Any],
    *,
    changes_window: TmdbChangesWindow | None = None,
) -> ProviderRequest:
    """Build one keyed TMDB request, using an optional leased window for movie changes pagination."""
    from modules.connectors.providers.keyed import place_secret

    return place_secret(
        provider, _tmdb_url(provider, config, changes_window), "query:api_key", secret
    )


def _tmdb_genres(raw_values: object, provider: str) -> list[str]:
    """Normalize TMDB integer genre IDs or detail-response genre objects for one provider map."""
    if not isinstance(raw_values, list):
        return []
    genre_ids = []
    for item in raw_values:
        if type(item) is int:
            genre_ids.append(str(item))
        elif isinstance(item, Mapping) and type(item.get("id")) is int:
            genre_ids.append(str(item["id"]))
    return normalize_genres(genre_ids, provider=provider)


def _tmdb_record(
    row: object, provider: str, collected_at: datetime, *, allow_id_only: bool = False
) -> IngestionRecord | None:
    """Normalize one TMDB Movie/TV list result into typed media provenance."""
    if not isinstance(row, Mapping) or type(row.get("id")) is not int or row["id"] <= 0:
        return None
    title = row.get("title") if provider == "tmdb_movies" else row.get("name")
    if allow_id_only and not isinstance(title, str):
        title = f"TMDB movie {row['id']}"
    if not isinstance(title, str) or not title.strip():
        return None
    date_raw = row.get("release_date") if provider == "tmdb_movies" else row.get("first_air_date")
    date_text, year, release_at = normalize_partial_date(date_raw)
    genres = _tmdb_genres(row.get("genre_ids") or row.get("genres"), provider)
    country_values = row.get("origin_country")
    country = (
        normalize_country(country_values[0])
        if isinstance(country_values, list) and country_values
        else None
    )
    if (
        country is None
        and isinstance(row.get("production_countries"), list)
        and row["production_countries"]
    ):
        country = (
            normalize_country(row["production_countries"][0].get("iso_3166_1"))
            if isinstance(row["production_countries"][0], Mapping)
            else None
        )
    language = normalize_language(row.get("original_language"))
    rating = normalize_rating(row.get("vote_average"), scale=10)
    poster = normalize_cover_url(
        f"https://image.tmdb.org/t/p/w500{row['poster_path']}"
        if isinstance(row.get("poster_path"), str)
        else None
    )
    fields: dict[str, Any] = {
        "media_type": "movie" if provider == "tmdb_movies" else "tv",
        "artwork_aspect": "poster",
        "external_ids": build_external_ids({"tmdb": row["id"]}),
        "attribution": "Metadata by TMDB",
    }
    if release_at:
        fields["release_status"] = "upcoming" if release_at > collected_at else "released"
    if date_text and year:
        fields.update(release_date=date_text, release_year=year)
    if country:
        fields["country"] = country
    if language:
        fields["language"] = language
    if genres:
        fields["genres"] = genres
    if rating is not None:
        fields["rating"] = rating
    if poster:
        fields["image_url"] = poster
    overview_value = row.get("overview")
    overview = overview_value if isinstance(overview_value, str) else ""
    return _movie_record(
        provider=provider,
        identity=build_media_identity("tmdb", row["id"]),
        title=title.strip(),
        collected_at=collected_at,
        fields=fields,
        summary=overview,
        canonical_url=f"https://www.themoviedb.org/{'movie' if provider == 'tmdb_movies' else 'tv'}/{row['id']}",
    )


def map_tmdb(
    payload: object, collected_at: datetime, config: Mapping[str, Any], provider: str
) -> list[IngestionRecord]:
    """Map one TMDB page, retaining upstream country matches when movie rows omit that metadata."""

    rows = payload.get("results") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError(f"{provider}_response_invalid")
    records = {
        record.provider_id: record
        for row in rows
        if (
            record := _tmdb_record(
                row, provider, collected_at, allow_id_only=config.get("tmdb_mode") == "changes"
            )
        )
        is not None
    }
    if provider != "tmdb_movies" or not (
        config.get("media_countries") or config.get("media_country")
    ):
        return filter_media_records(config, list(records.values()))
    # Discover already applied with_origin_country. Relax only country checks on abbreviated movie rows.
    country_fields = {"media_country", "media_countries"}
    without_country = {name: value for name, value in config.items() if name not in country_fields}
    return [
        record
        for record in records.values()
        if filter_media_records(
            config
            if record.metadata["provider_record"]["source_fields"].get("country")
            else without_country,
            [record],
        )
    ]


def _trakt_url(config: Mapping[str, Any]) -> str:
    """Build a one-request Trakt trending, anticipated, calendar, or search URL."""
    from urllib.parse import urlencode

    mode = config.get("trakt_mode") or "movies_trending"
    fixed = {
        "movies_trending": "/movies/trending",
        "movies_anticipated": "/movies/anticipated",
        "shows_trending": "/shows/trending",
        "shows_anticipated": "/shows/anticipated",
    }
    if mode in fixed:
        path = fixed[mode]
    elif mode in {"movies_calendar", "shows_calendar"}:
        days = config.get("trakt_calendar_days") or 7
        kind = "movies" if mode == "movies_calendar" else "shows"
        path = f"/calendars/all/{kind}/{datetime.now(UTC).date().isoformat()}/{days}"
    elif mode in {"movies_search", "shows_search"}:
        kind = "movie" if mode == "movies_search" else "show"
        path = f"/search/{kind}"
    else:
        raise ValueError("trakt_media_scope_invalid")
    params = _media_options(config, "trakt_media")
    if mode.endswith("_search"):
        params.update(query=config["trakt_query"], extended="full")
    else:
        params["extended"] = "full"
    return f"{TRAKT_BASE}{path}?{urlencode(params)}"


def build_trakt_request(secret: str, config: Mapping[str, Any]) -> ProviderRequest:
    """Build one Trakt API v2 request with the key in its required header."""
    from modules.connectors.providers.keyed import place_secret

    return place_secret(
        "trakt_media", _trakt_url(config), "trakt-api-key", secret, {"trakt-api-version": "2"}
    )


def map_trakt(
    payload: object, collected_at: datetime, config: Mapping[str, Any]
) -> list[IngestionRecord]:
    """Map the selected Trakt mode and deduplicate stable media or episode identities."""
    if not isinstance(payload, list) or len(payload) > 100:
        raise ValueError("trakt_media_response_invalid")
    mode = config.get("trakt_mode") or "movies_trending"
    records: dict[str, IngestionRecord] = {}
    for item in payload:
        if isinstance(item, Mapping) and (record := _trakt_record(item, mode, collected_at)):
            records.setdefault(record.provider_id, record)
    return filter_media_records(config, list(records.values()))


def _trakt_record(
    item: Mapping[str, Any], mode: str, collected_at: datetime
) -> IngestionRecord | None:
    """Use the envelope to identify shows and retain each calendar episode's ID and air date."""
    kind = (
        "tv"
        if item.get("show") is not None
        else "movie"
        if item.get("movie") is not None
        else ("tv" if item.get("type") == "show" or mode.startswith("shows_") else "movie")
    )
    media = item.get("show" if kind == "tv" else "movie", item)
    if not isinstance(media, Mapping):
        return None
    title, ids = media.get("title"), media.get("ids")
    if not isinstance(ids, Mapping):
        return None
    trakt_id = ids.get("trakt")
    if (
        not isinstance(title, str)
        or not title.strip()
        or type(trakt_id) is not int
        or trakt_id <= 0
    ):
        return None
    slug = ids.get("slug")
    base_url = f"https://trakt.tv/{'movies' if kind == 'movie' else 'shows'}/{slug or trakt_id}"
    if mode == "shows_calendar" or (kind == "tv" and "episode" in item):
        return _trakt_episode(item, media, title, base_url, collected_at)
    release = item.get("released") or media.get("released") or media.get("first_aired")
    fields, _ = _trakt_fields(media, kind, release, collected_at)
    fields["external_ids"] = build_external_ids(ids)
    return _movie_record(
        provider="trakt_media",
        identity=build_media_identity("trakt", trakt_id),
        title=title.strip(),
        collected_at=collected_at,
        fields=fields,
        summary=str(media.get("overview") or ""),
        canonical_url=base_url,
    )


def _trakt_episode(
    item: Mapping[str, Any],
    show: Mapping[str, Any],
    show_title: str,
    base_url: str,
    collected_at: datetime,
) -> IngestionRecord | None:
    """Normalize one aired or upcoming episode with its own external IDs and show-level filters."""
    episode = item.get("episode")
    if not isinstance(episode, Mapping):
        return None
    ids = episode.get("ids")
    if not isinstance(ids, Mapping):
        return None
    episode_id = ids.get("trakt")
    season, number = episode.get("season"), episode.get("number")
    if (
        type(episode_id) is not int
        or episode_id <= 0
        or type(season) is not int
        or type(number) is not int
    ):
        return None
    release = item.get("first_aired") or episode.get("first_aired")
    fields, aired = _trakt_fields(show, "tv", release, collected_at)
    fields.update(
        external_ids=build_external_ids(ids),
        season=str(season),
        episode=str(number),
        format="episodic",
    )
    title = episode.get("title")
    display_title = (
        f"{show_title.strip()} — {title.strip()}"
        if isinstance(title, str) and title.strip()
        else show_title.strip()
    )
    return _movie_record(
        provider="trakt_media",
        identity=f"trakt:episode:{episode_id}",
        title=display_title,
        collected_at=collected_at,
        fields=fields,
        summary=str(episode.get("overview") or ""),
        canonical_url=f"{base_url}/seasons/{season}/episodes/{number}",
        observed_at=aired,
    )


def _trakt_fields(
    media: Mapping[str, Any],
    kind: str,
    release: object,
    collected_at: datetime,
) -> tuple[dict[str, Any], datetime | None]:
    """Normalize filterable show/movie metadata and the selected release or air date."""
    fields: dict[str, Any] = {
        "media_type": kind,
        "artwork_aspect": "poster",
        "attribution": "Data via Trakt",
    }
    if (rating := normalize_rating(media.get("rating"), scale=10)) is not None:
        fields["rating"] = rating
    for key, value in (
        ("content_rating", normalize_content_rating(media.get("certification"))),
        ("genres", normalize_genres(media.get("genres") or [], provider="trakt_media")),
        ("country", normalize_country(media.get("country"))),
        ("language", normalize_language(media.get("language"))),
    ):
        if value:
            fields[key] = value
    date_text, year, release_at = normalize_partial_date(release)
    if date_text and year:
        fields.update(
            release_date=date_text,
            release_year=year,
            release_status="upcoming" if release_at and release_at > collected_at else "released",
        )
    return fields, release_at


def _omdb_url(config: Mapping[str, Any]) -> str:
    """Build a bounded single OMDb title, search, or IMDb-ID query with optional type and year."""
    from urllib.parse import urlencode

    validate_omdb_search_filters(config)
    mode = config.get("omdb_mode") or "title"
    params = {"r": "json"}
    key = {"imdb_id": "i", "title": "t", "search": "s"}.get(mode)
    value = config.get(
        {"imdb_id": "omdb_imdb_id", "title": "omdb_title", "search": "omdb_search"}.get(mode, "")
    )
    if key is None or not isinstance(value, str) or not value.strip():
        raise ValueError("omdb_lookup_scope_invalid")
    params[key] = value.strip()
    if config.get("omdb_type"):
        params["type"] = config["omdb_type"]
    if config.get("release_year") is not None:
        params["y"] = str(config["release_year"])
    return OMDB_BASE + "?" + urlencode(params)


def build_omdb_request(secret: str, config: Mapping[str, Any]) -> ProviderRequest:
    """Return the already-encoded OMDb request in a secret-redacting request envelope."""
    from modules.connectors.providers.keyed import place_secret

    return place_secret("omdb_lookup", _omdb_url(config), "query:apikey", secret)


def map_omdb(
    payload: object, collected_at: datetime, config: Mapping[str, Any]
) -> list[IngestionRecord]:
    """Map OMDb detail or abbreviated search rows, classifying individual episodes as episodic TV."""
    from modules.connectors.provider_options import filter_media_records

    if not isinstance(payload, Mapping) or payload.get("Response") == "False":
        raise ValueError("omdb_lookup_response_invalid")
    search_rows = payload.get("Search")
    rows: list[object] = search_rows if isinstance(search_rows, list) else [payload]
    if len(rows) > 100:
        raise ValueError("omdb_lookup_response_invalid")
    records: dict[str, IngestionRecord] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        imdb = row.get("imdbID")
        title = row.get("Title")
        if (
            not isinstance(imdb, str)
            or not re.fullmatch(r"tt\d{7,12}", imdb)
            or not isinstance(title, str)
            or not title.strip()
        ):
            continue
        kind = "tv" if row.get("Type") in ("series", "episode") else "movie"
        year_raw = row.get("Year")
        year_match = (
            re.search(r"\b(18\d{2}|19\d{2}|20\d{2}|2100)\b", year_raw)
            if isinstance(year_raw, str)
            else None
        )
        fields: dict[str, Any] = {
            "media_type": kind,
            "artwork_aspect": "poster",
            "external_ids": {"imdb": imdb},
            "attribution": "Data via OMDb API",
            "canonical_url": f"https://www.imdb.com/title/{imdb}/",
        }
        if row.get("Type") == "episode":
            fields["format"] = "episodic"
        if year_match:
            fields["release_year"] = int(year_match.group(1))
        countries = [normalize_country(value) for value in str(row.get("Country") or "").split(",")]
        country = next((value for value in countries if value), None)
        if country:
            fields["country"] = country
        language = normalize_language(str(row.get("Language") or "").split(",")[0])
        if language:
            fields["language"] = language
        genres = normalize_genres(str(row.get("Genre") or "").split(","))
        if genres:
            fields["genres"] = genres
        rating = normalize_rating(row.get("imdbRating"), scale=10)
        if rating is not None:
            fields["rating"] = rating
        content_rating = normalize_content_rating(row.get("Rated"))
        if content_rating:
            fields["content_rating"] = content_rating
        poster = normalize_cover_url(row.get("Poster"))
        if poster:
            fields["image_url"] = poster
        record = _movie_record(
            provider="omdb_lookup",
            identity=build_media_identity("imdb", imdb),
            title=title.strip(),
            collected_at=collected_at,
            fields=fields,
            summary=str(row.get("Plot") or ""),
        )
        records.setdefault(record.provider_id, record)
    return filter_media_records(config, list(records.values()))


@dataclass(frozen=True)
class TVMazePage:
    """One schedule response and its URL-scoped validators for atomic persistence."""

    records: tuple[IngestionRecord, ...]
    not_modified: bool
    etag: str | None
    last_modified: str | None


def tvmaze_schedule_url(*, country: str | None, date: str, web: bool = False) -> str:
    """Build a fixed TVMaze schedule URL; an empty web country selects global channels."""
    if (
        country is not None
        and not (web and country == "")
        and (
            len(country) != 2
            or not country.isascii()
            or not country.isupper()
            or not country.isalpha()
            or country == "UK"
        )
    ):
        raise ValueError("tvmaze_country_invalid")
    try:
        if datetime.strptime(date, "%Y-%m-%d").strftime("%Y-%m-%d") != date:
            raise ValueError
    except ValueError as exc:
        raise ValueError("tvmaze_date_invalid") from exc
    query: dict[str, str] = {"date": date}
    if country is not None:
        query["country"] = country
    return f"https://api.tvmaze.com/schedule{'/web' if web else ''}?{urlencode(query)}"


def _add_tvmaze_show_fields(
    fields: dict[str, Any], row: Mapping[str, Any], show: Mapping[str, Any]
) -> None:
    """Add the optional format, season, episode and cover fields to ``fields`` in place.

    Unknown show types, out-of-range numbers and unsafe cover URLs are skipped silently.
    """
    show_type = show.get("type")
    if isinstance(show_type, str):
        normalized_type = show_type.lower().replace(" ", "_")
        if normalized_type in {"scripted", "animation", "reality", "talk_show", "documentary"}:
            fields["format"] = normalized_type
    for key in ("season", "episode"):
        value = row.get("number" if key == "episode" else key)
        if type(value) is int and 0 <= value <= 99999:
            fields[key] = str(value)
    image = show.get("image")
    cover = normalize_cover_url(image.get("medium")) if isinstance(image, Mapping) else None
    if cover:
        fields["image_url"] = cover


def _tvmaze_record(row: object, collected_at: datetime) -> IngestionRecord | None:
    """Map one valid TVMaze episode to bounded provenance; skip incomplete rows."""
    if not isinstance(row, Mapping):
        return None
    embedded = row.get("_embedded")
    show = row.get("show") or (embedded.get("show") if isinstance(embedded, Mapping) else None)
    if not isinstance(show, Mapping):
        return None
    episode_id, show_id = row.get("id"), show.get("id")
    title, show_name = row.get("name"), show.get("name")
    if (
        type(episode_id) is not int
        or episode_id <= 0
        or type(show_id) is not int
        or show_id <= 0
        or not isinstance(show_name, str)
        or not show_name.strip()
    ):
        return None
    date_text, year, air_time = normalize_partial_date(row.get("airstamp") or row.get("airdate"))
    if air_time is None or date_text is None or year is None:
        return None
    title = title.strip() if isinstance(title, str) and title.strip() else "Episode"
    heading = f"{show_name.strip()} — {title}"[:500]
    summary = _plain_text(row.get("summary") or "") if isinstance(row.get("summary"), str) else ""
    network = show.get("network") or show.get("webChannel") or {}
    country = (
        normalize_country(network.get("country", {}).get("code"))
        if isinstance(network, Mapping) and isinstance(network.get("country"), Mapping)
        else None
    )
    language = normalize_language(show.get("language"))
    raw_genres = show.get("genres")
    genres = normalize_genres(
        [value for value in raw_genres if isinstance(value, str)]
        if isinstance(raw_genres, list)
        else []
    )
    rating_obj = show.get("rating")
    rating = (
        normalize_rating(rating_obj.get("average"), scale=10)
        if isinstance(rating_obj, Mapping)
        else None
    )
    fields: dict[str, Any] = {
        "title": heading,
        "media_type": "tv",
        "artwork_aspect": "poster",
        "release_date": date_text,
        "release_year": year,
        "release_status": "upcoming" if air_time > collected_at else "released",
        "creator": show_name.strip()[:240],
        "attribution": "Data via TVMaze (CC BY-SA 4.0)",
        "external_ids": {"tvmaze": str(show_id)},
        "canonical_url": f"https://www.tvmaze.com/episodes/{episode_id}",
    }
    if country:
        fields["country"] = country
    if language:
        fields["language"] = language
    if genres:
        fields["genres"] = genres
    if rating is not None:
        fields["rating"] = rating
    _add_tvmaze_show_fields(fields, row, show)
    identity = f"tvmaze:{episode_id}"
    raw_content = f"{heading}\n\n{summary}"
    content = raw_content[:20000]
    provider_record = ProviderRecordMetadata(
        provider="tvmaze_schedule",
        identity=identity,
        timestamp_basis="collection",
        coverage="returned_snapshot",
        content_truncated=len(raw_content) > len(content),
        source_fields=fields,
    )
    metadata: dict[str, Any] = {
        "provider": "tvmaze_schedule",
        "title": heading,
        "canonical_url": fields["canonical_url"],
        "provider_record": provider_record.model_dump(mode="json", exclude_none=True),
    }
    if language:
        metadata["language"] = language
    return IngestionRecord(
        provider_id=identity,
        content=content,
        observed_at=collected_at,
        collected_at=collected_at,
        metadata=metadata,
    )


def map_tvmaze_schedule(
    payload: object, *, collected_at: datetime, config: Mapping[str, Any]
) -> list[IngestionRecord]:
    """Map a bounded schedule and apply source filters; reject oversized or invalid batches."""
    if not isinstance(payload, list) or len(payload) > 500:
        raise ValueError("tvmaze_response_invalid")
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collected_at must include a timezone")
    records: dict[str, IngestionRecord] = {}
    for row in payload:
        record = _tvmaze_record(row, collected_at)
        if record is not None:
            records.setdefault(record.provider_id, record)
    return filter_media_records(config, list(records.values()))


async def collect_tvmaze_schedule(
    fetch_bytes: Callable[..., Awaitable[Fetched]],
    *,
    country: str | None,
    date: str,
    web: bool,
    config: Mapping[str, Any],
    collected_at: datetime,
    etag: str | None = None,
    last_modified: str | None = None,
) -> TVMazePage:
    """Fetch one bounded schedule through the injected gated transport.

    Callers must only supply validators previously captured for the same URL and connector revision.
    A 304 retains existing records; HTTP, quota, and authorization errors propagate without acceptance.
    """
    headers = {"Accept": "application/json"}
    etag, last_modified = _header_value(etag), _header_value(last_modified)
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    fetched = await fetch_bytes(
        tvmaze_schedule_url(country=country, date=date, web=web),
        headers=headers,
        max_bytes=2 * 1024 * 1024,
    )
    if fetched.body is None:
        if not etag and not last_modified:
            raise ProviderHttpError(304)
        return TVMazePage((), True, etag, last_modified)
    try:
        payload = json.loads(fetched.body)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("tvmaze_response_invalid") from exc
    return TVMazePage(
        tuple(map_tvmaze_schedule(payload, collected_at=collected_at, config=config)),
        False,
        _header_value(fetched.etag),
        _header_value(fetched.last_modified),
    )


TVMAZE_FOLLOWED_LOOKBACK = timedelta(days=14)
TVMAZE_FOLLOWED_LOOKAHEAD = timedelta(days=60)


def tvmaze_show_url(show_id: int) -> str:
    """Build the fixed TVMaze show URL embedding only its previous and next episodes (one small call).

    Raises:
        ValueError: ``tvmaze_show_id_invalid`` unless ``show_id`` is a positive integer.
    """
    if type(show_id) is not int or show_id <= 0:
        raise ValueError("tvmaze_show_id_invalid")
    return f"https://api.tvmaze.com/shows/{show_id}?embed%5B%5D=previousepisode&embed%5B%5D=nextepisode"


def map_tvmaze_followed_show(
    payload: object, *, collected_at: datetime, config: Mapping[str, Any]
) -> list[IngestionRecord]:
    """Map one show's embedded previous/next episodes inside the recent/upcoming window.

    Episodes older than 14 days or later than 60 days after ``collected_at`` are dropped so the
    followed-show snapshot stays a bounded calendar. Source filters apply as for schedules.

    Raises:
        ValueError: ``tvmaze_response_invalid`` when ``payload`` is not a show object.
    """
    if not isinstance(payload, Mapping):
        raise ValueError("tvmaze_response_invalid")
    embedded = payload.get("_embedded")
    show = {key: value for key, value in payload.items() if key != "_embedded"}
    rows: list[dict[str, Any]] = []
    for key in ("previousepisode", "nextepisode"):
        episode = embedded.get(key) if isinstance(embedded, Mapping) else None
        if isinstance(episode, Mapping):
            rows.append({**episode, "show": show})
    records: list[IngestionRecord] = []
    for row in rows:
        record = _tvmaze_record(row, collected_at)
        air = normalize_partial_date(row.get("airstamp") or row.get("airdate"))[2]
        if (
            record is not None
            and air is not None
            and collected_at - TVMAZE_FOLLOWED_LOOKBACK
            <= air
            <= collected_at + TVMAZE_FOLLOWED_LOOKAHEAD
        ):
            records.append(record)
    return filter_media_records(config, records)


async def collect_tvmaze_followed_shows(
    fetch_bytes: Callable[..., Awaitable[Fetched]],
    *,
    show_ids: tuple[int, ...],
    config: Mapping[str, Any],
    collected_at: datetime,
) -> tuple[IngestionRecord, ...]:
    """Fetch each followed show sequentially (one gated call per id, at most 20) and merge episodes.

    Every call goes through the injected gated transport so quota is debited per request. A 404
    (show removed upstream) is skipped; any other HTTP, quota or parse error propagates so the run
    accepts nothing rather than a partial snapshot. Records dedupe by episode id.

    Raises:
        ValueError: more than 20 ids, or a malformed payload.
    """
    if len(show_ids) > 20:
        raise ValueError("tvmaze_show_ids_too_many")
    records: dict[str, IngestionRecord] = {}
    for show_id in sorted(set(show_ids)):
        try:
            fetched = await fetch_bytes(
                tvmaze_show_url(show_id),
                headers={"Accept": "application/json"},
                max_bytes=512 * 1024,
            )
        except ProviderHttpError as exc:
            if exc.status_code == 404:
                continue
            raise
        if fetched.body is None:
            continue
        try:
            payload = json.loads(fetched.body)
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("tvmaze_response_invalid") from exc
        for record in map_tvmaze_followed_show(payload, collected_at=collected_at, config=config):
            records.setdefault(record.provider_id, record)
    return tuple(records.values())
