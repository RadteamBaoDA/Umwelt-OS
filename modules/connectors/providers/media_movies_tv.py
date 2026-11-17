"""Bounded TVMaze episode mapping for the gated media collection path."""

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from modules.connectors.provider_options import filter_media_records
from modules.connectors.providers._media_common import (
    normalize_country,
    normalize_cover_url,
    normalize_genres,
    normalize_language,
    normalize_partial_date,
    normalize_rating,
)
from modules.connectors.providers.feed_catalog import _plain_text
from modules.connectors.providers.news import _header_value
from modules.connectors.providers.rest import Fetched, ProviderHttpError
from modules.ingestion.public import IngestionRecord
from modules.knowledge.documents.public import ProviderRecordMetadata


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
