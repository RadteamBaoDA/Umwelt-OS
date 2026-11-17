"""Bounded LiveChart episode RSS mapping for the gated media collection path."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from modules.connectors.provider_options import filter_media_records
from modules.connectors.providers import news
from modules.connectors.providers._media_common import normalize_genres, normalize_partial_date
from modules.connectors.providers.news import _header_value, _local
from modules.connectors.providers.rest import Fetched, ProviderHttpError
from modules.ingestion.public import IngestionRecord
from modules.knowledge.documents.public import ProviderRecordMetadata

LIVECHART_URL = "https://www.livechart.me/feeds/episodes"
MAX_ITEMS = 500


@dataclass(frozen=True)
class LiveChartPage:
    """One RSS response with validators suitable for atomic state persistence."""

    records: tuple[IngestionRecord, ...]
    not_modified: bool
    etag: str | None
    last_modified: str | None


def _text(item: ET.Element, name: str) -> str:
    """Read one direct RSS child by local name, including namespaced extension elements."""
    for child in item:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return ""


def map_livechart_feed(body: bytes, *, collected_at: datetime, config: Mapping[str, object]) -> list[IngestionRecord]:
    """Parse bounded LiveChart RSS into deduplicable anime episode records and apply media filters."""
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collected_at must include a timezone")
    root = news.safe_xml_root(body, max_bytes=news.MAX_FEED_BYTES)
    items = [node for node in root.iter() if _local(node.tag) == "item"]
    if len(items) > MAX_ITEMS:
        raise ValueError("livechart_response_invalid")
    records: dict[str, IngestionRecord] = {}
    for item in items:
        title = news.plain_text(_text(item, "title"), 500)
        link = news.https_link(_text(item, "link")) or ""
        guid = _text(item, "guid") or link
        parsed_url = urlsplit(link)
        if not title or not guid or parsed_url.scheme != "https" or parsed_url.hostname not in {"livechart.me", "www.livechart.me"}:
            continue
        date_text, year, air_time = normalize_partial_date(_text(item, "pubDate"))
        if not date_text or year is None:
            continue
        description = _text(item, "description")
        summary = news.plain_text(description, 4000)
        safe_guid = guid if len(guid) <= 480 and not any(ord(char) < 32 for char in guid) else sha256(guid.encode()).hexdigest()
        identity = f"livechart:{safe_guid}"
        fields: dict[str, object] = {
            "title": title,
            "media_type": "anime",
            "artwork_aspect": "poster",
            "release_date": date_text,
            "release_year": year,
            "release_status": "upcoming" if air_time and air_time > collected_at else "released",
            "attribution": "Data via LiveChart.me",
            "canonical_url": link,
        }
        genres = normalize_genres([child.text or "" for child in item if _local(child.tag) == "category"])
        if genres:
            fields["genres"] = genres
        raw_content = f"{title}\n\n{summary}".strip()
        metadata = ProviderRecordMetadata(
            provider="livechart_episodes",
            identity=identity,
            timestamp_basis="provider_published",
            coverage="returned_snapshot",
            content_truncated=len(raw_content) > 4000,
            source_fields=fields,
        ).model_dump(mode="json", exclude_none=True)
        records.setdefault(identity, IngestionRecord(
            provider_id=identity,
            content=raw_content[:4000],
            observed_at=air_time or collected_at,
            collected_at=collected_at,
            metadata={"provider": "livechart_episodes", "title": title, "canonical_url": link, "provider_record": metadata},
        ))
    return filter_media_records(config, list(records.values()), keep_missing=True)


async def collect_livechart_feed(
    fetch_bytes: Callable[..., Awaitable[Fetched]],
    *,
    collected_at: datetime,
    config: Mapping[str, object],
    etag: str | None = None,
    last_modified: str | None = None,
) -> LiveChartPage:
    """Fetch and map one fixed RSS feed through the caller's SendGate transport."""
    headers = {"Accept": "application/rss+xml, application/xml, text/xml"}
    etag, last_modified = _header_value(etag), _header_value(last_modified)
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    fetched = await fetch_bytes(LIVECHART_URL, headers=headers, max_bytes=news.MAX_FEED_BYTES)
    if fetched.body is None:
        if not etag and not last_modified:
            raise ProviderHttpError(304)
        return LiveChartPage((), True, etag, last_modified)
    records = map_livechart_feed(fetched.body, collected_at=collected_at, config=config)
    return LiveChartPage(tuple(records), False, _header_value(fetched.etag), _header_value(fetched.last_modified))
