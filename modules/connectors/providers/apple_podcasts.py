"""Bounded Apple Books and podcast RSS collectors for the gated media path."""

import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlencode, urlsplit
from xml.etree import ElementTree as ET

from modules.connectors.provider_options import filter_media_records
from modules.connectors.providers import news
from modules.connectors.providers._media_common import (
    normalize_cover_url,
    normalize_genres,
    normalize_language,
    normalize_partial_date,
    parse_itunes_duration,
)
from modules.connectors.providers.news import _header_value, _local
from modules.connectors.providers.rest import Fetched, ProviderHttpError
from modules.ingestion.public import IngestionRecord
from modules.knowledge.documents.public import ProviderRecordMetadata

APPLE_BOOKS_BASE = "https://rss.marketingtools.apple.com/api/v2"
ITUNES_LOOKUP_BASE = "https://itunes.apple.com/lookup"
MAX_ITEMS = 100
MAX_FEED_BYTES = 1024 * 1024
_SAFE_PATH_PART = re.compile(r"^[a-z0-9-]{1,64}$")


@dataclass(frozen=True)
class MediaFeedPage:
    """One feed response with conditional validators for atomic persistence."""

    records: tuple[IngestionRecord, ...]
    not_modified: bool
    etag: str | None
    last_modified: str | None


def apple_books_url(country: str, kind: str, chart: str, *, limit: int = 100) -> str:
    """Build a canonical Apple Marketing Tools URL from bounded storefront and chart scope."""
    if (
        not isinstance(country, str)
        or len(country) != 2
        or not country.isascii()
        or not country.isalpha()
        or country != country.upper()
        or kind not in {"books", "audio-books"}
        or not isinstance(chart, str)
        or _SAFE_PATH_PART.fullmatch(chart) is None
        or type(limit) is not int
        or limit not in {10, 25, 50, 100}
    ):
        raise ValueError("apple_books_scope_invalid")
    return f"{APPLE_BOOKS_BASE}/{country.lower()}/{kind}/{chart}/{limit}/{kind}.json"


def _json_object(body: bytes) -> Mapping[str, Any]:
    """Decode one bounded provider JSON object and reject malformed or non-object payloads."""
    if len(body) > MAX_FEED_BYTES:
        raise ValueError("media_response_too_large")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("media_response_invalid") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("media_response_invalid")
    return payload


def _apple_book_record(row: object, *, collected_at: datetime, kind: str) -> IngestionRecord | None:
    """Normalize one Apple chart row; discard entries without a stable numeric ID and title."""
    if not isinstance(row, Mapping):
        return None
    item_id, raw_title = row.get("id"), row.get("name")
    if not isinstance(item_id, (str, int)) or isinstance(item_id, bool) or not str(item_id).isdigit():
        return None
    if not isinstance(raw_title, str) or not raw_title.strip():
        return None
    title = news.plain_text(raw_title, 500)
    date_text, year, released = normalize_partial_date(row.get("releaseDate"))
    raw_genres = row.get("genres")
    genres = normalize_genres([
        genre["name"] for genre in raw_genres
        if isinstance(genre, Mapping) and isinstance(genre.get("name"), str)
    ]) if isinstance(raw_genres, list) else []
    artwork = normalize_cover_url(row.get("artworkUrl100") or row.get("artworkUrl600"))
    link = news.https_link(row.get("url"))
    artist = row.get("artistName")
    identity = f"apple:{item_id}"
    fields: dict[str, Any] = {
        "title": title,
        "media_type": "book",
        "format": "audiobook" if kind == "audio-books" else "ebook",
        "artwork_aspect": "poster",
        "attribution": "Data via Apple Books",
        "external_ids": {"itunes": str(item_id)},
    }
    if date_text:
        fields["release_date"] = date_text
    if year:
        fields["release_year"] = year
    if released:
        fields["release_status"] = "upcoming" if released > collected_at else "released"
    if genres:
        fields["genres"] = genres
    if artwork:
        fields["image_url"] = artwork
    if isinstance(artist, str) and artist.strip():
        fields["creator"] = news.plain_text(artist, 240)
    advisory = row.get("contentAdvisoryRating")
    if isinstance(advisory, str):
        rating = {"explicit": "explicit", "clean": "clean"}.get(advisory.strip().casefold())
        if rating:
            fields["content_rating"] = rating
    if link:
        fields["canonical_url"] = link
    meta = ProviderRecordMetadata(
        provider="apple_books_rss", identity=identity,
        timestamp_basis="provider_published" if released else "collection",
        coverage="returned_snapshot", content_truncated=False, source_fields=fields,
    ).model_dump(mode="json", exclude_none=True)
    return IngestionRecord(
        provider_id=identity, content=title, observed_at=released or collected_at, collected_at=collected_at,
        metadata={"provider": "apple_books_rss", "title": title, "canonical_url": link, "provider_record": meta},
    )


def map_apple_books(payload: object, *, collected_at: datetime, config: Mapping[str, Any], kind: str) -> list[IngestionRecord]:
    """Map at most 100 Apple chart entries into normalized book records and apply configured filters."""
    if collected_at.tzinfo is None or collected_at.utcoffset() is None or kind not in {"books", "audio-books"}:
        raise ValueError("apple_books_response_invalid")
    feed = payload.get("feed") if isinstance(payload, Mapping) else None
    rows = feed.get("results") if isinstance(feed, Mapping) else None
    if not isinstance(rows, list) or len(rows) > MAX_ITEMS:
        raise ValueError("apple_books_response_invalid")
    records: dict[str, IngestionRecord] = {}
    for row in rows:
        record = _apple_book_record(row, collected_at=collected_at, kind=kind)
        if record is not None:
            records.setdefault(record.provider_id, record)
    return filter_media_records(config, list(records.values()), keep_missing=True)


def _xml_child(item: ET.Element, name: str) -> list[ET.Element]:
    """Return direct RSS children by namespace-insensitive local name."""
    return [node for node in item if _local(node.tag) == name]


def _episode_date(raw: str, collected_at: datetime) -> datetime | None:
    """Parse bounded RFC 2822/ISO episode timestamps, including future scheduled episodes."""
    if not raw or len(raw) > 64:
        return None
    try:
        moment = parsedate_to_datetime(raw) if not re.match(r"^\d{4}-\d{2}-\d{2}", raw) else datetime.fromisoformat(raw)
    except (ValueError, TypeError, OverflowError):
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None
    moment = moment.astimezone(collected_at.tzinfo)
    return moment if 1800 <= moment.year <= 2100 else None


def _podcast_explicit_rating(item: ET.Element, channel: ET.Element) -> str | None:
    """Use episode explicit rating when present, otherwise inherit the channel-level declaration."""
    node = next(iter(_xml_child(item, "explicit")), None)
    if node is None:
        node = next(iter(_xml_child(channel, "explicit")), None)
    value = (node.text or "").strip().lower() if node is not None else ""
    if value in {"yes", "true", "1", "explicit"}:
        return "explicit"
    if value in {"no", "false", "0", "clean"}:
        return "clean"
    return None


def _podcast_episode_record(
    item: ET.Element,
    *,
    channel: ET.Element,
    channel_type: str,
    language: str | None,
    collected_at: datetime,
) -> IngestionRecord | None:
    """Map one bounded episode, inheriting channel metadata without trusting XML URLs or text."""
    title_node = next(iter(_xml_child(item, "title")), None)
    title = news.plain_text(title_node.text if title_node is not None else None, 500)
    if not title:
        return None
    guid_node = next(iter(_xml_child(item, "guid")), None)
    link_node = next(iter(_xml_child(item, "link")), None)
    canonical = news.https_link(link_node.text if link_node is not None else None)
    guid = (guid_node.text or "").strip() if guid_node is not None else ""
    identity_seed = guid or canonical
    if not identity_seed:
        return None
    identity = "podcast:" + (
        identity_seed
        if len(identity_seed) <= 400 and not any(ord(ch) < 32 for ch in identity_seed)
        else hashlib.sha256(identity_seed.encode()).hexdigest()
    )
    summary_node = next(iter(_xml_child(item, "description")), None)
    summary = news.plain_text(summary_node.text if summary_node is not None else None, 4000)
    date_node = next(iter(_xml_child(item, "pubDate")), None)
    released = _episode_date(date_node.text or "", collected_at) if date_node is not None else None
    date_text, year, _ = normalize_partial_date(released.isoformat() if released else None)
    fields: dict[str, Any] = {
        "title": title,
        "media_type": "podcast",
        "format": channel_type,
        "artwork_aspect": "square",
        "attribution": "Data via podcast RSS",
        "release_status": "upcoming" if released and released > collected_at else "released",
    }
    if released and date_text:
        fields["release_date"], fields["release_year"] = date_text, year
    categories = [
        node.get("text") or (node.text or "")
        for node in item.iter()
        if _local(node.tag) == "category"
    ]
    genres = normalize_genres(categories)
    if genres:
        fields["genres"] = genres
    if language:
        fields["language"] = language
    if rating := _podcast_explicit_rating(item, channel):
        fields["content_rating"] = rating
    duration_node = next(iter(_xml_child(item, "duration")), None)
    duration = parse_itunes_duration(duration_node.text if duration_node is not None else None)
    if duration is not None:
        fields["duration_seconds"] = duration
    image = next(iter(_xml_child(item, "image")), None)
    if image is None:
        image = next(iter(_xml_child(channel, "image")), None)
    image_url = normalize_cover_url(image.get("href") or image.get("url") if image is not None else None)
    if image_url:
        fields["image_url"] = image_url
    if canonical:
        fields["canonical_url"] = canonical
    raw_content = f"{title}\n\n{summary}".strip()
    meta = ProviderRecordMetadata(
        provider="podcast_rss",
        identity=identity,
        timestamp_basis="provider_published" if released else "collection",
        coverage="returned_snapshot",
        content_truncated=len(raw_content) > 20000,
        source_fields=fields,
    ).model_dump(mode="json", exclude_none=True)
    return IngestionRecord(
        provider_id=identity,
        content=raw_content[:20000],
        observed_at=released or collected_at,
        collected_at=collected_at,
        metadata={"provider": "podcast_rss", "title": title, "canonical_url": canonical, "provider_record": meta},
    )


def map_podcast_feed(body: bytes, *, collected_at: datetime, config: Mapping[str, Any]) -> list[IngestionRecord]:
    """Map a capped RSS feed using channel-level type/rating defaults and shared media filters."""
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collected_at must include a timezone")
    root = news.safe_xml_root(body, max_bytes=MAX_FEED_BYTES)
    items = [node for node in root.iter() if _local(node.tag) == "item"]
    if len(items) > MAX_ITEMS:
        raise ValueError("podcast_feed_response_invalid")
    channel = next((node for node in root.iter() if _local(node.tag) == "channel"), root)
    raw_language = next((node.text or "" for node in _xml_child(channel, "language")), "")
    language = normalize_language(raw_language)
    channel_type = next((node.text or "" for node in _xml_child(channel, "type")), "episodic")
    channel_type = channel_type.strip().lower()
    if channel_type not in {"episodic", "serial"}:
        channel_type = "episodic"
    records: dict[str, IngestionRecord] = {}
    for item in items:
        record = _podcast_episode_record(
            item,
            channel=channel,
            channel_type=channel_type,
            language=language,
            collected_at=collected_at,
        )
        if record is not None:
            records.setdefault(record.provider_id, record)
    return filter_media_records(config, list(records.values()), keep_missing=True)


def podcast_feed_url(raw: object) -> str:
    """Validate a podcast feed URL before the SendGate performs DNS pinning and network policy checks."""
    if not isinstance(raw, str) or len(raw) > 2048 or any(ord(ch) < 33 for ch in raw):
        raise ValueError("podcast_feed_url_invalid")
    try:
        parts = urlsplit(raw)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
            raise ValueError
        _ = parts.port
    except ValueError as exc:
        raise ValueError("podcast_feed_url_invalid") from exc
    return raw


def itunes_podcast_lookup_url(raw: object) -> str:
    """Build Apple's fixed HTTPS lookup URL from a numeric podcast catalog ID."""
    if isinstance(raw, bool) or not isinstance(raw, (str, int)) or not str(raw).isdigit() or len(str(raw)) > 16:
        raise ValueError("itunes_podcast_id_invalid")
    return f"{ITUNES_LOOKUP_BASE}?" + urlencode({"id": str(raw), "entity": "podcast"})


async def _fetch_conditional(
    fetch_bytes: Callable[..., Awaitable[Fetched]], url: str, *, etag: str | None, last_modified: str | None
) -> Fetched:
    """GET one fixed or validated podcast URL with bounded validators through the injected gated transport."""
    headers = {"Accept": "application/rss+xml, application/atom+xml, application/xml, application/json"}
    if _header_value(etag):
        headers["If-None-Match"] = str(_header_value(etag))
    if _header_value(last_modified):
        headers["If-Modified-Since"] = str(_header_value(last_modified))
    return await fetch_bytes(url, headers=headers, max_bytes=MAX_FEED_BYTES)


async def collect_apple_books(
    fetch_bytes: Callable[..., Awaitable[Fetched]], *, country: str, kind: str, chart: str,
    collected_at: datetime, config: Mapping[str, Any], etag: str | None = None, last_modified: str | None = None,
) -> MediaFeedPage:
    """Fetch one canonical Apple chart with validators, map its bounded JSON response and apply filters."""
    url = apple_books_url(country, kind, chart)
    fetched = await _fetch_conditional(fetch_bytes, url, etag=etag, last_modified=last_modified)
    if fetched.body is None:
        if not etag and not last_modified:
            raise ProviderHttpError(304)
        return MediaFeedPage((), True, etag, last_modified)
    return MediaFeedPage(
        tuple(map_apple_books(_json_object(fetched.body), collected_at=collected_at, config=config, kind=kind)),
        False, _header_value(fetched.etag), _header_value(fetched.last_modified),
    )


async def collect_podcast_rss(
    fetch_bytes: Callable[..., Awaitable[Fetched]], *, feed_url: str | None = None, itunes_id: str | None = None,
    collected_at: datetime, config: Mapping[str, Any], etag: str | None = None, last_modified: str | None = None,
) -> MediaFeedPage:
    """Resolve an optional iTunes podcast ID and fetch one RSS feed with conditional caching and bounded mapping."""
    if (feed_url is None) == (itunes_id is None):
        raise ValueError("podcast_scope_invalid")
    if itunes_id is not None:
        lookup = await fetch_bytes(itunes_podcast_lookup_url(itunes_id), headers={"Accept": "application/json"}, max_bytes=MAX_FEED_BYTES)
        if lookup.body is None:
            raise ProviderHttpError(304)
        results = _json_object(lookup.body).get("results")
        resolved = next((row.get("feedUrl") for row in results if isinstance(row, Mapping) and isinstance(row.get("feedUrl"), str)), None) if isinstance(results, list) else None
        feed_url = podcast_feed_url(resolved)
    url = podcast_feed_url(feed_url)
    fetched = await _fetch_conditional(fetch_bytes, url, etag=etag, last_modified=last_modified)
    if fetched.body is None:
        if not etag and not last_modified:
            raise ProviderHttpError(304)
        return MediaFeedPage((), True, etag, last_modified)
    return MediaFeedPage(
        tuple(map_podcast_feed(fetched.body, collected_at=collected_at, config=config)),
        False, _header_value(fetched.etag), _header_value(fetched.last_modified),
    )
