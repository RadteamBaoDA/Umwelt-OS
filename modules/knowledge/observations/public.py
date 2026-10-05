"""Owner-scoped observation revision writes, selection and bounded reads."""

from datetime import UTC, datetime
import base64
import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from modules.knowledge.observations.models import Observation
from modules.knowledge.observations.schemas import ObservationQuery, WorldMeasurement
from modules.connectors import public as connectors
from modules.knowledge.documents import public as documents
from modules.sources import public as sources


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
) -> tuple[tuple[Observation, ...], str | None, bool]:
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
        allowed = await documents.current_observation_evidence_ids(session, candidates, current_scopes)
        for row in batch:
            if row.id in allowed:
                page.append(row)
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
    next_cursor = None
    if has_more and page:
        last = page[-1]
        payload = json.dumps([fingerprint, last.observed_at.astimezone(UTC).isoformat(), str(last.id)], separators=(",", ":")).encode()
        next_cursor = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    elif scan_truncated and last_scanned is not None:
        last = last_scanned
        payload = json.dumps([fingerprint, last.observed_at.astimezone(UTC).isoformat(), str(last.id)], separators=(",", ":")).encode()
        next_cursor = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return tuple(page), next_cursor, scan_truncated


async def purge_source_in_uow(session: AsyncSession, source_id: UUID) -> None:
    """Remove every derived observation revision in the source's existing deletion transaction."""
    await session.execute(update(Observation).where(Observation.source_id == source_id).values(is_current=False))
    await session.execute(
        Observation.__table__.delete().where(Observation.source_id == source_id)
    )


async def purge_document_in_uow(session: AsyncSession, document_id: UUID) -> None:
    """Remove evidence-supported measurement revisions before their document is deleted."""
    await session.execute(
        Observation.__table__.delete().where(Observation.document_id == document_id)
    )
