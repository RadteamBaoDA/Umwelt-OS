import hashlib
import json
import secrets
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import String, case, cast, delete, func, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.events import DomainEvent
from core.pagination import decode_cursor, encode_cursor
from core.realtime import commit_with_replay, make_ingestion_change, make_knowledge_change, make_source_change
from modules.ingestion.models import (
    COLLECTION_LEASE,
    CollectorCredential,
    EventOutbox,
    IngestionBatch,
    IngestionRun,
    IngestionStage,
    ObservationNormalization,
    SourceIngestionState,
    SourceObservation,
)
from modules.ingestion.schemas import CrawlReceipt, EventDelivery, Receipt, ReceiveBatch, RunRead, SourceIngestionRead, StageRead
from modules.knowledge.documents import public as documents
from modules.sources import public as sources
from modules.sources.schemas import ConnectorSource


@dataclass(frozen=True)
class NewsDocumentReadyEvent:
    """Detached fixed-shape News readiness event owned by Ingestion."""
    id: UUID
    version: int
    status: str
    payload: dict[str, object]
    valid_payload: bool


async def lock_news_document_ready_event(
    session: AsyncSession, event_id: UUID,
) -> NewsDocumentReadyEvent | None:
    """Lock one News readiness outbox row and return only its bounded event payload.

    The event table remains private to Ingestion. Invalid or oversized payloads
    return a detached DTO with valid_payload false so the consumer can terminally
    fail the receipt without parsing arbitrary or unbounded JSON fields.
    """
    event = await session.scalar(select(EventOutbox).where(
        EventOutbox.id == event_id, EventOutbox.type == "news.document.ready",
    ).with_for_update())
    if event is None:
        return None
    payload = event.payload
    required = {"source_id", "source_generation", "document_id", "document_version_id", "version_number"}
    valid = isinstance(payload, Mapping) and set(payload) == required and len(payload) == len(required)
    if valid:
        strings = (payload.get("source_id"), payload.get("document_id"), payload.get("document_version_id"))
        valid = all(isinstance(value, str) and len(value) <= 36 for value in strings)
        generation = payload.get("source_generation")
        version_number = payload.get("version_number")
        valid = valid and type(generation) is int and 0 <= generation <= 2**31 - 1
        valid = valid and type(version_number) is int and 1 <= version_number <= 2**31 - 1
    detached = dict(payload) if valid else {}
    return NewsDocumentReadyEvent(
        id=event.id, version=event.version, status=event.status,
        payload=detached, valid_payload=bool(valid),
    )


async def mark_news_document_ready_event_delivered(session: AsyncSession, event_id: UUID) -> bool:
    """Flush News event acknowledgement without committing the caller's transaction."""
    event = await session.scalar(select(EventOutbox).where(
        EventOutbox.id == event_id, EventOutbox.type == "news.document.ready",
    ).with_for_update())
    if event is None:
        return False
    event.status = "delivered"
    await session.flush()
    return True


async def fail_news_document_ready_event(session: AsyncSession, event_id: UUID) -> bool:
    """Flush a terminal invalid News receipt state while leaving commit to the caller."""
    event = await session.scalar(select(EventOutbox).where(
        EventOutbox.id == event_id, EventOutbox.type == "news.document.ready",
    ).with_for_update())
    if event is None:
        return False
    event.status = "failed"
    await session.flush()
    return True

def _digest(value: object) -> str:
    """Hash canonical JSON so equivalent payload mappings share an identity."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


async def create_collector_credential(session: AsyncSession, source_id: UUID) -> str:
    """Rotate the source's ingestion credential and return its one-time token.

    Locks the source before revoking active credentials; only the token hash is
    persisted, and the caller controls transaction completion.
    """
    source = await sources.lock_source(session, source_id)
    if source is None or source.status == "archived":
        raise LookupError("Source not found")
    now = datetime.now(UTC)
    credentials = list(
        (
            await session.scalars(
                select(CollectorCredential)
                .where(CollectorCredential.source_id == source_id, CollectorCredential.revoked_at.is_(None))
                .with_for_update()
            )
        ).all()
    )
    for credential in credentials:
        credential.revoked_at = now
    token = secrets.token_urlsafe(32)
    session.add(CollectorCredential(token_hash=hashlib.sha256(token.encode()).hexdigest(), source_id=source_id))
    await session.flush()
    return token


async def revoke_collector_credential(session: AsyncSession, token: str) -> None:
    """Revoke a matching collector token after acquiring its source lock."""
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    source_id = await session.scalar(
        select(CollectorCredential.source_id).where(CollectorCredential.token_hash == token_hash)
    )
    if source_id is None or await sources.lock_source(session, source_id) is None:
        return
    row = await session.scalar(
        select(CollectorCredential)
        .where(CollectorCredential.token_hash == token_hash, CollectorCredential.source_id == source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is not None:
        row.revoked_at = datetime.now(UTC)


async def revoke_source_credentials(session: AsyncSession, source_id: UUID) -> None:
    """Revoke every active collector token for an existing locked source."""
    if await sources.lock_source(session, source_id) is None:
        return
    await session.execute(
        update(CollectorCredential)
        .where(CollectorCredential.source_id == source_id, CollectorCredential.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )


async def publish_event(session: AsyncSession, event: DomainEvent) -> None:
    """Add a durable pending event to the caller's transaction outbox."""
    session.add(EventOutbox(
        id=event.id,
        type=event.type,
        version=event.version,
        occurred_at=event.occurred_at,
        producer=event.producer,
        payload=event.payload,
        status="pending",
    ))


NORMALIZATION_VERSION = 1


async def schedule_normalization(
    session: AsyncSession, run: IngestionRun, batch: IngestionBatch, source_generation: int,
    received_at: datetime,
) -> IngestionStage | None:
    """Create or reuse normalization work and idempotent observation progress.

    Empty batches produce no stage; a new stage emits one durable request event.
    """
    observations = list((await session.scalars(
        select(SourceObservation).where(SourceObservation.batch_id == batch.id).order_by(SourceObservation.id)
    )).all())
    if not observations:
        return None
    stage = await session.scalar(
        select(IngestionStage).where(IngestionStage.run_id == run.id, IngestionStage.stage_key == "normalize")
    )
    if stage is None:
        stage = IngestionStage(run_id=run.id, stage_key="normalize", status="pending")
        session.add(stage)
        await session.flush()
        event = DomainEvent(
            id=uuid4(), type="ingestion.normalize.requested", version=1,
            occurred_at=received_at, producer="modules.ingestion",
            payload={
                "run_id": str(run.id), "stage_id": str(stage.id),
                "source_generation": source_generation, "normalization_version": NORMALIZATION_VERSION,
            },
        )
        await publish_event(session, event)
    existing_ids = set((await session.scalars(
        select(ObservationNormalization.observation_id).where(
            ObservationNormalization.stage_id == stage.id,
            ObservationNormalization.normalization_version == NORMALIZATION_VERSION,
        )
    )).all())
    for observation in observations:
        if observation.id not in existing_ids:
            session.add(ObservationNormalization(
                observation_id=observation.id, source_id=observation.source_id,
                run_id=run.id, stage_id=stage.id, source_generation=source_generation,
                normalization_version=NORMALIZATION_VERSION,
            ))
        if observation.received_at is None:
            observation.received_at = received_at
    await session.flush()
    return stage


async def tombstone_document_materializations(session: AsyncSession, document_id: UUID) -> None:
    """Make already accepted work terminal when its normalized document is deleted."""
    await session.execute(
        update(ObservationNormalization)
        .where(ObservationNormalization.document_id == document_id)
        .values(disposition="skipped", error_code="document_deleted", document_id=None, document_version_id=None)
    )


async def get_event_delivery(session: AsyncSession, event_id: UUID) -> EventDelivery | None:
    """Read an event delivery status with a defensive copy of its payload."""
    event = await session.get(EventOutbox, event_id)
    if event is None:
        return None
    return EventDelivery(id=event.id, status=event.status, payload=deepcopy(event.payload))


async def set_event_delivery(
    session: AsyncSession,
    event_id: UUID,
    status: Literal["failed", "pending", "delivered"],
    *,
    next_attempt_at: datetime | None = None,
) -> bool:
    """Update an outbox event status and optional retry time; report if found."""
    values: dict[str, object] = {"status": status}
    if next_attempt_at is not None:
        values["next_attempt_at"] = next_attempt_at
    result = await session.execute(
        update(EventOutbox)
        .where(EventOutbox.id == event_id)
        .values(**values)
        .returning(EventOutbox.id)
    )
    return result.scalar_one_or_none() is not None


async def get_source_cursor(session: AsyncSession, source_id: UUID) -> str | None:
    """Return the persisted collection cursor, or None before first ingestion."""
    state = await session.get(SourceIngestionState, source_id)
    return state.cursor if state is not None else None


async def cancel_and_purge_source_ingestion(session: AsyncSession, source_id: UUID) -> None:
    """Fail queued events and delete ingestion data while clearing leases.

    The caller must hold the source lock first to serialize this purge with
    collection and credential changes.
    """
    run_ids = select(cast(IngestionRun.id, String)).where(IngestionRun.source_id == source_id)
    await session.execute(
        update(EventOutbox)
        .where(EventOutbox.payload["run_id"].astext.in_(run_ids))
        .values(status="failed")
    )
    await session.execute(delete(SourceObservation).where(SourceObservation.source_id == source_id))
    await session.execute(delete(IngestionBatch).where(IngestionBatch.source_id == source_id))
    state = await session.get(SourceIngestionState, source_id, with_for_update=True)
    if state is not None:
        state.lease_run_id = None
        state.lease_expires_at = None
    await session.execute(update(CollectorCredential).where(CollectorCredential.source_id == source_id)
                          .values(revoked_at=datetime.now(UTC)))


async def collector_can_ingest(session: AsyncSession, source_id: UUID, token: str) -> bool:
    """Check token scope, revocation state, and active connector status."""
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    credential_valid = bool(await session.scalar(
        select(CollectorCredential.token_hash).where(
            CollectorCredential.token_hash == token_hash,
            CollectorCredential.source_id == source_id,
            CollectorCredential.scope == "ingestion:write",
            CollectorCredential.revoked_at.is_(None),
        )
    ))
    source = await sources.get_connector_source(session, source_id) if credential_valid else None
    return source is not None and source.status == "active"


async def receive_batch(
    session: AsyncSession,
    payload: ReceiveBatch,
    collector_token: str,
) -> tuple[IngestionBatch, IngestionRun]:
    """Authenticate and idempotently accept a fenced collection batch.

    Enforces active source/generation, collector token, and connector fence
    before duplicate lookup. Exact duplicate keys return the existing batch/run
    before new-work cursor/lease checks and without committing new work; differing
    payload hashes raise HTTP 409. A new batch deduplicates identical
    provider/hash/observation-time records, advances the cursor and lease, writes
    stage/outbox state, and commits with realtime changes before returning.
    """
    source = await sources.lock_source(session, payload.source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if source.status != "active":
        raise HTTPException(status_code=409, detail="Source is not active")
    if source.generation != payload.source_generation:
        raise HTTPException(status_code=409, detail="Source generation changed during collection")
    token_hash = hashlib.sha256(collector_token.encode()).hexdigest()
    grant_valid = bool(await session.scalar(
        select(CollectorCredential.token_hash).where(
            CollectorCredential.token_hash == token_hash,
            CollectorCredential.source_id == payload.source_id,
            CollectorCredential.scope == "ingestion:write",
            CollectorCredential.revoked_at.is_(None),
        )
    ))
    if not grant_valid:
        raise HTTPException(status_code=401, detail="Collector authentication required")
    from modules.connectors import public as connectors

    if not await connectors.require_batch_fence(
        session,
        ConnectorSource(
            id=source.id,
            type="api",
            status=source.status,
            generation=source.generation,
            configuration={},
        ),
        payload.source_generation,
        payload.connector_revision,
    ):
        raise HTTPException(status_code=409, detail="Connector collection fence is stale or required")

    payload_hash = _digest(payload.model_dump(mode="json"))
    existing = await session.scalar(
        select(IngestionBatch).where(
            IngestionBatch.source_id == payload.source_id,
            IngestionBatch.batch_key == payload.batch_key,
        )
    )
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise HTTPException(status_code=409, detail="Batch key was already used with different content")
        run = await session.scalar(select(IngestionRun).where(IngestionRun.batch_id == existing.id))
        if run is None:
            raise RuntimeError("Ingestion batch has no run")
        return existing, run

    state = await session.get(SourceIngestionState, payload.source_id, with_for_update=True)
    if state is None:
        state = SourceIngestionState(source_id=payload.source_id, cursor=None)
        session.add(state)
        await session.flush()
    now = datetime.now(UTC)
    if not await sources.record_collection_started(session, payload.source_id, source.generation, now):
        raise HTTPException(status_code=409, detail="Source is not active")
    if state.lease_expires_at is not None and state.lease_expires_at > now:
        raise HTTPException(status_code=409, detail="Source already has an active collection run")
    if state.cursor != payload.cursor_before:
        raise HTTPException(status_code=409, detail="Collection cursor is stale")

    batch = IngestionBatch(
        source_id=payload.source_id, batch_key=payload.batch_key, payload_hash=payload_hash,
        source_generation=source.generation,
    )
    session.add(batch)
    await session.flush()
    run = IngestionRun(batch_id=batch.id, source_id=payload.source_id, status="queued")
    session.add(run)
    await session.flush()
    stage = IngestionStage(run_id=run.id, stage_key="receive", status="pending")
    session.add(stage)
    await session.flush()
    event = DomainEvent(
        id=uuid4(),
        type="ingestion.stage.requested",
        version=1,
        occurred_at=now,
        producer="modules.ingestion",
        payload={
            "run_id": str(run.id),
            "stage_id": str(stage.id),
            "source_generation": source.generation,
            **({"connector_revision": payload.connector_revision} if payload.connector_revision is not None else {}),
        },
    )
    await publish_event(session, event)
    # Keep every distinct provider/content observation in the accepted batch.
    seen: set[tuple[str, str, datetime]] = set()
    for record in payload.records:
        data = record.model_dump(mode="json")
        record_hash = _digest({"version": record.version, "content": record.content, "metadata": record.metadata})
        identity = (record.provider_id, record_hash, record.observed_at)
        if identity in seen:
            continue
        seen.add(identity)
        session.add(
            SourceObservation(
                source_id=payload.source_id,
                batch_id=batch.id,
                provider_id=record.provider_id,
                record_hash=record_hash,
                payload=data,
                observed_at=record.observed_at,
                received_at=now,
                collected_at=None,
            )
        )
    await session.flush()
    normalize_stage = await schedule_normalization(session, run, batch, source.generation, now)
    result = await session.execute(
        update(SourceIngestionState)
        .where(
            SourceIngestionState.source_id == payload.source_id,
            SourceIngestionState.cursor.is_not_distinct_from(payload.cursor_before),
        )
        .values(cursor=payload.cursor_after, lease_run_id=run.id, lease_expires_at=now + COLLECTION_LEASE)
    )
    if result.rowcount != 1:
        raise HTTPException(status_code=409, detail="Collection cursor changed")
    changes = [
        make_source_change(source.id, source.generation, source.status),
        make_ingestion_change(source.id, run.id, run.status, stage.stage_key, stage.status),
    ]
    if normalize_stage is not None:
        changes.append(make_ingestion_change(source.id, run.id, run.status, normalize_stage.stage_key, normalize_stage.status))
    await commit_with_replay(session, changes)
    await session.refresh(batch)
    await session.refresh(run)
    return batch, run


async def receive_connector_batch(
    session: AsyncSession, payload: ReceiveBatch, collector_token: str
) -> Receipt:
    """Accept a connector batch and return its public run receipt."""
    batch, run = await receive_batch(session, payload, collector_token)
    return Receipt(batch_id=batch.id, run_id=run.id, status=run.status)


async def queue_connector_crawl(
    session: AsyncSession,
    source_id: UUID,
    source_generation: int,
    connector_revision: int,
    cursor_before: str | None,
    configuration: dict[str, object],
) -> CrawlReceipt:
    """Idempotently queue a crawl keyed by source, cursor, config, and minute.

    A matching batch/run receipt returns without the new-work commit. New work
    persists its stage, request event, cursor lease and realtime updates in this
    function's commit; stale source, connector revision, cursor, or active-lease
    checks raise HTTP 404/409.
    """
    source = await sources.lock_source(session, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if source.status != "active":
        raise HTTPException(status_code=409, detail="Source is not active")
    from modules.connectors import public as connectors
    from modules.connectors.public import CollectionFence

    if not await connectors.require_collection_fence(
        session,
        ConnectorSource(
            id=source.id,
            type="api",
            status=source.status,
            generation=source.generation,
            configuration={},
        ),
        CollectionFence(
            source_generation=source_generation,
            connector_revision=connector_revision,
        ),
        lock=True,
    ):
        raise HTTPException(status_code=409, detail="Connector collection fence is stale")
    state = await session.get(SourceIngestionState, source_id, with_for_update=True)
    if state is None:
        state = SourceIngestionState(source_id=source_id, cursor=None)
        session.add(state)
        await session.flush()
    if state.cursor != cursor_before:
        raise HTTPException(status_code=409, detail="Collection cursor is stale")

    now = datetime.now(UTC)
    if not await sources.record_collection_started(session, source_id, source.generation, now):
        raise HTTPException(status_code=409, detail="Source is not active")
    minute = now.replace(second=0, microsecond=0).isoformat()
    key = "crawl:" + _digest({"source_id": str(source_id), "cursor": cursor_before, "config": configuration, "minute": minute})
    existing = await session.scalar(
        select(IngestionBatch).where(IngestionBatch.source_id == source_id, IngestionBatch.batch_key == key)
    )
    if existing is not None:
        run = await session.scalar(select(IngestionRun).where(IngestionRun.batch_id == existing.id))
        if run is None:
            raise RuntimeError("Crawl batch has no run")
        return CrawlReceipt(run_id=run.id)
    if state.lease_expires_at is not None and state.lease_expires_at > now:
        raise HTTPException(status_code=409, detail="Source already has an active collection run")

    batch = IngestionBatch(
        source_id=source_id, batch_key=key, payload_hash=_digest(configuration),
        source_generation=source.generation,
    )
    session.add(batch)
    await session.flush()
    run = IngestionRun(batch_id=batch.id, source_id=source_id, status="queued")
    session.add(run)
    await session.flush()
    stage = IngestionStage(run_id=run.id, stage_key="collect_web", status="pending")
    session.add(stage)
    await session.flush()
    event = DomainEvent(
        id=uuid4(),
        type="connector.crawl.requested",
        version=1,
        occurred_at=now,
        producer="modules.connectors",
        payload={
            "source_id": str(source_id),
            "source_generation": source_generation,
            "connector_revision": connector_revision,
            "run_id": str(run.id),
            "stage_id": str(stage.id),
            "cursor_before": cursor_before,
            "configuration": configuration,
        },
    )
    await publish_event(session, event)
    state.lease_run_id = run.id
    state.lease_expires_at = now + COLLECTION_LEASE
    await commit_with_replay(session, [
        make_source_change(source.id, source.generation, source.status),
        make_ingestion_change(source.id, run.id, run.status, stage.stage_key, stage.status),
    ])
    await session.refresh(run)
    return CrawlReceipt(run_id=run.id)


async def receive_file(
    session: AsyncSession,
    source_id: UUID,
    document_id: UUID,
    filename: str,
    mime_type: str,
    raw_uri: str,
    size: int,
    digest: str,
) -> tuple[IngestionRun, bool]:
    """Accept an uploaded file and return its run plus whether it was created.

    The owner-write route performs caller authorization and owns cleanup of staged
    raw bytes. This function locks and checks the active source, commits either
    the existing idempotent run (False) or a new document, batch, stage, and
    durable event (True), and rejects reuse whose document identity was deleted
    with HTTP 409.
    """
    await sources.lock_source_for_document(session, source_id)
    source = await sources.lock_source(session, source_id)
    if source is None or source.status != "active":
        raise HTTPException(status_code=409, detail="Source is not active")
    batch_key = f"file:{digest}"
    existing = await session.scalar(
        select(IngestionBatch).where(IngestionBatch.source_id == source_id, IngestionBatch.batch_key == batch_key)
    )
    if existing is not None:
        if existing.payload_hash != digest:
            raise HTTPException(status_code=409, detail="Upload identity conflicts with stored content")
        if not await documents.has_document_identity(session, source_id, f"file:{digest}"):
            raise HTTPException(status_code=409, detail="This file was previously ingested and its document was deleted")
        run = await session.scalar(select(IngestionRun).where(IngestionRun.batch_id == existing.id))
        if run is None:
            raise RuntimeError("Ingestion batch has no run")
        await session.commit()
        return run, False

    now = datetime.now(UTC)
    if not await sources.record_collection_started(session, source_id, source.generation, now):
        raise HTTPException(status_code=409, detail="Source is not active")
    batch = IngestionBatch(
        source_id=source_id, batch_key=batch_key, payload_hash=digest,
        source_generation=source.generation,
    )
    session.add(batch)
    await session.flush()
    run = IngestionRun(batch_id=batch.id, source_id=source_id, status="queued")
    session.add(run)
    await session.flush()
    stage = IngestionStage(run_id=run.id, stage_key="parse_file", status="pending")
    session.add(stage)
    await session.flush()
    metadata = {"filename": filename, "raw_sha256": digest, "raw_size": size, "format": mime_type}
    stored_document_id = await documents.add_uploaded_document(
        session, source_id, filename[:500] or "Uploaded file", mime_type, raw_uri, metadata, f"file:{digest}", document_id
    )
    event = DomainEvent(
        id=uuid4(),
        type="document.file.uploaded",
        version=1,
        occurred_at=now,
        producer="modules.ingestion",
        payload={"run_id": str(run.id), "stage_id": str(stage.id), "document_id": str(stored_document_id), "raw_uri": raw_uri, "mime_type": mime_type, "source_generation": source.generation},
    )
    await publish_event(session, event)
    await commit_with_replay(session, [
        make_source_change(source.id, source.generation, source.status),
        make_ingestion_change(source.id, run.id, run.status, stage.stage_key, stage.status),
        make_knowledge_change(source_id, stored_document_id, 1),
    ])
    await session.refresh(run)
    return run, True


async def _read_stages(session: AsyncSession, stages: list[IngestionStage]) -> list[StageRead]:
    """Project persisted stage state into ordered API read models."""
    if not stages:
        return []
    counts = await session.execute(
        select(
            ObservationNormalization.stage_id,
            func.sum(case((ObservationNormalization.disposition == "normalized", 1), else_=0)),
            func.sum(case((ObservationNormalization.disposition == "duplicate", 1), else_=0)),
            func.sum(case((ObservationNormalization.disposition == "skipped", 1), else_=0)),
            func.sum(case((ObservationNormalization.disposition == "failed", 1), else_=0)),
            func.sum(case((ObservationNormalization.disposition == "pending", 1), else_=0)),
        )
        .where(ObservationNormalization.stage_id.in_([stage.id for stage in stages]))
        .group_by(ObservationNormalization.stage_id)
    )
    by_stage = {
        row[0]: tuple(int(value or 0) for value in row[1:])
        for row in counts
    }
    return [
        StageRead(
            stage_key=stage.stage_key, status=stage.status, attempts=stage.attempts,
            error_code=stage.error_code, result_count=stage.result_count, updated_at=stage.updated_at,
            normalized_count=by_stage.get(stage.id, (0, 0, 0, 0, 0))[0],
            duplicate_count=by_stage.get(stage.id, (0, 0, 0, 0, 0))[1],
            skipped_count=by_stage.get(stage.id, (0, 0, 0, 0, 0))[2],
            failed_count=by_stage.get(stage.id, (0, 0, 0, 0, 0))[3],
            pending_count=by_stage.get(stage.id, (0, 0, 0, 0, 0))[4],
        )
        for stage in stages
    ]


async def get_run(session: AsyncSession, run_id: UUID) -> tuple[IngestionRun, list[StageRead]] | None:
    """Return a run and its stage projection, or None when the run is absent."""
    run = await session.get(IngestionRun, run_id)
    if run is None:
        return None
    stages = list(
        (
            await session.scalars(
                select(IngestionStage).where(IngestionStage.run_id == run_id).order_by(IngestionStage.stage_key)
            )
        ).all()
    )
    return run, await _read_stages(session, stages)


async def list_source_runs(
    session: AsyncSession,
    source_id: UUID,
    *,
    limit: int = 20,
    cursor: str | None = None,
) -> SourceIngestionRead | None:
    """Return detached current and bounded recent runs after source-owner existence check."""
    from modules.ingestion.schemas import SourceIngestionRead

    source = await sources.get_connector_source(session, source_id)
    if source is None:
        return None
    statement = select(IngestionRun).where(IngestionRun.source_id == source_id)
    if cursor:
        created_at, identifier = decode_cursor(cursor)
        statement = statement.where(
            tuple_(IngestionRun.created_at, IngestionRun.id) < (created_at, identifier)
        )
    rows = list((await session.scalars(
        statement.order_by(IngestionRun.created_at.desc(), IngestionRun.id.desc()).limit(limit + 1)
    )).all())
    page_rows = rows[:limit]
    next_cursor = (
        encode_cursor(page_rows[-1].created_at, page_rows[-1].id)
        if len(rows) > limit and page_rows
        else None
    )
    current = await session.scalar(
        select(IngestionRun)
        .where(IngestionRun.source_id == source_id, IngestionRun.status.in_(("queued", "running")))
        .order_by(IngestionRun.created_at.desc(), IngestionRun.id.desc())
        .limit(1)
    )
    run_rows = list({run.id: run for run in [*page_rows, *([current] if current else [])]}.values())
    stage_rows = list((await session.scalars(
        select(IngestionStage)
        .where(IngestionStage.run_id.in_([run.id for run in run_rows]))
        .order_by(IngestionStage.stage_key)
    )).all()) if run_rows else []
    stages_by_run: dict[UUID, list[IngestionStage]] = {run.id: [] for run in run_rows}
    for stage in stage_rows:
        stages_by_run[stage.run_id].append(stage)
    stage_reads = {
        run_id: await _read_stages(session, stages)
        for run_id, stages in stages_by_run.items()
    }

    def detach(run: IngestionRun) -> RunRead:
        """Project an ORM run and its stages into a detached response."""
        return RunRead(
            run_id=run.id,
            source_id=run.source_id,
            status=run.status,
            stages=stage_reads[run.id],
            error_code=run.error_code,
            created_at=run.created_at,
            updated_at=run.updated_at,
        )

    return SourceIngestionRead(
        current_run=detach(current) if current is not None else None,
        items=[detach(run) for run in page_rows],
        next_cursor=next_cursor,
    )


async def retry_run(
    session: AsyncSession, run_id: UUID, requested_stage_key: str | None = None
) -> IngestionRun | None:
    """Requeue an eligible failed stage after validating source generation.

    Locks in source, run, stage order and requires a durable prior event. Returns
    None when the run is absent; no-op paths return the existing run for active,
    successful, or otherwise non-retryable work. Accepted retries reset attempts/errors, copy the prior
    event payload into a fresh outbox event, refresh collection leases when
    needed, and commit. Failed normalization progress requiring correction and
    stale source generations are rejected with HTTP 409.
    """
    run_hint = await session.get(IngestionRun, run_id)
    if run_hint is None:
        return None
    # Match source archive and workers: source, run, then stage.
    source = await sources.lock_source(session, run_hint.source_id)
    if source is None or source.status != "active":
        raise HTTPException(status_code=409, detail="Source is not active")
    run = await session.scalar(
        select(IngestionRun).where(IngestionRun.id == run_id, IngestionRun.source_id == source.id).with_for_update()
    )
    if run is None:
        return None
    stages = list((await session.scalars(
        select(IngestionStage).where(IngestionStage.run_id == run_id)
        .order_by(IngestionStage.stage_key).with_for_update()
    )).all())
    if not stages:
        raise RuntimeError("Ingestion run has no stage")
    if any(stage.status in {"pending", "queued", "running", "retrying"} for stage in stages):
        if requested_stage_key is not None:
            raise HTTPException(status_code=409, detail="Ingestion run still has an active stage")
        return run
    if all(stage.status == "succeeded" for stage in stages):
        return run
    retry_order = {"receive": 0, "collect_web": 1, "normalize": 2, "parse_file": 3}
    failed = sorted(
        (stage for stage in stages if stage.status == "failed"),
        key=lambda stage: (retry_order.get(stage.stage_key, 100), stage.stage_key),
    )
    if not failed:
        return run
    stage = next((item for item in failed if item.stage_key == requested_stage_key), None) if requested_stage_key else failed[0]
    if stage is None:
        raise HTTPException(status_code=409, detail="Requested stage is not retryable")
    prior_event = await session.scalar(
        select(EventOutbox)
        .where(EventOutbox.payload["stage_id"].astext == str(stage.id))
        .order_by(EventOutbox.created_at.desc())
        .limit(1)
        .with_for_update()
    )
    if prior_event is None:
        raise HTTPException(status_code=409, detail="Failed stage has no durable retry event")
    captured_generation = prior_event.payload.get("source_generation")
    if not isinstance(captured_generation, int) or captured_generation != source.generation:
        raise HTTPException(status_code=409, detail="Failed stage belongs to an obsolete source generation")
    if stage.stage_key == "normalize":
        failed_progress = await session.scalar(
            select(ObservationNormalization.id).where(
                ObservationNormalization.stage_id == stage.id,
                ObservationNormalization.disposition == "failed",
            ).limit(1)
        )
        if failed_progress is not None:
            raise HTTPException(status_code=409, detail="Invalid observations require correction before normalization retry")
    stage.status = "pending"
    stage.attempts = 0
    stage.error_code = None
    stage.next_attempt_at = datetime.now(UTC)
    stage.lease_expires_at = None
    run.status = "queued"
    run.error_code = None
    event = DomainEvent(
        id=uuid4(),
        type=prior_event.type,
        version=1,
        occurred_at=datetime.now(UTC),
        producer="modules.ingestion",
        payload={
            **prior_event.payload,
        },
    )
    session.add(
        EventOutbox(
            id=event.id,
            type=event.type,
            version=1,
            occurred_at=event.occurred_at,
            producer=event.producer,
            payload=event.payload,
        )
    )
    state = await session.get(SourceIngestionState, run.source_id, with_for_update=True)
    if stage.stage_key in {"receive", "collect_web"} and state is not None:
        now = datetime.now(UTC)
        if state.lease_run_id not in (None, run.id) and state.lease_expires_at and state.lease_expires_at > now:
            raise HTTPException(status_code=409, detail="Source already has an active collection run")
        state.lease_run_id = run.id
        state.lease_expires_at = now + COLLECTION_LEASE
    run.status = "queued"
    run.error_code = None
    await commit_with_replay(
        session,
        [make_ingestion_change(source.id, run.id, run.status, stage.stage_key, stage.status)],
    )
    await session.refresh(run)
    return run
