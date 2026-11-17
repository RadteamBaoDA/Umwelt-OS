"""Keyless, bounded AniList GraphQL collectors for anime and manga catalogs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from modules.connectors.provider_options import filter_media_records
from modules.connectors.providers._media_common import (
    build_external_ids,
    build_media_identity,
    normalize_content_rating,
    normalize_country,
    normalize_cover_url,
    normalize_genres,
    normalize_partial_date,
    normalize_rating,
)
from modules.connectors.providers.rest import Fetched
from modules.ingestion.public import IngestionRecord
from modules.knowledge.documents.public import ProviderRecordMetadata

ANILIST_URL = "https://graphql.anilist.co"
MAX_RESULTS = 50
MAX_PAGES_PER_RUN = 10
AIRING_WINDOW = timedelta(days=14)
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_ANILIST_GENRES = {
    "sci_fi": "Sci-Fi",
    "slice_of_life": "Slice of Life",
    "non_fiction": "Non-Fiction",
}
_MEDIA_QUERY = """query ($page: Int!, $perPage: Int!, $type: MediaType!, $ids: [Int], $country: CountryCode,
  $genres: [String], $tags: [String], $formats: [MediaFormat], $season: MediaSeason, $seasonYear: Int,
  $status: [MediaStatus], $isAdult: Boolean, $score: Int, $sort: [MediaSort], $updatedAfter: Int) {
  Page(page: $page, perPage: $perPage) {
    pageInfo { hasNextPage }
    media(type: $type, id_in: $ids, countryOfOrigin: $country, genre_in: $genres, tag_in: $tags, format_in: $formats,
      season: $season, seasonYear: $seasonYear, status_in: $status, isAdult: $isAdult,
      averageScore_greater: $score, updatedAt_greater: $updatedAfter, sort: $sort) {
      id idMal type format status seasonYear season countryOfOrigin isAdult averageScore updatedAt
      genres tags { name } episodes chapters volumes description siteUrl
      title { romaji english native }
      startDate { year month day }
      coverImage { large }
      airingSchedule(notYetAired: true, perPage: 1) { nodes { airingAt episode } }
    }
  }
}"""


def build_anilist_request(
    provider: str, config: Mapping[str, Any], *, page: int = 1, updated_after: int | None = None
) -> bytes:
    """Serialize one bounded AniList page and its provider-specific mode and filters."""
    if provider not in {"anilist_anime", "anilist_manga"}:
        raise ValueError("anilist_provider_invalid")
    if type(page) is not int or not 1 <= page <= 1_000_000:
        raise ValueError("anilist_page_invalid")
    mode = config.get("anilist_mode") or "updated"
    if mode not in (
        {"airing", "trending", "updated"}
        if provider == "anilist_anime"
        else {"trending", "updated"}
    ):
        raise ValueError("anilist_mode_invalid")
    year = config.get("release_year")
    season = config.get("anime_season")
    statuses = [value.upper() for value in config.get("media_statuses", ())]
    if mode == "airing" and not statuses:
        statuses = ["RELEASING"]
    country = (
        (config.get("country_of_origin") if config.get("country_of_origin") != "ALL" else None)
        if provider == "anilist_manga" and config.get("country_of_origin") is not None
        else (
            config.get("media_country")
            or (
                next(iter(config.get("media_countries") or ()), None)
                if len(config.get("media_countries") or ()) == 1
                else None
            )
        )
    )
    variables: dict[str, Any] = {
        "page": page,
        "perPage": MAX_RESULTS,
        "type": "ANIME" if provider == "anilist_anime" else "MANGA",
        "ids": config.get("media_ids") or None,
        "country": country,
        "genres": [
            _ANILIST_GENRES.get(value, value.replace("_", " ").title())
            for value in config.get("media_genres", ())
        ]
        or None,
        "tags": list(config.get("media_tags", ())) or None,
        "formats": [value.upper() for value in config.get("media_formats", ())] or None,
        "season": season.upper() if season else None,
        "seasonYear": year,
        "status": statuses or None,
        # Adult titles require an explicit opt-in; None means the API applies no adult filter.
        "isAdult": False if config.get("exclude_adult") is not False else None,
        "score": int(config["min_rating"]) if config.get("min_rating") is not None else None,
        "updatedAfter": updated_after,
        # ID order makes offset pages stable across runs while the bounded full sweep rotates.
        "sort": ["TRENDING_DESC"] if mode == "trending" else ["ID"],
    }
    return json.dumps(
        {"query": _MEDIA_QUERY, "variables": variables}, separators=(",", ":")
    ).encode()


def anilist_query_fingerprint(provider: str, config: Mapping[str, Any]) -> str:
    """Hash query variables except the mutable page number for cursor and scope matching."""
    variables = json.loads(build_anilist_request(provider, config))["variables"]
    variables.pop("page")
    filter_names = (
        "media_country",
        "media_countries",
        "media_genres",
        "media_tags",
        "media_formats",
        "release_year",
        "anime_season",
        "media_statuses",
        "min_rating",
        "exclude_adult",
        "country_of_origin",
        "media_ids",
        "anilist_mode",
    )
    post_filters = {key: config[key] for key in filter_names if key in config}
    return hashlib.sha256(
        json.dumps(
            [provider, variables, post_filters], sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def parse_anilist_cursor(
    cursor: str | None, *, fingerprint_matches: bool, collected_at: datetime
) -> tuple[int, int]:
    """Read the page and fixed 24-hour update watermark, resetting malformed or stale state safely."""
    if fingerprint_matches and cursor:
        try:
            page_text, watermark_text = cursor.split(":", 1)
            page, watermark = int(page_text), int(watermark_text)
            if 1 <= page <= 1_000_000 and 0 <= watermark <= int(collected_at.timestamp()):
                if page != 1 or watermark != 0:
                    return page, watermark
        except (ValueError, OverflowError):
            pass
    return 1, max(0, int((collected_at - timedelta(days=1)).timestamp()))


def _primary_title(title_obj: object) -> str | None:
    """Return the first non-blank localized title, stripped, or None when every variant is blank.

    Pure: reads the mapping's values in iteration order; non-mapping input yields None.
    """
    return next(
        (
            v.strip()
            for v in (title_obj.values() if isinstance(title_obj, Mapping) else ())
            if isinstance(v, str) and v.strip()
        ),
        None,
    )


def _media_kind(is_manga: bool, country: str | None) -> str:
    """Classify a row: anime for anime providers, else manhwa, manhua or manga by country."""
    if not is_manga:
        return "anime"
    return "manhwa" if country == "KR" else "manhua" if country in {"CN", "TW"} else "manga"


def _canonical_url(row: Mapping[str, Any], is_manga: bool) -> str:
    """Return siteUrl when it is an https anilist.co link, else the rebuilt canonical URL."""
    site = row.get("siteUrl")
    if isinstance(site, str) and site.startswith("https://anilist.co/"):
        return site
    return f"https://anilist.co/{'manga' if is_manga else 'anime'}/{row['id']}"


def _airing_in_window(episode: object, airing_at: object, collected_at: datetime) -> bool:
    """Return True when episode is an int in 1..10000 and airing_at is in the 14-day window.

    The window is (collected_at, collected_at + AIRING_WINDOW] in whole seconds. Non-int values
    fail closed (False), so the caller drops the airing.
    """
    return (
        type(episode) is int
        and 1 <= episode <= 10000
        and type(airing_at) is int
        and int(collected_at.timestamp())
        < airing_at
        <= int((collected_at + AIRING_WINDOW).timestamp())
    )


def _start_date(row: Mapping[str, Any]) -> tuple[int | None, str | None]:
    """Read startDate as (year, partial date text). Year is 1800..2100 or None.

    Date text is produced only when month and day are both ints; otherwise it is None.
    """
    start = row.get("startDate")
    if not (
        isinstance(start, Mapping)
        and type(start.get("year")) is int
        and 1800 <= start["year"] <= 2100
    ):
        return None, None
    year: int = start["year"]
    month, day = start.get("month"), start.get("day")
    date_text: str | None = None
    if type(month) is int and type(day) is int:
        date_text, _, _ = normalize_partial_date(f"{year:04d}-{month:02d}-{day:02d}")
    return year, date_text


def _base_fields(
    row: Mapping[str, Any], *, title: str, kind: str, url: str, country: str | None
) -> dict[str, Any]:
    """Return the always-present source fields in stable key order; country only when set."""
    fields: dict[str, Any] = {
        "title": title[:500],
        "media_type": kind,
        "attribution": "Data via AniList",
        "canonical_url": url,
        "external_ids": build_external_ids({"anilist": row["id"], "mal": row.get("idMal")}),
    }
    if country:
        fields["country"] = country
    return fields


def _release_fields(
    row: Mapping[str, Any], *, episode: object, airing_at: object
) -> dict[str, Any]:
    """Return release_year and release_date, plus the airing date and episode for an airing.

    An airing overrides release_date with its UTC air time and sets episode as a string.
    Callers must validate episode and airing_at through _airing_in_window first.
    """
    fields: dict[str, Any] = {}
    year, date_text = _start_date(row)
    if year:
        fields["release_year"] = year
    if date_text:
        fields["release_date"] = date_text
    if airing_at is not None:
        fields["release_date"] = datetime.fromtimestamp(cast(int, airing_at), UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        fields["episode"] = str(episode)
    return fields


def _catalog_fields(row: Mapping[str, Any], *, airing_at: object) -> dict[str, Any]:
    """Return format, season, release status, rating and provider update time when present.

    release_status is omitted for airings, whose status is implied by the air time.
    """
    fields: dict[str, Any] = {}
    if row.get("format"):
        fields["format"] = {"tv_short": "tv_short", "one_shot": "one_shot"}.get(
            str(row["format"]).lower(), str(row["format"]).lower()
        )
    if row.get("season"):
        fields["release_season"] = str(row["season"]).lower()
    release_status = {
        "FINISHED": "completed",
        "RELEASING": "airing",
        "NOT_YET_RELEASED": "upcoming",
        "HIATUS": "hiatus",
    }.get(str(row.get("status", "")).upper())
    if release_status and airing_at is None:
        fields["release_status"] = release_status
    rating = normalize_rating(row.get("averageScore"))
    if rating is not None:
        fields["rating"] = rating
    if type(row.get("updatedAt")) is int and row["updatedAt"] > 0:
        fields["provider_updated_at"] = datetime.fromtimestamp(row["updatedAt"], UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    return fields


def _content_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return genres, content rating and cover image for each that normalizes to a value."""
    fields: dict[str, Any] = {}
    genres = normalize_genres(
        [value for value in cast(list[object], row.get("genres") or []) if isinstance(value, str)]
    )
    if genres:
        fields["genres"] = genres
    content_rating = normalize_content_rating(row.get("isAdult", False))
    if content_rating:
        fields["content_rating"] = content_rating
    cover = row.get("coverImage")
    image = normalize_cover_url(cover.get("large")) if isinstance(cover, Mapping) else None
    if image:
        fields["image_url"] = image
    return fields


def _record(
    provider: str, row: object, collected_at: datetime, *, airing: object | None = None
) -> IngestionRecord | None:
    """Map a valid AniList media node or next airing to stable, attributed provenance.

    Raises ValueError("anilist_record_invalid") for a non-mapping row, a non-positive or non-int
    id, or a blank title. Returns None when an airing's episode or air time is out of range.
    Output field order is part of the stored source_fields shape and must not change.
    """
    if not isinstance(row, Mapping) or type(row.get("id")) is not int or row["id"] <= 0:
        raise ValueError("anilist_record_invalid")
    title = _primary_title(row.get("title"))
    if not title:
        raise ValueError("anilist_record_invalid")
    is_manga = provider == "anilist_manga"
    country = normalize_country(row.get("countryOfOrigin"))
    kind = _media_kind(is_manga, country)
    episode = airing.get("episode") if isinstance(airing, Mapping) else None
    airing_at = airing.get("airingAt") if isinstance(airing, Mapping) else None
    if airing is not None and not _airing_in_window(episode, airing_at, collected_at):
        return None
    identity = build_media_identity(
        provider, row["id"], scope=f"episode:{episode}" if episode is not None else None
    )
    url = _canonical_url(row, is_manga)
    output_title = f"{title} — Episode {episode}" if episode is not None else title
    fields = _base_fields(row, title=output_title, kind=kind, url=url, country=country)
    fields.update(_release_fields(row, episode=episode, airing_at=airing_at))
    fields.update(_catalog_fields(row, airing_at=airing_at))
    fields.update(_content_fields(row))
    description = row.get("description") if isinstance(row.get("description"), str) else ""
    content = f"{output_title}\n\n{description}"[:20000]
    metadata = ProviderRecordMetadata(
        provider=provider,
        identity=identity,
        timestamp_basis="collection",
        coverage="returned_snapshot",
        content_truncated=len(f"{output_title}\n\n{description}") > len(content),
        source_fields=fields,
    )
    return IngestionRecord(
        provider_id=identity,
        content=content,
        observed_at=collected_at,
        collected_at=collected_at,
        metadata={
            "provider": provider,
            "title": output_title[:500],
            "canonical_url": url,
            "provider_record": metadata.model_dump(mode="json", exclude_none=True),
        },
    )


@dataclass(frozen=True)
class AniListPage:
    """One validated GraphQL page and its continuation marker."""

    records: tuple[IngestionRecord, ...]
    has_next_page: bool
    row_count: int


def map_anilist_page(
    provider: str, payload: object, *, collected_at: datetime, config: Mapping[str, Any]
) -> AniListPage:
    """Validate page shape and mapped records before its cursor can advance."""
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collected_at must include a timezone")
    try:
        page = payload["data"]["Page"]  # type: ignore[index]
        rows = page["media"]
        has_next_page = page["pageInfo"]["hasNextPage"]
    except (TypeError, KeyError):
        raise ValueError("anilist_response_invalid") from None
    if not isinstance(rows, list) or len(rows) > MAX_RESULTS or type(has_next_page) is not bool:
        raise ValueError("anilist_response_invalid")
    records: dict[str, IngestionRecord] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("anilist_record_invalid")
        if (
            wanted_tags := {tag.casefold() for tag in config.get("media_tags", ())}
        ) and not wanted_tags.intersection(
            str(tag.get("name", "")).casefold()
            for tag in row.get("tags", ())
            if isinstance(tag, Mapping)
        ):
            continue
        airing = None
        if config.get("anilist_mode") == "airing":
            schedule = row.get("airingSchedule")
            nodes = schedule.get("nodes", ()) if isinstance(schedule, Mapping) else ()
            airing = next(iter(nodes), None)
            if airing is None:
                continue
        record = _record(provider, row, collected_at, airing=airing)
        if record is not None:
            records.setdefault(record.provider_id, record)
    return AniListPage(
        tuple(filter_media_records(config, list(records.values()))), has_next_page, len(rows)
    )


def map_anilist_response(
    provider: str, payload: object, *, collected_at: datetime, config: Mapping[str, Any]
) -> list[IngestionRecord]:
    """Compatibility mapper returning only records from one validated AniList page."""
    return list(
        map_anilist_page(provider, payload, collected_at=collected_at, config=config).records
    )


async def collect_anilist(
    fetch_bytes: Callable[..., Awaitable[Fetched]],
    provider: str,
    *,
    config: Mapping[str, Any],
    collected_at: datetime,
    page: int,
    updated_after: int | None = None,
) -> AniListPage:
    """Send one bounded gated GraphQL POST and validate records before the page is accepted."""
    fetched = await fetch_bytes(
        ANILIST_URL,
        headers={"Accept": "application/json"},
        json_body=build_anilist_request(provider, config, page=page, updated_after=updated_after),
        max_bytes=MAX_RESPONSE_BYTES,
        timeout=6,
    )
    if fetched.body is None:
        raise ValueError("anilist_response_invalid")
    try:
        payload = json.loads(fetched.body)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("anilist_response_invalid") from exc
    if isinstance(payload, Mapping) and payload.get("errors"):
        raise ValueError("anilist_graphql_error")
    return map_anilist_page(provider, payload, collected_at=collected_at, config=config)
