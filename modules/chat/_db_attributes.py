"""Record attributes, row serialisation and discovery helpers for the chat collected-data tool.

Provider facts that have no column (quake depth, KEV vendor, HN score, airdate, OHLC...) live in the
current version's ``normalized_version_provenance.provenance_json`` at
``provider_record.source_fields`` (allowlisted per provider) and, for measurement providers, at
``provider_record.world_data.provider_fields``. This module reads both through the knowledge.documents
read projection, in ONE query per call, and bounds what reaches the model.
"""

from __future__ import annotations

import json
from contextlib import suppress
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.chat._db_scope import observation_scope_predicates
from modules.knowledge.documents.public import (
    DocumentChunkReadProjection as DocumentChunk,
)
from modules.knowledge.documents.public import (
    DocumentReadProjection as Document,
)
from modules.knowledge.documents.public import (
    NormalizedVersionProvenanceReadProjection as NormalizedVersionProvenance,
)
from modules.knowledge.observations.public import ObservationReadProjection as Observation

MAX_ATTRIBUTE_KEYS = 20
MAX_ATTRIBUTE_VALUE_CHARS = 200
MAX_ATTRIBUTE_LIST_ITEMS = 10
MAX_ATTRIBUTES_CHARS = 1000
MAX_AVAILABLE_SERIES = 50
# Keys that duplicate a column the row already carries, never shown again as attributes.
_COLUMN_KEYS = frozenset({"title", "summary", "body", "canonical_url", "published_at"})
# Substrings that mark a credential-like key; none are expected in allowlisted fields, never surfaced anyway.
_SECRET_MARKERS = ("token", "secret", "password", "key")


def _scalar(value: Any) -> Any:
    """Return ``value`` if it is a bounded JSON scalar (strings cut), else the sentinel ``...``."""
    if isinstance(value, bool) or value is None or isinstance(value, int | float):
        return value
    if isinstance(value, str):
        return value[:MAX_ATTRIBUTE_VALUE_CHARS]
    return ...


def _bounded_value(value: Any) -> Any:
    """Return a scalar or a short list of scalars (bounded), or ``...`` when the value is not representable."""
    if isinstance(value, list):
        items = [_scalar(v) for v in value[:MAX_ATTRIBUTE_LIST_ITEMS]]
        return ... if any(v is ... for v in items) else items
    return _scalar(value)


def bound_attributes(*field_maps: Any) -> dict[str, Any]:
    """Merge provenance field maps into one bounded, secret-free attribute dict (pure).

    Later maps override earlier ones. Keeps scalars and short scalar lists only, cuts strings, skips keys that
    duplicate a row column or look like credentials, keeps at most ``MAX_ATTRIBUTE_KEYS`` keys and stops
    adding keys once the serialized dict would exceed ``MAX_ATTRIBUTES_CHARS`` characters.
    """
    merged: dict[str, Any] = {}
    for fields in field_maps:
        if isinstance(fields, dict):
            merged.update(fields)
    out: dict[str, Any] = {}
    size = 2
    for key, raw in merged.items():
        name = str(key)
        if name.lower() in _COLUMN_KEYS or any(m in name.lower() for m in _SECRET_MARKERS):
            continue
        value = _bounded_value(raw)
        if value is ... or len(out) >= MAX_ATTRIBUTE_KEYS:
            continue
        size += len(json.dumps({name: value}, ensure_ascii=False, default=str))
        if size > MAX_ATTRIBUTES_CHARS:
            break
        out[name] = value
    return out


async def fetch_attributes(
    session: AsyncSession, version_ids: list[UUID | None]
) -> dict[UUID, dict[str, Any]]:
    """Bounded attributes per document version id, from ONE batched provenance query (read-only).

    Versions without provenance or without usable fields are absent from the result. Callers pass versions
    already fenced by the scope filters of the rows that produced them.
    """
    ids = list({v for v in version_ids if v is not None})
    if not ids:
        return {}
    record = NormalizedVersionProvenance.provenance_json["provider_record"]
    rows = (
        await session.execute(
            select(
                NormalizedVersionProvenance.document_version_id,
                record["source_fields"],
                record["world_data"]["provider_fields"],
            ).where(NormalizedVersionProvenance.document_version_id.in_(ids))
        )
    ).all()
    result: dict[UUID, dict[str, Any]] = {}
    for version_id, source_fields, provider_fields in rows:
        attrs = bound_attributes(source_fields, provider_fields)
        if attrs:
            result.setdefault(cast(UUID, version_id), attrs)
    return result


def serialize_observations(
    obs_rows: Any,
    citation_map: dict[UUID, int],
    attributes: dict[UUID, dict[str, Any]],
    source_names: dict[UUID, str] | None = None,
) -> list[dict[str, Any]]:
    """Serialise observation rows with ``[n]`` citation markers, coordinates and bounded ``attributes``.

    ``source_name`` is the owner's label for the row's source; it is often the only place-name a
    coordinate- or station-keyed series carries (open_meteo ``lat,lon``, openaq ``location <id>``).
    """
    names: dict[Any, str] = dict(source_names or {})
    return [
        {
            "provider": obs.provider,
            "source_name": names.get(getattr(obs, "source_id", None)),
            "metric": obs.metric,
            "symbol": obs.symbol,
            "region": obs.region,
            "value": obs.value,
            "unit": obs.unit,
            "currency": obs.currency,
            "latitude": getattr(obs, "latitude", None),
            "longitude": getattr(obs, "longitude", None),
            "observed_at": obs.observed_at.isoformat(),
            "is_current": obs.is_current,
            "attributes": attributes.get(obs.document_version_id, {}),
            "citation": (
                f"[{citation_map[obs.document_version_id]}]"
                if obs.document_version_id in citation_map
                else None
            ),
        }
        for obs in obs_rows
    ]


def serialize_documents(
    doc_rows: list[Any], citation_map: dict[UUID, int], attributes: dict[UUID, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Serialise structured document rows with ``[n]`` citation markers and bounded ``attributes``."""
    return [
        {
            "provider": d.provider,
            "source_name": d.source_name,
            "title": d.title,
            "url": d.url,
            "published_at": d.published_at.isoformat() if d.published_at else None,
            "observed_at": d.observed_at.isoformat() if d.observed_at else None,
            "excerpt": d.excerpt,
            "attributes": attributes.get(d.document_version_id, {}),
            "citation": (
                f"[{citation_map[d.document_version_id]}]"
                if d.document_version_id in citation_map
                else None
            ),
        }
        for d in doc_rows
    ]


async def available_series(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    allowed_source_ids: list[UUID],
    source_scopes: dict[UUID, str | None] | None,
) -> list[dict[str, Any]]:
    """Up to 50 DISTINCT (provider, metric, symbol, region) among the allowed current observations.

    Discovery aid for an empty observation result: same workspace/source/scope fences as the data query but
    none of the caller's filters, so the model can pick a series that exists. Read-only, one grouped SELECT.
    """
    rows = (
        await session.execute(
            select(Observation.provider, Observation.metric, Observation.symbol, Observation.region)
            .where(
                *observation_scope_predicates(
                    Observation,
                    workspace_id=workspace_id,
                    allowed_source_ids=allowed_source_ids,
                    source_scopes=source_scopes,
                ),
                Observation.is_current.is_(True),
            )
            .group_by(
                Observation.provider, Observation.metric, Observation.symbol, Observation.region
            )
            .order_by(Observation.provider, Observation.metric, Observation.symbol)
            .limit(MAX_AVAILABLE_SERIES)
        )
    ).all()
    return [
        {"provider": r.provider, "metric": r.metric, "symbol": r.symbol, "region": r.region}
        for r in rows
    ]


def _unaccent(expr: Any) -> Any:
    """``public.f_unaccent(expr)``: the IMMUTABLE wrapper installed by migration 0025 (matches its indexes)."""
    return func.public.f_unaccent(expr)


def escape_like(raw: str) -> str:
    """Escape ``\\``, ``%`` and ``_`` so user text matches literally inside an ``ILIKE`` pattern."""
    return raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def text_match_filter(query: str, version_id_column: Any) -> Any:
    """Accent-insensitive keyword predicate: any chunk of the version, or the title, matches ``query``.

    Chunks use ``to_tsvector('simple', f_unaccent(content)) @@ websearch_to_tsquery('simple',
    f_unaccent(q))`` (served by the GIN index of migration 0025); the title uses
    ``f_unaccent(title) ILIKE f_unaccent('%q%')`` with LIKE metacharacters escaped. So 'gia vang' matches
    'giá vàng' and the reverse.

    The chunk side is an uncorrelated ``IN`` subquery with a literal ``'simple'::regconfig`` so its
    expression is byte-identical to the index expression: a bound config parameter or a correlated
    ``EXISTS`` would make the planner recompute ``to_tsvector`` per chunk instead of using the GIN index.
    """
    simple: Any = literal_column("'simple'::regconfig")
    ts_query = func.websearch_to_tsquery(simple, _unaccent(query))
    chunk_match = version_id_column.in_(
        select(DocumentChunk.document_version_id).where(
            func.to_tsvector(simple, _unaccent(DocumentChunk.content)).op("@@")(ts_query)
        )
    )
    title_match = _unaccent(Document.title).ilike(_unaccent(f"%{escape_like(query)}%"), escape="\\")
    return or_(chunk_match, title_match)


async def hot_cache_snapshots(
    redis: Any, workspace_id: UUID, allowed_source_ids: list[UUID]
) -> list[dict[str, Any]]:
    """Read up to 8 connector snapshot metadata entries from Redis with one ``MGET``; failure yields nothing.

    The snapshots are a best-effort enrichment, so every error is suppressed (never raises).
    """
    hot_snapshots: list[dict[str, Any]] = []
    keys = [f"connector:snapshot:{workspace_id}:{sid}" for sid in allowed_source_ids[:8]]
    if not keys:
        return hot_snapshots
    with suppress(Exception):
        for raw_snap in await redis.mget(keys):
            if raw_snap:
                snap = json.loads(
                    raw_snap.decode("utf-8") if isinstance(raw_snap, bytes) else str(raw_snap)
                )
                if isinstance(snap, dict):
                    hot_snapshots.append(
                        {
                            "provider": snap.get("provider"),
                            "realtime_tier": snap.get("realtime_tier"),
                            "collected_at": snap.get("collected_at"),
                            "record_count": snap.get("record_count"),
                        }
                    )
    return hot_snapshots


def evidence_pairs(aggregates: list[dict[str, Any]], obs_rows: list[Any]) -> list[tuple[Any, Any]]:
    """``(document_id, version_id)`` pairs to cite: newest observation per aggregate, then each row (pure)."""
    pairs = [
        (agg["_document_id"], agg["_version_id"])
        for agg in aggregates
        if agg["_document_id"] is not None and agg["_version_id"] is not None
    ]
    return pairs + [(obs.document_id, obs.document_version_id) for obs in obs_rows]
