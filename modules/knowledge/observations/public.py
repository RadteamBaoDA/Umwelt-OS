"""Owner-scoped observation revision writes, selection and bounded reads."""

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import Table, and_, func, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.models import Owner
from modules.connectors import public as connectors
from modules.knowledge.documents import public as documents
from modules.knowledge.observations.models import Observation
from modules.knowledge.observations.schemas import (
    GeospatialObservationPage,
    GeospatialObservationRead,
    ObservationExportFence,
    ObservationExportFenceValidation,
    ObservationExportPage,
    ObservationExportRead,
    ObservationQuery,
    WorldMeasurement,
)
from modules.sources import public as sources
from modules.sources.schemas import SourceExportFence

OBSERVATION_EXPORT_PAGE_MAX_BYTES = 16_777_216


def _observation_export_cursor(owner_id: int, snapshot_at: datetime, accepted_at: datetime, row_id: UUID) -> str:
    """Encode the fixed owner, cutoff and accepted-time/ID keyset position."""
    value = {"v": 1, "owner": owner_id, "kind": "observations", "snapshot": snapshot_at.astimezone(UTC).isoformat(),
             "at": accepted_at.astimezone(UTC).isoformat(), "id": str(row_id)}
    return base64.urlsafe_b64encode(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).decode().rstrip("=")


def _decode_observation_export_cursor(cursor: str, owner_id: int) -> tuple[datetime, datetime, UUID]:
    """Validate a canonical cutoff-bound cursor and reject cross-owner reuse."""
    try:
        if not cursor or len(cursor) > 1024 or "=" in cursor:
            raise ValueError
        value = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
        if not isinstance(value, dict) or set(value) != {"v", "owner", "kind", "snapshot", "at", "id"}:
            raise ValueError
        if value["v"] != 1 or value["owner"] != owner_id or value["kind"] != "observations":
            raise ValueError
        snapshot, position = datetime.fromisoformat(value["snapshot"]), datetime.fromisoformat(value["at"])
        if any(item.tzinfo is None or item.utcoffset() is None for item in (snapshot, position)):
            raise ValueError
        snapshot, position = snapshot.astimezone(UTC), position.astimezone(UTC)
        row_id = UUID(value["id"])
        if snapshot > datetime.now(UTC) or _observation_export_cursor(owner_id, snapshot, position, row_id) != cursor:
            raise ValueError
        return snapshot, position, row_id
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid observation export cursor") from exc


def _observation_export_digest(item: ObservationExportRead) -> str:
    """Hash the exact allowlisted portable record rather than internal ORM state."""
    return hashlib.sha256(json.dumps(item.model_dump(mode="json"), sort_keys=True,
                                     separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


async def _observation_export_count(session: AsyncSession, snapshot_at: datetime) -> int:
    """Count only rows whose current source scope and exact document version remain valid."""
    # ponytail: exact public evidence filtering scans O(N) per page; replace with a Documents-owned
    # SQL eligibility count only when capacity evidence shows this bounded-memory path is too slow.
    base = select(Observation).where(
        Observation.is_current.is_(True), Observation.accepted_at <= snapshot_at,
        Observation.source_id.in_(sources.export_eligible_source_ids()),
    )
    count, position = 0, None
    while True:
        statement = base
        if position is not None:
            statement = statement.where(tuple_(Observation.accepted_at, Observation.id) > position)
        rows = list((await session.scalars(statement.order_by(Observation.accepted_at, Observation.id)
                                           .limit(256).execution_options(populate_existing=True))).all())
        if not rows:
            return count
        scope_by_source: dict[UUID, Any] = {}
        candidates = []
        for row in rows:
            scope = scope_by_source.get(row.source_id)
            if row.source_id not in scope_by_source:
                source = await sources.get_connector_source(session, row.source_id)
                scope = (await connectors.export_provider_scope(session, row.source_id, source.generation)
                         if source is not None else None)
                scope_by_source[row.source_id] = scope
            if scope is not None and scope.provider_id == row.provider:
                candidates.append((documents.ObservationExportEvidenceCandidate(
                    observation_id=row.id, source_id=row.source_id, accepted_source_generation=row.source_generation,
                    provider=row.provider, provider_scope_discriminator=row.provider_scope_discriminator,
                    external_id=row.external_id, document_id=row.document_id, document_version_id=row.document_version_id,
                ), scope))
        for candidate, scope in candidates:
            count += int(await documents.export_observation_evidence(session, candidate, scope) is not None)
        position = (rows[-1].accepted_at, rows[-1].id)


async def export_page(session: AsyncSession, *, owner_id: int, record_kind: str, limit: int = 50,
                      cursor: str | None = None) -> ObservationExportPage:
    """Export bounded current measurements; require current source scope and exact retained document evidence."""
    if record_kind != "observations" or not 1 <= limit <= 100:
        raise ValueError("Observation export kind or limit is invalid")
    if owner_id != 1 or await session.scalar(select(Owner.id).where(Owner.id == owner_id)) is None:
        raise PermissionError("Observation export requires the current owner")
    if cursor is None:
        snapshot, position = datetime.now(UTC), None
    else:
        snapshot, position_at, position_id = _decode_observation_export_cursor(cursor, owner_id)
        position = (position_at, position_id)
    count = await _observation_export_count(session, snapshot)
    statement = select(Observation).where(
        Observation.is_current.is_(True), Observation.accepted_at <= snapshot,
        Observation.source_id.in_(sources.export_eligible_source_ids()),
    )
    if position is not None:
        statement = statement.where(tuple_(Observation.accepted_at, Observation.id) > position)
    rows = list((await session.scalars(statement.order_by(Observation.accepted_at, Observation.id)
                                       .limit(limit + 1).execution_options(populate_existing=True))).all())
    more = len(rows) > limit
    items: list[ObservationExportRead] = []
    fences: list[ObservationExportFence] = []
    last_examined: tuple[datetime, UUID] | None = None
    for row in rows[:limit]:
        source = await sources.get_connector_source(session, row.source_id)
        if source is None:
            last_examined = (row.accepted_at, row.id)
            continue
        scope = await connectors.export_provider_scope(session, row.source_id, source.generation)
        if scope is None or scope.provider_id != row.provider:
            last_examined = (row.accepted_at, row.id)
            continue
        evidence = await documents.export_observation_evidence(session, documents.ObservationExportEvidenceCandidate(
            observation_id=row.id, source_id=row.source_id, accepted_source_generation=row.source_generation,
            provider=row.provider, provider_scope_discriminator=row.provider_scope_discriminator,
            external_id=row.external_id, document_id=row.document_id, document_version_id=row.document_version_id,
        ), scope)
        if evidence is None:
            last_examined = (row.accepted_at, row.id)
            continue
        item = ObservationExportRead(
            id=row.id, source_id=row.source_id, source_generation=row.source_generation,
            provider=row.provider, external_id=row.external_id, revision=row.revision, metric=row.metric,
            symbol=row.symbol, region=row.region, latitude=row.latitude, longitude=row.longitude,
            observed_at=row.observed_at, published_at=row.published_at, collected_at=row.collected_at,
            accepted_at=row.accepted_at, value=row.value, unit=row.unit, currency=row.currency,
            timezone=row.timezone, quality=row.quality, missing_reason=row.missing_reason,
            document_id=row.document_id, document_version_id=row.document_version_id,
        )
        proposed = items + [item]
        size = len(json.dumps([value.model_dump(mode="json") for value in proposed], ensure_ascii=False,
                              separators=(",", ":")).encode())
        if size > OBSERVATION_EXPORT_PAGE_MAX_BYTES:
            if not items:
                raise ValueError("An observation export record exceeds the page byte budget")
            more = True
            break
        items.append(item)
        last_examined = (row.accepted_at, row.id)
        fences.append(ObservationExportFence(
            id=row.id, source_id=row.source_id, accepted_source_generation=row.source_generation,
            current_source_generation=evidence.current_source_generation,
            provider_scope_digest=hashlib.sha256(scope.discriminator.encode()).hexdigest(),
            record_digest=_observation_export_digest(item),
        ))
    next_cursor = (_observation_export_cursor(owner_id, snapshot, *last_examined)
                   if more and last_examined is not None else None)
    payload = len(json.dumps([item.model_dump(mode="json") for item in items], ensure_ascii=False,
                             separators=(",", ":")).encode())
    return ObservationExportPage(owner_id=owner_id, snapshot_at=snapshot, snapshot_count=count,
                                 items=items, fences=fences, payload_bytes=payload, next_cursor=next_cursor)


async def validate_export_fences(session: AsyncSession, *, owner_id: int, record_kind: str,
                                 snapshot_at: datetime, expected_snapshot_count: int,
                                 fences: list[ObservationExportFence]) -> ObservationExportFenceValidation:
    """Recheck snapshot count, current provider scope, generation and exact exported fields."""
    if record_kind != "observations" or len(fences) > 100 or expected_snapshot_count < 0:
        raise ValueError("Observation export validation input is invalid")
    if owner_id != 1 or await session.scalar(select(Owner.id).where(Owner.id == owner_id)) is None:
        return ObservationExportFenceValidation(valid=False, reason="owner_unavailable", observed_snapshot_count=0)
    observed = await _observation_export_count(session, snapshot_at)
    if observed != expected_snapshot_count:
        return ObservationExportFenceValidation(valid=False, reason="snapshot_count_changed", observed_snapshot_count=observed)
    for fence in fences:
        row = await session.scalar(select(Observation).where(Observation.id == fence.id).execution_options(populate_existing=True))
        if (row is None or row.source_id != fence.source_id
                or row.source_generation != fence.accepted_source_generation):
            return ObservationExportFenceValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
        source = await sources.get_connector_source(session, row.source_id)
        scope = (await connectors.export_provider_scope(session, row.source_id, source.generation)
                 if source is not None else None)
        if (source is None or source.generation != fence.current_source_generation or scope is None
                or scope.provider_id != row.provider
                or hashlib.sha256(scope.discriminator.encode()).hexdigest() != fence.provider_scope_digest):
            return ObservationExportFenceValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
        item = ObservationExportRead(
            id=row.id, source_id=row.source_id, source_generation=row.source_generation, provider=row.provider,
            external_id=row.external_id, revision=row.revision, metric=row.metric, symbol=row.symbol, region=row.region,
            latitude=row.latitude, longitude=row.longitude, observed_at=row.observed_at, published_at=row.published_at,
            collected_at=row.collected_at, accepted_at=row.accepted_at, value=row.value, unit=row.unit,
            currency=row.currency, timezone=row.timezone, quality=row.quality, missing_reason=row.missing_reason,
            document_id=row.document_id, document_version_id=row.document_version_id,
        )
        evidence = await documents.export_observation_evidence(session, documents.ObservationExportEvidenceCandidate(
            observation_id=row.id, source_id=row.source_id, accepted_source_generation=row.source_generation,
            provider=row.provider, provider_scope_discriminator=row.provider_scope_discriminator,
            external_id=row.external_id, document_id=row.document_id, document_version_id=row.document_version_id,
        ), scope)
        eligible = await sources.filter_export_eligible_sources(session, [SourceExportFence(
            source_id=fence.source_id, generation=fence.current_source_generation,
        )])
        if (not row.is_current or row.accepted_at > snapshot_at or row.source_id not in eligible
                or evidence is None or evidence.current_source_generation != fence.current_source_generation):
            return ObservationExportFenceValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
        if _observation_export_digest(item) != fence.record_digest:
            return ObservationExportFenceValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
    return ObservationExportFenceValidation(valid=True, reason="valid", observed_snapshot_count=observed)


@dataclass(frozen=True)
class ObservationWriteResult:
    """Expose only the accepted row identity and whether current series output changed."""
    observation_id: UUID
    selected_current: bool
    current_changed: bool


@dataclass(frozen=True)
class ObservationScopeRead:
    """Pair each selected point with the public current source and provider scope fence."""
    source_id: UUID
    source_generation: int
    provider: str
    discriminator: str


async def upsert_from_ingestion(
    session: AsyncSession, *, source_id: UUID, source_generation: int,
    ingestion_observation_id: UUID, document_id: UUID, document_version_id: UUID,
    external_id: str, provider_version: str | None, observed_at: datetime,
    collected_at: datetime, accepted_at: datetime, provider_scope_discriminator: str,
    measurement: WorldMeasurement,
) -> ObservationWriteResult | None:
    """Insert an immutable revision and choose current by accepted time plus ingestion ID.

    The caller keeps this write in the document normalization transaction and,
    when this row wins, projects its exact version through Documents' public
    selection contract before committing.
    """
    source = await sources.lock_source(session, source_id)
    if source is None or source.status != "active" or source.generation != source_generation:
        return None
    normalized = WorldMeasurement.model_validate(measurement.model_dump(mode="python"))
    scope = await connectors.get_current_provider_scope(session, source_id, source_generation)
    if (
        scope is None or scope.provider_id != normalized.provider
        or scope.discriminator != provider_scope_discriminator
        or accepted_at.tzinfo is None or accepted_at.utcoffset() is None
    ):
        return None
    digest = hashlib.sha256(json.dumps(
        {**normalized.model_dump(mode="json"), "observed_at": observed_at.astimezone(UTC).isoformat()},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()
    existing = await session.scalar(select(Observation).where(
        Observation.source_id == source_id,
        Observation.external_id == external_id,
        Observation.ingestion_observation_id == ingestion_observation_id,
    ).with_for_update())
    if existing is not None:
        if existing.content_hash != digest:
            raise ValueError("ingestion observation was reused for different provider content")
        return ObservationWriteResult(existing.id, existing.is_current, False)
    current = await session.scalar(select(Observation).where(
        Observation.source_id == source_id,
        Observation.external_id == external_id,
        Observation.is_current.is_(True),
    ).order_by(Observation.revision.desc()).limit(1).with_for_update())
    highest_revision = int(await session.scalar(select(func.max(Observation.revision)).where(
        Observation.source_id == source_id, Observation.external_id == external_id,
    )) or 0)
    revision = highest_revision + 1
    accepted_utc = accepted_at.astimezone(UTC)
    is_current = current is None or (accepted_utc, str(ingestion_observation_id)) > (
        current.accepted_at.astimezone(UTC), str(current.ingestion_observation_id),
    )
    if current is not None and is_current:
        current.is_current = False
    row = Observation(
        source_id=source_id, external_id=external_id, provider=normalized.provider,
        provider_scope_discriminator=scope.discriminator,
        provider_version=provider_version, revision=revision,
        metric=normalized.metric, symbol=normalized.symbol, region=normalized.region,
        latitude=normalized.latitude, longitude=normalized.longitude,
        observed_at=observed_at, published_at=normalized.published_at,
        collected_at=collected_at, accepted_at=accepted_utc, value=normalized.value, unit=normalized.unit,
        currency=normalized.currency, quality=normalized.quality, missing_reason=normalized.missing_reason,
        timezone=normalized.timezone,
        provider_delay_seconds=None,
        is_current=is_current, content_hash=digest, document_id=document_id,
        document_version_id=document_version_id,
        ingestion_observation_id=ingestion_observation_id, source_generation=source_generation,
    )
    session.add(row)
    await session.flush()
    return ObservationWriteResult(row.id, is_current, is_current)


def _cursor_fingerprint(query: ObservationQuery, scopes: tuple[ObservationScopeRead, ...]) -> str:
    """Bind continuation cursors to exact normalized filters and half-open date bounds."""
    payload = {
        "filters": query.model_dump(mode="json", exclude={"limit"}),
        "scopes": [
            [str(item.source_id), item.source_generation, item.provider, item.discriminator]
            for item in scopes
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _decode_cursor(cursor: str, fingerprint: str) -> tuple[datetime, UUID]:
    """Decode one bounded cursor and reject malformed or cross-filter reuse."""
    if len(cursor) > 2048:
        raise HTTPException(status_code=422, detail="Observation cursor is invalid")
    try:
        raw = base64.b64decode(cursor.encode() + b"=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        identity, stamp, row_id = json.loads(raw)
        if identity != fingerprint:
            raise ValueError
        parsed = datetime.fromisoformat(stamp)
        parsed_id = UUID(row_id)
        if parsed.tzinfo is None or parsed.utcoffset() is None or str(parsed_id) != row_id:
            raise ValueError
        return parsed.astimezone(UTC), parsed_id
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail="Observation cursor is invalid") from exc


async def list_observations(
    session: AsyncSession, query: ObservationQuery, cursor: str | None = None,
) -> tuple[tuple[Observation, ...], str | None, bool, dict[UUID, int]]:
    """Read only current-generation, current-provider-scope, current-evidence points under bounded scanning."""
    scopes: list[ObservationScopeRead] = []
    current_scopes: dict[UUID, object] = {}
    for source_id in sorted(query.source_ids, key=str):
        source = await sources.get_connector_source(session, source_id)
        snapshot = await connectors.get_current_provider_scope(
            session, source_id,
            source.generation if source is not None and source.status == "active" else -1,
        )
        if snapshot is None:
            raise HTTPException(status_code=404, detail="One or more sources are unavailable")
        current_scopes[source_id] = snapshot
        scopes.append(ObservationScopeRead(
            source_id=snapshot.source_id, source_generation=snapshot.source_generation,
            provider=snapshot.provider_id, discriminator=snapshot.discriminator,
        ))
    source_scope_filters = tuple(and_(
        Observation.source_id == item.source_id,
        Observation.source_generation == item.source_generation,
        Observation.provider == item.provider,
        Observation.provider_scope_discriminator == item.discriminator,
    ) for item in scopes)
    statement = select(Observation).where(
        or_(*source_scope_filters), Observation.is_current.is_(True),
        Observation.observed_at >= query.from_at, Observation.observed_at < query.to_at,
    )
    if query.geospatial_only:
        # Do not infer point coordinates from a location label; only provider-recorded points map.
        statement = statement.where(
            Observation.latitude.is_not(None), Observation.longitude.is_not(None),
        )
    if query.metrics:
        statement = statement.where(Observation.metric.in_(query.metrics))
    if query.symbols:
        statement = statement.where(Observation.symbol.in_(query.symbols))
    if query.regions:
        statement = statement.where(Observation.region.in_(query.regions))
    scopes_tuple = tuple(scopes)
    fingerprint = _cursor_fingerprint(query, scopes_tuple)
    if cursor:
        stamp, row_id = _decode_cursor(cursor, fingerprint)
        statement = statement.where(tuple_(Observation.observed_at, Observation.id) < tuple_(stamp, row_id))
    page: list[Observation] = []
    scanned = 0
    last_scanned: Observation | None = None
    page_version_numbers: dict[UUID, int] = {}
    scan_truncated = False
    while scanned < 2_048 and len(page) <= query.limit:
        batch = tuple((await session.scalars(
            statement.order_by(Observation.observed_at.desc(), Observation.id.desc())
            .limit(min(256, 2_048 - scanned))
        )).all())
        if not batch:
            break
        candidates = tuple(documents.ObservationEvidenceCandidate(
            observation_id=row.id, source_id=row.source_id, source_generation=row.source_generation,
            provider=row.provider, provider_scope_discriminator=row.provider_scope_discriminator,
            external_id=row.external_id, document_id=row.document_id,
            document_version_id=row.document_version_id,
        ) for row in batch)
        allowed = await documents.current_observation_evidence_versions(session, candidates, current_scopes)
        for row in batch:
            if row.id in allowed:
                page.append(row)
                page_version_numbers[row.id] = allowed[row.id]
                if len(page) > query.limit:
                    break
        scanned += len(batch)
        last_scanned = batch[-1]
        statement = statement.where(tuple_(Observation.observed_at, Observation.id) < tuple_(last_scanned.observed_at, last_scanned.id))
        if len(batch) < min(256, 2_048 - (scanned - len(batch))):
            break
        if scanned >= 2_048:
            scan_truncated = True
    has_more = len(page) > query.limit
    page = page[:query.limit]
    page_version_numbers = {row.id: page_version_numbers[row.id] for row in page}
    next_cursor = None
    if has_more and page:
        last = page[-1]
        payload = json.dumps([fingerprint, last.observed_at.astimezone(UTC).isoformat(), str(last.id)], separators=(",", ":")).encode()
        next_cursor = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    elif scan_truncated and last_scanned is not None:
        last = last_scanned
        payload = json.dumps([fingerprint, last.observed_at.astimezone(UTC).isoformat(), str(last.id)], separators=(",", ":")).encode()
        next_cursor = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return tuple(page), next_cursor, scan_truncated, page_version_numbers


async def list_geospatial_observations(
    session: AsyncSession, query: ObservationQuery, cursor: str | None = None,
) -> GeospatialObservationPage:
    """Return at most one page of current Open-Meteo point evidence for valid selected sources.

    Non-weather and inactive sources are counted as omitted rather than causing a
    broad query failure. The underlying query still applies the standard source,
    provider-scope, generation, current revision and exact-document evidence fences.
    """
    if not query.source_ids:
        raise ValueError("At least one map source is required")
    eligible: list[UUID] = []
    omitted = 0
    for source_id in query.source_ids:
        source = await sources.get_connector_source(session, source_id)
        snapshot = await connectors.get_current_provider_scope(
            session, source_id,
            source.generation if source is not None and source.status == "active" else -1,
        )
        # Only the existing weather adapter declares point coordinates; market symbols have none.
        if snapshot is None or snapshot.provider_id != "open_meteo":
            omitted += 1
        else:
            eligible.append(source_id)
    if not eligible:
        return GeospatialObservationPage(
            items=[], next_cursor=None, truncated=False, omitted_source_count=omitted,
        )
    scoped_query = query.model_copy(update={
        "source_ids": eligible, "geospatial_only": True,
    })
    rows, next_cursor, truncated, version_numbers = await list_observations(session, scoped_query, cursor)
    version_numbers_by_observation = version_numbers
    items = [GeospatialObservationRead.model_validate({
        **{key: getattr(row, key) for key in GeospatialObservationRead.model_fields if hasattr(row, key)},
        "document_version_number": version_numbers_by_observation.get(row.id),
    }) for row in rows]
    return GeospatialObservationPage(
        items=items, next_cursor=next_cursor, truncated=truncated,
        omitted_source_count=omitted,
    )


async def purge_source_in_uow(session: AsyncSession, source_id: UUID) -> None:
    """Remove every derived observation revision in the source's existing deletion transaction."""
    await session.execute(update(Observation).where(Observation.source_id == source_id).values(is_current=False))
    await session.execute(
        cast(Table, Observation.__table__).delete().where(Observation.source_id == source_id)
    )


async def purge_document_in_uow(session: AsyncSession, document_id: UUID) -> None:
    """Remove evidence-supported measurement revisions before their document is deleted."""
    await session.execute(
        cast(Table, Observation.__table__).delete().where(Observation.document_id == document_id)
    )
