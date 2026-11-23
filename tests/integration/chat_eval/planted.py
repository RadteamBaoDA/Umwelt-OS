"""Planted gold documents for the chat real-model eval, seeded through the real ingestion normalization.

``planted.json`` lists items grouped by ``source_key``. Each source gets one feed-shaped body that goes
through a production mapper (``map_feed`` for the news source, ``map_json_feed`` with the EMSC row for
quakes, and a test-local ``JsonFeed`` row for TVMaze-shaped media rows), then the matrix ``_ingest`` path.
Keys are the ``provider_id`` of each record; ``seed_planted`` resolves them to document version ids.
Timestamps are fixed; only ``media_time`` release dates depend on the run week (see ``seed_planted``).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from modules.connectors.providers.json_feed import JSON_FEEDS, JsonFeed, map_json_feed
from modules.connectors.providers.news import map_feed
from modules.ingestion.schemas import IngestionRecord
from tests.integration.test_chat_collected_data_matrix import (
    COLLECTED,
    Matrix,
    _create_source,
    _ingest,
    _set,
)

PLANTED_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "chat_eval" / "planted.json"
SETS = frozenset(
    {
        "paraphrase",
        "cross_lingual",
        "volume",
        "attribute",
        "media_time",
        "injection",
        "local_only",
    }
)
_LANGS = frozenset({"en", "vi"})
_REQUIRED = ("key", "set", "provider", "source_key", "title", "body", "lang", "published_at")
_API_PROVIDERS = frozenset({"emsc_quakes", "tvmaze_schedule"})
# Providers whose records come from json_feed.map_json_feed, which prefixes the identity with the provider.
_JSON_FEED_PROVIDERS = frozenset({"emsc_quakes", "tvmaze_schedule"})
_TVMAZE_FEED = JsonFeed(
    provider="tvmaze_schedule",
    publisher="TVMaze",
    url="https://api.tvmaze.com/schedule",
    items="results",
    id=("id",),
    title=("name",),
    summary=(("Release date", "release_date"),),
    time="observed_at",
)


@dataclass(frozen=True)
class PlantedItem:
    """One planted gold document; ``key`` doubles as the record provider id and must be unique."""

    key: str
    set: str
    provider: str
    source_key: str
    title: str
    body: str
    lang: str
    published_at: datetime
    attributes: dict[str, Any]


def load_planted(path: Path) -> list[PlantedItem]:
    """Parse ``planted.json`` into items; pure apart from reading ``path``.

    Raises ValueError for a missing item list, a missing or blank required field, an unknown set or
    language, a naive ``published_at`` (fixed timestamps must carry a UTC offset), or a duplicate key.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    rows = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        raise ValueError("planted.json must hold an 'items' list")
    seen: set[str] = set()
    items: list[PlantedItem] = []
    for row in rows:
        for field_name in _REQUIRED:
            if not isinstance(row.get(field_name), str) or not row[field_name].strip():
                raise ValueError(f"planted item {row.get('key')!r}: missing {field_name}")
        key = row["key"]
        if key in seen:
            raise ValueError(f"duplicate planted key {key!r}")
        seen.add(key)
        if row["set"] not in SETS or row["lang"] not in _LANGS:
            raise ValueError(f"planted item {key!r}: unknown set or lang")
        when = datetime.fromisoformat(row["published_at"])
        if when.tzinfo is None:
            raise ValueError(f"planted item {key!r}: published_at needs a UTC offset")
        items.append(
            PlantedItem(
                key=key,
                set=row["set"],
                provider=row["provider"],
                source_key=row["source_key"],
                title=row["title"],
                body=row["body"],
                lang=row["lang"],
                published_at=when,
                attributes=dict(row.get("attributes") or {}),
            )
        )
    return items


def _atom_body(items: Sequence[PlantedItem]) -> bytes:
    """Atom feed for news-shaped items; ``key`` is the entry id so ``map_feed`` keeps it as provider id."""
    entries = "".join(
        f"<entry><id>{escape(i.key)}</id><title>{escape(i.title)}</title>"
        f'<link rel="alternate" href="https://example.invalid/{escape(i.key)}"/>'
        f"<summary>{escape(i.body)}</summary>"
        f"<published>{i.published_at.isoformat()}</published>"
        f"<updated>{i.published_at.isoformat()}</updated></entry>"
        for i in items
    )
    xml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        f'<feed xmlns="http://www.w3.org/2005/Atom"><title>planted</title>{entries}</feed>'
    )
    return xml.encode("utf-8")


def _quake_body(items: Sequence[PlantedItem]) -> bytes:
    """EMSC-shaped FeatureCollection carrying the fields the ``emsc_quakes`` JSON row reads."""
    features = [
        {
            "type": "Feature",
            "id": i.key,
            "geometry": {"type": "Point", "coordinates": [106.0, 10.0, i.attributes["depth"]]},
            "properties": {
                "mag": i.attributes["magnitude"],
                "flynn_region": i.title,
                "time": i.published_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.0Z"),
                "lat": 10.0,
                "lon": 106.0,
                "depth": i.attributes["depth"],
            },
        }
        for i in items
    ]
    return json.dumps({"type": "FeatureCollection", "features": features}).encode("utf-8")


def _media_body(items: Sequence[PlantedItem], monday: date) -> bytes:
    """TVMaze-shaped rows; ``release_date`` is the run week's Monday plus the item's offset."""
    rows = [
        {
            "id": i.key,
            "name": i.title,
            "release_date": (
                monday + timedelta(days=int(i.attributes["release_week_offset_days"]))
            ).isoformat(),
            "observed_at": i.published_at.astimezone(UTC).isoformat(),
        }
        for i in items
    ]
    return json.dumps({"results": rows}).encode("utf-8")


def _records_for(items: Sequence[PlantedItem], monday: date) -> list[IngestionRecord]:
    """Feed-shaped bytes for one source's items, mapped by the production mapper of its provider."""
    provider = items[0].provider
    if provider == "emsc_quakes":
        return map_json_feed(JSON_FEEDS["emsc_quakes"], _quake_body(items), COLLECTED)
    if provider == "tvmaze_schedule":
        return map_json_feed(_TVMAZE_FEED, _media_body(items, monday), COLLECTED)
    return map_feed(provider, _atom_body(items), COLLECTED)


def external_id_for(item: PlantedItem) -> str:
    """Return the ``documents.external_id`` the normalizer stores for this planted item.

    ``json_feed.map_json_feed`` builds the record identity as ``<provider>:<id>`` (quakes and media),
    while the news mapper keeps the entry id verbatim. Keys are far below the 480-character limit where
    ``news._stable_id`` would switch to a SHA-256 digest, so the id is never hashed here.
    """
    if item.provider in _JSON_FEED_PROVIDERS:
        return f"{item.provider}:{item.key}"
    return item.key


async def _resolve_versions(
    engine: AsyncEngine, matrix: Matrix, items: Sequence[PlantedItem]
) -> dict[str, str]:
    """Map each planted key to the current document version id of its document in its source.

    ``external_id`` is the record provider id as produced by the mapper (see ``external_id_for``).
    Items with no document are left out; the caller decides whether that is an error.
    """
    out: dict[str, str] = {}
    async with engine.connect() as conn:
        for item in items:
            row = (
                await conn.execute(
                    text(
                        "SELECT v.id FROM documents d JOIN document_versions v "
                        "ON v.document_id = d.id AND v.version_number = d.current_version "
                        "WHERE d.source_id = :s AND d.external_id = :k"
                    ),
                    {"s": str(matrix.sources[item.source_key]), "k": external_id_for(item)},
                )
            ).scalar_one_or_none()
            if row is not None:
                out[item.key] = str(row)
    return out


async def seed_planted(
    client: AsyncClient,
    engine: AsyncEngine,
    m: Matrix,
    items: Sequence[PlantedItem],
) -> dict[str, str]:
    """Create one source per ``source_key``, ingest its items, and return key -> document_version_id.

    Sources are created through the matrix helper so their keys land in ``m.sources`` (scope lookups
    use it). Items flagged ``attributes.local_only`` switch their source to local-only after ingestion.
    The media release dates use the run week's Monday, so only that field depends on the clock.
    Raises RuntimeError naming every key that has no document version after ingestion.
    """
    now = datetime.now(UTC)
    monday = now.date() - timedelta(days=now.weekday())
    groups: dict[str, list[PlantedItem]] = {}
    for item in items:
        groups.setdefault(item.source_key, []).append(item)
    for source_key, group in groups.items():
        providers = {i.provider for i in group}
        if len(providers) != 1:
            raise ValueError(f"planted source {source_key!r} mixes providers {sorted(providers)}")
        provider = group[0].provider
        source_type = "api" if provider in _API_PROVIDERS else "rss"
        await _create_source(
            client, engine, m, source_key, provider=provider, source_type=source_type
        )
        await _ingest(engine, m, source_key, _records_for(group, monday))
        if any(i.attributes.get("local_only") is True for i in group):
            await _set(engine, m, source_key, "local_only = true")
    resolved = await _resolve_versions(engine, m, items)
    missing = sorted(i.key for i in items if i.key not in resolved)
    if missing:
        raise RuntimeError(f"planted documents without a version id: {', '.join(missing)}")
    return resolved
