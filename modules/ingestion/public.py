import hashlib
import json
import secrets
from core.telemetry import RunMeta as _RunMeta
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import String, and_, case, cast, delete, func, not_, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

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
from modules.ingestion.schemas import (
    ConnectorCollectionLease, CrawlReceipt, EventDelivery, NativeCollectionBatch,
    NativeCollectionReceipt, Receipt, ReceiveBatch, RunRead, SourceIngestionRead,
    StageRead, TelegramCursor, TelegramDeliveryProof, TelegramProbeClassification,
    TelegramRawDelivery, classify_telegram_probe,
)
from modules.knowledge.documents import public as documents
from modules.sources import public as sources
from modules.sources.schemas import ConnectorSource

_RUN_RETRY_ORDER = {"receive": 0, "collect_web": 1, "normalize": 2, "parse_file": 3}


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


async def create_collector_credential(
    session: AsyncSession, source_id: UUID, *, scope: str = "ingestion:write",
) -> str:
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
                .where(
                    CollectorCredential.source_id == source_id,
                    CollectorCredential.scope == scope,
                    CollectorCredential.revoked_at.is_(None),
                )
                .with_for_update()
            )
        ).all()
    )
    for credential in credentials:
        credential.revoked_at = now
    token = secrets.token_urlsafe(32)
    session.add(CollectorCredential(
        token_hash=hashlib.sha256(token.encode()).hexdigest(), source_id=source_id, scope=scope,
    ))
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


async def reset_native_collection_cursor(session: AsyncSession, source_id: UUID) -> None:
    """Clear one native provider cursor only after its exact collection and run leases are inactive.

    The caller holds source, connector, and provider identity locks before this state lock. The
    owner commits the reset with replay publication; a live collector or nonterminal run rejects it.
    """
    source = await sources.get_connector_source(session, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    state = await session.get(SourceIngestionState, source_id, with_for_update=True)
    if state is None:
        await commit_with_replay(session, [make_source_change(source.id, source.generation, source.status)])
        return
    now = datetime.now(UTC)
    if state.collection_lease_token is not None and state.lease_expires_at is not None and state.lease_expires_at > now:
        raise HTTPException(status_code=409, detail="A collection is still active")
    active_run = await session.scalar(
        select(IngestionRun.id).where(
            IngestionRun.source_id == source_id,
            IngestionRun.status.not_in(("succeeded", "failed")),
        ).limit(1).with_for_update()
    )
    if active_run is not None:
        raise HTTPException(status_code=409, detail="An ingestion run is still active")
    state.collection_lease_token = None
    state.lease_run_id = None
    state.lease_expires_at = None
    state.cursor = None
    await commit_with_replay(session, [make_source_change(source.id, source.generation, source.status)])


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
        state.collection_lease_token = None
        state.lease_expires_at = None
    await session.execute(update(CollectorCredential).where(CollectorCredential.source_id == source_id)
                          .values(revoked_at=datetime.now(UTC)))


async def collector_can_ingest(
    session: AsyncSession, source_id: UUID, token: str, *, scope: str = "ingestion:write",
) -> bool:
    """Check token scope, revocation state, and active connector status."""
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    credential_valid = bool(await session.scalar(
        select(CollectorCredential.token_hash).where(
            CollectorCredential.token_hash == token_hash,
            CollectorCredential.source_id == source_id,
            CollectorCredential.scope == scope,
            CollectorCredential.revoked_at.is_(None),
        )
    ))
    source = await sources.get_connector_source(session, source_id) if credential_valid else None
    return source is not None and source.status == "active"


def _encode_telegram_cursor(cursor: TelegramCursor) -> str:
    """Serialize the exact bounded Telegram cursor object stored in source state."""
    return json.dumps(cursor.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))


def _decode_telegram_cursor(value: str | None) -> TelegramCursor | None:
    """Parse a persisted Telegram cursor and fail closed on corrupt state."""
    if value is None:
        return None
    try:
        return TelegramCursor.model_validate_json(value)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="Telegram collection cursor is invalid") from exc


async def _lock_source_projection(session: AsyncSession, source_id: UUID) -> ConnectorSource | None:
    """Hold the narrow source fence while reading the detached connector projection.

    SourceFence owns lifecycle locking; provider, type, and configuration are
    read through the source owner's ConnectorSource contract under that lock.
    """
    fence = await sources.lock_source(session, source_id)
    if fence is None:
        return None
    projection = await sources.get_connector_source(session, source_id)
    if projection is None or (
        projection.status != fence.status or projection.generation != fence.generation
    ):
        raise HTTPException(status_code=409, detail="Source projection changed under lifecycle lock")
    return projection


async def acquire_connector_collection(
    session: AsyncSession,
    *,
    source_id: UUID,
    source_generation: int,
    connector_revision: int,
    collector_token: str,
) -> ConnectorCollectionLease:
    """Reserve one native fetch under source, provisioning, credential, then state locks.

    The lease token is distinct from a processing run lease. It is committed with
    the source collection-start event before the provider performs network I/O;
    only an expired owner can be replaced and every later write rechecks its token.
    """
    source = await _lock_source_projection(session, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if source.status != "active" or source.generation != source_generation:
        raise HTTPException(status_code=409, detail="Source generation is not active")
    token_hash = hashlib.sha256(collector_token.encode()).hexdigest()
    grant_valid = bool(await session.scalar(select(CollectorCredential.token_hash).where(
        CollectorCredential.token_hash == token_hash,
        CollectorCredential.source_id == source_id,
        CollectorCredential.scope == "ingestion:write",
        CollectorCredential.revoked_at.is_(None),
    )))
    if not grant_valid:
        raise HTTPException(status_code=401, detail="Collector authentication required")
    from modules.connectors import public as connectors

    if not connectors.is_native_provider(source.provider):
        raise HTTPException(status_code=409, detail="Native provider is not configured")
    if not await connectors.require_collection_fence(
        session, source,
        connectors.CollectionFence(source_generation=source_generation, connector_revision=connector_revision),
        lock=True,
    ):
        raise HTTPException(status_code=409, detail="Connector collection fence is stale")
    if source.provider == "telegram":
        credential = await connectors.get_native_credential_snapshot(
            session, source_id, source_generation=source_generation,
            connector_revision=connector_revision,
        )
        if (
            credential is None or credential.source_generation != source_generation
            or credential.configuration_revision != connector_revision
            or credential.state != "ready" or not credential.verified_bot_id
            or not credential.encrypted_token or credential.validated_at is None
        ):
            raise HTTPException(status_code=409, detail="Native Telegram credential is not ready")
    state = await session.get(SourceIngestionState, source_id, with_for_update=True)
    if state is None:
        state = SourceIngestionState(source_id=source_id, cursor=None)
        session.add(state)
        await session.flush()
    now = datetime.now(UTC)
    if (
        (state.lease_run_id is not None and state.lease_expires_at is None)
        or (state.collection_lease_token is not None and state.lease_expires_at is None)
    ):
        raise HTTPException(status_code=409, detail="Source collection ownership state is invalid")
    if state.lease_expires_at is not None and state.lease_expires_at > now:
        raise HTTPException(status_code=409, detail="Source collection is already in progress")
    token = uuid4()
    expires_at = now + COLLECTION_LEASE
    state.lease_run_id = None
    state.collection_lease_token = token
    state.lease_expires_at = expires_at
    if not await sources.record_collection_started(session, source_id, source_generation, now):
        raise HTTPException(status_code=409, detail="Source is not active")
    await commit_with_replay(session, [make_source_change(source.id, source.generation, source.status)])
    return ConnectorCollectionLease(
        source_id=source_id, source_generation=source_generation,
        connector_revision=connector_revision, token=token,
        cursor_before=state.cursor, expires_at=expires_at,
    )


async def read_telegram_collection_state(
    session: AsyncSession, lease: ConnectorCollectionLease
) -> TelegramCursor | None:
    """Read a reserved Telegram cursor after rechecking source, revision, and bot fences.

    This short transaction verifies the current native binding and releases every
    row lock before the connector performs network I/O.
    """
    source = await _lock_source_projection(session, lease.source_id)
    from modules.connectors import public as connectors

    if (
        source is None or source.provider != "telegram" or source.status != "active"
        or source.generation != lease.source_generation
    ):
        await session.rollback()
        raise HTTPException(status_code=409, detail="Telegram collection reservation is stale")
    if not await connectors.require_collection_fence(
        session, source,
        connectors.CollectionFence(
            source_generation=lease.source_generation,
            connector_revision=lease.connector_revision,
        ),
        lock=True,
    ):
        await session.rollback()
        raise HTTPException(status_code=409, detail="Telegram connector revision changed")
    credential = await connectors.get_native_credential_snapshot(
        session, lease.source_id,
        source_generation=lease.source_generation,
        connector_revision=lease.connector_revision,
    )
    if credential is None or credential.state != "ready" or not credential.verified_bot_id:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Native Telegram credential is not ready")
    if (
        credential.source_generation != lease.source_generation
        or credential.configuration_revision != lease.connector_revision
        or credential.encrypted_token is None or credential.validated_at is None
    ):
        await session.rollback()
        raise HTTPException(status_code=409, detail="Native Telegram credential fence changed")
    state = await session.get(SourceIngestionState, lease.source_id, with_for_update=True)
    now = datetime.now(UTC)
    if (
        state is None or state.collection_lease_token != lease.token
        or state.lease_expires_at is None or state.lease_expires_at <= now
        or state.cursor != lease.cursor_before
    ):
        await session.rollback()
        raise HTTPException(status_code=409, detail="Telegram collection reservation is stale")
    cursor = _decode_telegram_cursor(state.cursor)
    if cursor is not None and cursor.bot_id != credential.verified_bot_id:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Telegram cursor bot binding changed")
    await session.commit()
    return cursor


async def release_connector_collection(
    session: AsyncSession,
    lease: ConnectorCollectionLease,
    *,
    error_code: str | None,
) -> bool:
    """Release the matching reservation; stale revisions never publish old health errors.

    An exact token from the same source generation is cleared even after a desired
    revision changes so it cannot block the replacement. Health updates are only
    written while the original connector revision is still active.
    """
    source = await _lock_source_projection(session, lease.source_id)
    from modules.connectors import public as connectors

    fence_current = False
    if source is not None and source.status == "active" and source.generation == lease.source_generation:
        fence_current = await connectors.require_collection_fence(
            session, source,
            connectors.CollectionFence(
                source_generation=lease.source_generation,
                connector_revision=lease.connector_revision,
            ),
            lock=True,
        )
    state = await session.get(SourceIngestionState, lease.source_id, with_for_update=True)
    if (
        source is None or source.generation != lease.source_generation or state is None
        or state.collection_lease_token != lease.token
    ):
        await session.rollback()
        return False
    if error_code is not None and (not error_code or len(error_code) > 64):
        raise ValueError("Collection error code must be bounded")
    state.collection_lease_token = None
    state.lease_expires_at = None
    now = datetime.now(UTC)
    if error_code is not None and fence_current:
        await sources.record_collection_result(session, source.id, source.generation, now, error_code)
    await commit_with_replay(session, [make_source_change(source.id, source.generation, source.status)])
    return True


def validate_telegram_record_delivery(
    record: IngestionRecord,
    provider_metadata: object,
    proof: TelegramDeliveryProof,
    raw: TelegramRawDelivery,
    *,
    allowed_chat_ids: tuple[str, ...] | None = None,
) -> None:
    """Bind mapper identity, timestamps, version, and bot epoch to the raw update.

    Called before receipt/cursor commit and again when normalizing persisted owner
    proof. Telegram's Unix clocks and identity are compared to exact Bot API fields;
    optional scope filtering is enforced only at initial acceptance.
    """
    from modules.knowledge.documents.schemas import ProviderRecordMetadata

    metadata = ProviderRecordMetadata.model_validate(provider_metadata)
    detail = metadata.telegram
    update = raw.update
    edited_present = "edited_channel_post" in update
    original_present = "channel_post" in update
    message_key = "edited_channel_post" if edited_present else "channel_post"
    message = update.get(message_key)
    chat = message.get("chat") if isinstance(message, dict) else None
    if not isinstance(message, dict) or not isinstance(chat, dict) or detail is None:
        raise ValueError("Telegram channel message is missing")
    chat_id_value = chat.get("id")
    message_id_value = message.get("message_id")
    date_value = message.get("date")
    edit_date_value = message.get("edit_date")
    if (
        isinstance(update.get("update_id"), bool)
        or update.get("update_id") != raw.update_id
        or raw.update_id != proof.update_id
        or proof.raw_update_sha256 != raw.raw_update_sha256
        or edited_present == original_present
        or detail.edited_received != edited_present
        or chat.get("type") != "channel"
        or isinstance(chat_id_value, bool) or not isinstance(chat_id_value, int)
        or isinstance(message_id_value, bool) or not isinstance(message_id_value, int) or message_id_value < 0
        or isinstance(date_value, bool) or not isinstance(date_value, int) or date_value < 0
        or (edited_present and (isinstance(edit_date_value, bool) or not isinstance(edit_date_value, int) or edit_date_value < 0))
    ):
        raise ValueError("Telegram raw identity or clock fields are invalid")
    channel_id = str(chat_id_value)
    message_id = str(message_id_value)
    if allowed_chat_ids is not None and channel_id not in allowed_chat_ids:
        raise ValueError("Telegram channel is outside configured source scope")
    try:
        published_at = datetime.fromtimestamp(date_value, UTC)
        edited_at = datetime.fromtimestamp(edit_date_value, UTC) if edited_present else None
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("Telegram raw timestamps are invalid") from exc
    observed_at = edited_at if edited_present else published_at
    expected_version = f"telegram:{proof.epoch}:{proof.update_id}:{observed_at.isoformat()}"
    if (
        metadata.provider != "telegram"
        or metadata.identity != record.provider_id
        or record.provider_id != f"telegram:{channel_id}:{message_id}"
        or metadata.provider_version != record.version
        or record.version != expected_version
        or detail.bot_id != proof.bot_id
        or detail.epoch != proof.epoch
        or detail.update_id != proof.update_id
        or detail.channel_id != channel_id
        or detail.message_id != message_id
        or detail.published_at != published_at
        or detail.edited_at != edited_at
        or metadata.provider_modified_at != edited_at
        or record.observed_at != observed_at
        or metadata.timestamp_basis != ("provider_modified" if edited_present else "provider_published")
        or detail.raw_update_sha256 is not None
    ):
        raise ValueError("Telegram mapped record does not match its raw delivery")


async def accept_native_collection(
    session: AsyncSession,
    payload: NativeCollectionBatch,
    *,
    collector_token: str,
) -> NativeCollectionReceipt:
    """Authenticate, reclassify, and atomically persist one reserved native page.

    Source and connector/native credential fences precede batch replay lookup;
    the collection token and cursor are rechecked only for new work. Telegram
    proofs are recomputed from raw updates and the verified bot binding. GitHub
    proof shape, digest, and current grant fence are checked before exact replay
    lookup; for new work, records and cursor are recomputed only after the live
    reservation is confirmed. Targeted hint acknowledgement is flush-only in the
    same batch/source visibility transaction. Ambiguous missing current GitHub
    targets acknowledge uncertainty before pausing the source, fencing current
    evidence while preserving owner history. Replay head publication is last.
    """
    source = await _lock_source_projection(session, payload.source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if source.status != "active" or source.generation != payload.source_generation:
        raise HTTPException(status_code=409, detail="Source generation changed during collection")
    token_hash = hashlib.sha256(collector_token.encode()).hexdigest()
    grant_valid = bool(await session.scalar(select(CollectorCredential.token_hash).where(
        CollectorCredential.token_hash == token_hash,
        CollectorCredential.source_id == source.id,
        CollectorCredential.scope == "ingestion:write",
        CollectorCredential.revoked_at.is_(None),
    )))
    if not grant_valid:
        raise HTTPException(status_code=401, detail="Collector authentication required")
    from modules.connectors import public as connectors

    if not connectors.is_native_provider(source.provider):
        raise HTTPException(status_code=409, detail="Native provider is not configured")
    if not await connectors.require_collection_fence(
        session, source,
        connectors.CollectionFence(
            source_generation=payload.source_generation,
            connector_revision=payload.connector_revision,
        ),
        lock=True,
    ):
        raise HTTPException(status_code=409, detail="Connector collection fence is stale")
    bot_id: str | None = None
    github_proof = None
    github_fence = None
    if source.provider == "telegram":
        credential = await connectors.get_native_credential_snapshot(
            session, source.id, source_generation=payload.source_generation,
            connector_revision=payload.connector_revision,
        )
        if (
            credential is None or credential.source_generation != payload.source_generation
            or credential.configuration_revision != payload.connector_revision
            or credential.state != "ready" or not credential.verified_bot_id
            or credential.encrypted_token is None or credential.validated_at is None
        ):
            raise HTTPException(status_code=409, detail="Native Telegram credential is not ready")
        bot_id = credential.verified_bot_id
        if any(item.update_id != item.update.get("update_id") for item in payload.telegram_raw_deliveries):
            raise HTTPException(status_code=422, detail="Telegram update identity is invalid")
    elif payload.telegram_raw_deliveries or payload.telegram_deliveries:
        raise HTTPException(status_code=422, detail="Telegram proof is not valid for this provider")
    if source.provider == "github":
        from modules.connectors.github.schemas import GitHubSegmentProof

        if payload.github_segment is None or payload.telegram_raw_deliveries or payload.telegram_deliveries:
            raise HTTPException(status_code=422, detail="GitHub collection proof is required")
        try:
            github_proof = GitHubSegmentProof.model_validate(payload.github_segment.model_dump(mode="python"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="GitHub segment proof is malformed") from exc
        if github_proof.fence.connector_revision != payload.connector_revision:
            raise HTTPException(status_code=409, detail="GitHub collection revision is stale")
        github_fence = await connectors.get_github_binding_fence(
            session, source.id, source_generation=payload.source_generation,
            connector_revision=payload.connector_revision, lock=True,
        )
        if github_fence is None or github_proof.fence != github_fence:
            raise HTTPException(status_code=409, detail="GitHub grant is unavailable or requires reconnection")
    elif payload.github_segment is not None:
        raise HTTPException(status_code=422, detail="GitHub proof is not valid for this provider")

    stable_records = [record.model_dump(mode="json", exclude={"collected_at"}) for record in payload.records]
    stable = {
        "source_id": str(payload.source_id), "source_generation": payload.source_generation,
        "connector_revision": payload.connector_revision, "cursor_before": payload.cursor_before,
        "cursor_after": payload.cursor_after,
        "raw_hashes": [item.raw_update_sha256 for item in payload.telegram_raw_deliveries],
        "telegram_deliveries": [item.model_dump(mode="json") for item in payload.telegram_deliveries],
        "github_segment": github_proof.model_dump(mode="json") if github_proof is not None else None,
        "records": stable_records, "coverage": payload.coverage,
    }
    payload_hash = _digest(stable)
    batch_key = f"native:{source.provider}:{payload_hash}"
    if len(batch_key) > 255:
        raise HTTPException(status_code=422, detail="Native batch key exceeds its bound")
    # Lock state before receipt lookup to preserve the source -> connector -> state -> batch order.
    state = await session.get(SourceIngestionState, source.id, with_for_update=True)
    existing = await session.scalar(select(IngestionBatch).where(
        IngestionBatch.source_id == source.id,
        IngestionBatch.batch_key == batch_key,
    ))
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise HTTPException(status_code=409, detail="Native receipt conflicts with an existing batch")
        run = await session.scalar(select(IngestionRun).where(IngestionRun.batch_id == existing.id))
        if run is None:
            raise RuntimeError("Native ingestion batch has no run")
        if state is not None and state.collection_lease_token == payload.lease_token:
            # A replay may have acquired a fresh reservation; release only that exact token.
            state.collection_lease_token = None
            state.lease_expires_at = None
            await commit_with_replay(session, [make_source_change(source.id, source.generation, source.status)])
        count = len(payload.records)
        return NativeCollectionReceipt(
            batch_id=existing.id, run_id=run.id,
            status="queued" if count else ("succeeded" if payload.telegram_raw_deliveries else "no_changes"),
            received_update_count=len(payload.telegram_raw_deliveries), record_count=count,
            coverage=payload.coverage, cursor_after=payload.cursor_after,
        )

    if state is None or state.collection_lease_token != payload.lease_token:
        raise HTTPException(status_code=409, detail="Native collection reservation is stale")
    now = datetime.now(UTC)
    if state.lease_expires_at is None or state.lease_expires_at <= now:
        raise HTTPException(status_code=409, detail="Native collection reservation expired")
    if state.cursor != payload.cursor_before:
        raise HTTPException(status_code=409, detail="Native collection cursor is stale")

    classification: TelegramProbeClassification | None = None
    accepted_proofs: dict[int, TelegramDeliveryProof] = {}
    cursor_after = payload.cursor_after
    if github_proof is not None:
        github_segment = await connectors.validate_github_collection_segment(
            session, source=source, reserved_cursor_before=state.cursor,
            proof=github_proof,
        )
        supplied_records = [record.model_dump(mode="json", exclude={"collected_at"}) for record in payload.records]
        validated_records = [record.model_dump(mode="json", exclude={"collected_at"}) for record in github_segment.records]
        if (
            supplied_records != validated_records
            or payload.cursor_after != github_segment.cursor_after
            or payload.coverage != github_segment.coverage
        ):
            raise HTTPException(status_code=409, detail="GitHub collection transition is invalid")
        cursor_after = github_segment.cursor_after
    if source.provider == "telegram":
        current_cursor = _decode_telegram_cursor(state.cursor)
        classification = classify_telegram_probe(
            current_cursor, payload.telegram_raw_deliveries,
            received_at=payload.collected_at, verified_bot_id=bot_id or "",
        )
        if classification.conflict_code:
            raise HTTPException(status_code=409, detail=classification.conflict_code)
        if classification.delivery_proofs != tuple(payload.telegram_deliveries):
            raise HTTPException(status_code=409, detail="Telegram delivery proof is stale or invalid")
        computed_cursor = _encode_telegram_cursor(classification.cursor_after) if classification.cursor_after else state.cursor
        if computed_cursor != payload.cursor_after:
            raise HTTPException(status_code=409, detail="Telegram cursor transition is invalid")
        replay_ids = set(classification.replay_update_ids)
        accepted_proofs = {
            proof.update_id: proof for proof in classification.delivery_proofs
            if proof.update_id not in replay_ids
        }
    else:
        replay_ids = set()
    provider_records: list[tuple[IngestionRecord, dict[str, object], dict[str, object] | None]] = []
    seen_update_ids: set[int] = set()
    from modules.knowledge.documents.schemas import ProviderRecordMetadata

    for record in payload.records:
        try:
            metadata = ProviderRecordMetadata.model_validate(record.metadata.get("provider_record"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="Native provider metadata is invalid") from exc
        if metadata.provider != source.provider or metadata.identity != record.provider_id or metadata.provider_version != record.version:
            raise HTTPException(status_code=422, detail="Native provider identity is invalid")
        if metadata.coverage != payload.coverage:
            raise HTTPException(status_code=422, detail="Native provider coverage is inconsistent")
        proof_envelope: dict[str, object] | None = None
        if source.provider == "telegram":
            detail = metadata.telegram
            proof = accepted_proofs.get(detail.update_id) if detail is not None else None
            raw = next((item for item in payload.telegram_raw_deliveries if detail is not None and item.update_id == detail.update_id), None)
            if (
                detail is None or proof is None or raw is None or detail.bot_id != bot_id
                or detail.channel_id not in tuple(source.configuration.get("telegram_chat_ids", ()))
                or proof.raw_update_sha256 != raw.raw_update_sha256
                or detail.raw_update_sha256 is not None
                or detail.update_id in seen_update_ids
            ):
                # A known exact replay is acknowledged in the cursor but creates no new version.
                if detail is not None and detail.update_id in replay_ids:
                    continue
                raise HTTPException(status_code=422, detail="Telegram document proof is invalid")
            try:
                validate_telegram_record_delivery(
                    record, metadata, proof, raw,
                    allowed_chat_ids=tuple(source.configuration.get("telegram_chat_ids", ())),
                )
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail="Telegram document does not match its raw delivery") from exc
            seen_update_ids.add(detail.update_id)
            proof_envelope = proof.model_dump(mode="json")
        record_data = record.model_dump(mode="json", exclude={"collected_at"})
        telegram_envelope = None
        if proof_envelope is not None:
            raw = next(item for item in payload.telegram_raw_deliveries if item.update_id == metadata.telegram.update_id)
            telegram_envelope = {
                "proof": proof_envelope,
                "raw_update": raw.model_dump(mode="json"),
            }
            # A normalized row must fit the worker's bounded single-record budget.
            persisted_size = len(json.dumps(
                {**record_data, "_native_telegram": telegram_envelope},
                ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8"))
            if persisted_size > 4 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="Telegram observation exceeds normalization size limit")
        provider_records.append((record, record_data, telegram_envelope))

    batch = IngestionBatch(
        source_id=source.id, batch_key=batch_key, payload_hash=payload_hash,
        source_generation=source.generation,
    )
    session.add(batch)
    await session.flush()
    run_status = "queued" if provider_records else "succeeded"
    run = IngestionRun(batch_id=batch.id, source_id=source.id, status=run_status)
    session.add(run)
    await session.flush()
    receive_stage = IngestionStage(
        run_id=run.id, stage_key="receive",
        status="pending" if provider_records else "succeeded",
        result_count=len(provider_records),
    )
    session.add(receive_stage)
    await session.flush()
    received_at = now
    seen: set[tuple[str, str, datetime]] = set()
    for record, record_data, telegram_envelope in provider_records:
        record_hash = _digest({
            "version": record.version, "content": record.content,
            "metadata": record_data["metadata"],
        })
        identity = (record.provider_id, record_hash, record.observed_at)
        if identity in seen:
            continue
        seen.add(identity)
        observation_payload = dict(record_data)
        if telegram_envelope is not None:
            # Keep owner proof outside the caller record so the accepted hash remains stable.
            observation_payload["_native_telegram"] = telegram_envelope
        session.add(SourceObservation(
            source_id=source.id, batch_id=batch.id,
            provider_id=record.provider_id, record_hash=record_hash,
            payload=observation_payload, observed_at=record.observed_at,
            received_at=received_at, collected_at=payload.collected_at,
        ))
    changes = [make_source_change(source.id, source.generation, source.status)]
    if provider_records:
        event = DomainEvent(
            id=uuid4(), type="ingestion.stage.requested", version=1,
            occurred_at=now, producer="modules.ingestion",
            payload={
                "run_id": str(run.id), "stage_id": str(receive_stage.id),
                "source_generation": source.generation,
                "connector_revision": payload.connector_revision,
            },
        )
        await publish_event(session, event)
        await session.flush()
        normalize_stage = await schedule_normalization(
            session, run, batch, source.generation, now
        )
        if normalize_stage is not None:
            changes.append(make_ingestion_change(
                source.id, run.id, run.status, normalize_stage.stage_key, normalize_stage.status
            ))
        state.lease_run_id = run.id
        state.collection_lease_token = None
        state.lease_expires_at = now + COLLECTION_LEASE
        changes.append(make_ingestion_change(
            source.id, run.id, run.status, receive_stage.stage_key, receive_stage.status
        ))
    else:
        state.lease_run_id = None
        state.collection_lease_token = None
        state.lease_expires_at = None
        changes.append(make_ingestion_change(
            source.id, run.id, run.status, receive_stage.stage_key, receive_stage.status
        ))
    state.cursor = cursor_after
    if github_proof is not None and github_proof.hint_claim is not None:
        from modules.connectors.public import GitHubHintClaim

        try:
            hint_claim = GitHubHintClaim.model_validate(github_proof.hint_claim.model_dump(mode="python"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail="GitHub hint claim is stale") from exc
        needs_visibility_fence = (
            github_proof.target_outcome == "forbidden"
            or github_proof.hint_claim.intent == "reconcile"
            and github_proof.hint_claim.locator_kind == "repository"
            and github_proof.target_outcome == "not_found"
            or github_proof.hint_claim.intent in {"refresh", "delete_candidate"}
            and github_proof.hint_claim.locator_kind in {"number", "release_id", "sha"}
            and github_proof.target_outcome in {"not_found", "forbidden", "partial"}
            or github_proof.hint_claim.intent == "visibility_check"
            and github_proof.target_outcome == "not_found"
            and github_proof.hint_claim.locator_kind in {"repository", "installation"}
        )
        deletion_unverified = (
            github_proof.hint_claim.intent == "delete_candidate"
            and github_proof.target_outcome != "found"
        )
        # A signed delete hint plus a missing/forbidden read lacks the exact current version proof required for a tombstone.
        visibility_unverified = (
            github_proof.target_outcome in {"not_found", "forbidden", "partial"}
        )
        disposition = (
            "visibility_unverified"
            if deletion_unverified or visibility_unverified
            else "accepted_ingestion" if provider_records else "completed"
        )
        if not await connectors.acknowledge_github_hint(
            session, claim=hint_claim, batch_id=batch.id, disposition=disposition,
            reconcile_next_page=(
                github_proof.next_page
                if hint_claim.intent == "reconcile" and github_proof.has_next else None
            ),
        ):
            raise HTTPException(status_code=409, detail="GitHub hint claim changed during acceptance")
        # A current target 404 may mean deletion or lost private-repository access; pausing fences
        # all current evidence while retained owner history remains available for review.
        if needs_visibility_fence:
            paused = await sources.pause_source_for_connector(session, source.id)
            if paused is None:
                raise HTTPException(status_code=409, detail="GitHub source changed during visibility confirmation")
            changes[0] = make_source_change(paused.id, paused.generation, paused.status)
    await sources.record_collection_result(
        session, source.id, source.generation, now, None, no_changes=not provider_records
    )
    await commit_with_replay(session, changes)
    return NativeCollectionReceipt(
        batch_id=batch.id, run_id=run.id,
        status="queued" if provider_records else ("succeeded" if payload.telegram_raw_deliveries else "no_changes"),
        received_update_count=len(payload.telegram_raw_deliveries),
        record_count=len(provider_records), coverage=payload.coverage,
        cursor_after=cursor_after,
    )


async def receive_batch(
    session: AsyncSession,
    payload: ReceiveBatch,
    collector_token: str,
) -> tuple[IngestionBatch, IngestionRun]:
    """Authenticate and idempotently accept a fenced collection batch.

    Enforces active source/generation, collector token, and connector fence
    before duplicate lookup. Exact duplicate keys return the existing batch/run
    before new-work cursor/lease checks and without committing new work; native
    provider sources fail closed to the native reservation path. Differing
    payload hashes raise HTTP 409. A new batch deduplicates identical
    provider/hash/observation-time records, advances the cursor and lease, writes
    stage/outbox state, and commits with realtime changes before returning.
    """
    source = await _lock_source_projection(session, payload.source_id)
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

    if connectors.is_native_provider(source.provider):
        raise HTTPException(status_code=409, detail="Native provider collection is required")

    if not await connectors.require_batch_fence(
        session,
        source,
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
    if (
        (state.lease_run_id is not None and state.lease_expires_at is None)
        or (state.collection_lease_token is not None and state.lease_expires_at is None)
    ):
        raise HTTPException(status_code=409, detail="Source collection ownership state is invalid")
    if state.lease_expires_at is not None and state.lease_expires_at > now:
        raise HTTPException(status_code=409, detail="Source already has an active collection run")
    if state.cursor != payload.cursor_before:
        raise HTTPException(status_code=409, detail="Collection cursor is stale")
    if not await sources.record_collection_started(session, payload.source_id, source.generation, now):
        raise HTTPException(status_code=409, detail="Source is not active")

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
                collected_at=record.collected_at,
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
        .values(
            cursor=payload.cursor_after, lease_run_id=run.id,
            collection_lease_token=None, lease_expires_at=now + COLLECTION_LEASE,
        )
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
    """Idempotently queue a generic crawl keyed by source, cursor, config, and minute.

    A matching batch/run receipt returns without the new-work commit. New work
    persists its stage, request event, cursor lease and realtime updates in this
    function's commit; stale source, connector revision, cursor, or active-lease
    checks raise HTTP 404/409; native sources must use their provider adapter.
    """
    source = await _lock_source_projection(session, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if source.status != "active":
        raise HTTPException(status_code=409, detail="Source is not active")
    from modules.connectors import public as connectors
    from modules.connectors.public import CollectionFence

    if connectors.is_native_provider(source.provider):
        raise HTTPException(status_code=409, detail="Native provider collection is required")

    if not await connectors.require_collection_fence(
        session,
        source,
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
    now = datetime.now(UTC)
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
    if (
        (state.lease_run_id is not None and state.lease_expires_at is None)
        or (state.collection_lease_token is not None and state.lease_expires_at is None)
    ):
        raise HTTPException(status_code=409, detail="Source collection ownership state is invalid")
    if state.lease_expires_at is not None and state.lease_expires_at > now:
        raise HTTPException(status_code=409, detail="Source already has an active collection run")
    if state.cursor != cursor_before:
        raise HTTPException(status_code=409, detail="Collection cursor is stale")
    if not await sources.record_collection_started(session, source_id, source.generation, now):
        raise HTTPException(status_code=409, detail="Source is not active")

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
    state.collection_lease_token = None
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

    Locks source, collection state, run, then stage and requires a durable prior event. Returns
    None when the run is absent; no-op paths return the existing run for active,
    successful, or otherwise non-retryable work. Accepted retries reset attempts/errors, copy the prior
    event payload into a fresh outbox event, refresh collection leases when
    needed, and commit. Failed normalization progress requiring correction and
    stale source generations are rejected with HTTP 409.
    """
    run_hint = await session.get(IngestionRun, run_id)
    if run_hint is None:
        return None
    # Source lock serializes retry against reservation acquisition and source purge.
    source = await sources.lock_source(session, run_hint.source_id)
    if source is None or source.status != "active":
        raise HTTPException(status_code=409, detail="Source is not active")
    state = await session.get(SourceIngestionState, run_hint.source_id, with_for_update=True)
    now = datetime.now(UTC)
    if state is not None and state.collection_lease_token is not None:
        if state.lease_expires_at is None or state.lease_expires_at > now:
            raise HTTPException(status_code=409, detail="Source collection is already in progress")
        state.collection_lease_token = None
        state.lease_expires_at = None
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
    failed = sorted(
        (stage for stage in stages if stage.status == "failed"),
        key=lambda stage: (_RUN_RETRY_ORDER.get(stage.stage_key, 100), stage.stage_key),
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
    if stage.stage_key in {"receive", "collect_web"} and state is not None:
        now = datetime.now(UTC)
        if state.lease_run_id is not None and state.lease_expires_at is None:
            raise HTTPException(status_code=409, detail="Source collection ownership state is invalid")
        if state.lease_run_id not in (None, run.id) and state.lease_expires_at and state.lease_expires_at > now:
            raise HTTPException(status_code=409, detail="Source already has an active collection run")
        if state.lease_expires_at is not None and state.lease_expires_at > now and state.lease_run_id != run.id:
            raise HTTPException(status_code=409, detail="Source already has an active collection run")
        state.lease_run_id = run.id
        state.collection_lease_token = None
        state.lease_expires_at = now + COLLECTION_LEASE
    run.status = "queued"
    run.error_code = None
    await commit_with_replay(
        session,
        [make_ingestion_change(source.id, run.id, run.status, stage.stage_key, stage.status)],
    )
    await session.refresh(run)
    return run


async def list_ready_events_after(
    session: AsyncSession, position: tuple[datetime, UUID] | None, limit: int = 100,
) -> list[tuple[datetime, UUID, str, dict[str, str] | None]]:
    """Read-only cursor page of ``document.version.ready`` outbox rows for the automations sweep.

    Ordered by ``(created_at, id)`` strictly after ``position``; returns ``(ts, id, key, payload)``
    with metadata ids only (source and document id). It never changes delivery status, so the single outbox consumer is
    unaffected.
    """
    stmt = select(EventOutbox).where(EventOutbox.type == "document.version.ready")
    if position is not None:
        stmt = stmt.where(tuple_(EventOutbox.created_at, EventOutbox.id) > tuple_(*position))
    rows = (await session.scalars(stmt.order_by(EventOutbox.created_at, EventOutbox.id).limit(limit))).all()
    # A malformed row keeps its slot with a None payload so the sweep cursor still advances past it.
    return [(r.created_at, r.id, str(r.id),
             {"source_id": str(r.payload["source_id"]), "document_id": str(r.payload["document_id"])}
             if "source_id" in r.payload and "document_id" in r.payload else None) for r in rows]


async def list_terminal_runs_after(
    session: AsyncSession, position: tuple[datetime, UUID] | None, limit: int = 100,
) -> list[tuple[datetime, UUID, str, dict[str, str]]]:
    """Read-only cursor page of ingestion runs in a terminal state for connector sync results.

    Ordered by ``(updated_at, id)``; the key combines run id and status so a later status change
    is a new event. Payload carries source id, status and the collected-observation count only.
    """
    stmt = select(IngestionRun).where(IngestionRun.status.in_(("succeeded", "failed", "needs_ocr")))
    if position is not None:
        stmt = stmt.where(tuple_(IngestionRun.updated_at, IngestionRun.id) > tuple_(*position))
    rows = (await session.scalars(stmt.order_by(IngestionRun.updated_at, IngestionRun.id).limit(limit))).all()
    # new_items = observations collected in the run's batch (one grouped count for the page).
    counts = dict((await session.execute(
        select(SourceObservation.batch_id, func.count()).where(
            SourceObservation.batch_id.in_([r.batch_id for r in rows])).group_by(SourceObservation.batch_id)
    )).all()) if rows else {}
    return [(r.updated_at, r.id, f"{r.id}:{r.status}",
             {"source_id": str(r.source_id), "status": r.status, "new_items": int(counts.get(r.batch_id, 0))})
            for r in rows]


async def list_run_meta(session: AsyncSession, limit: int) -> list[_RunMeta]:
    """Return at most ``limit`` (<=100) newest ingestion runs as metadata only: ID, status, error code, timestamps."""
    rows = await session.scalars(select(IngestionRun).order_by(IngestionRun.created_at.desc()).limit(min(limit, 100)))
    return [_RunMeta(kind="ingestion", id=str(r.id), status=r.status, error_code=r.error_code,
                     created_at=r.created_at, updated_at=r.updated_at,
                     finished_at=r.updated_at if r.status in {"succeeded", "failed", "needs_ocr"} else None)
            for r in rows]


async def observability_quality_summary(session: AsyncSession) -> dict[str, int | float]:
    """Return ingestion-owned duplicate and failed-run aggregates without exposing payloads."""
    normalized, duplicates = (await session.execute(select(
        func.count().filter(ObservationNormalization.disposition.in_(("normalized", "duplicate"))),
        func.count().filter(ObservationNormalization.disposition == "duplicate"),
    ))).one()
    failed_runs = int(await session.scalar(select(func.count()).select_from(IngestionRun).where(
        IngestionRun.status == "failed"
    )) or 0)
    return {"duplicate_rate": float(duplicates or 0) / int(normalized or 1), "failed_ingestion": failed_runs}


async def observability_queue_summary(session: AsyncSession, *, now: datetime | None = None) -> dict[str, object]:
    """Return durable ingestion counts and retries eligible under the retry owner's latest-event rules."""
    now = now or datetime.now(UTC)
    source_lifecycle = sources.ingestion_lifecycle_projection().subquery("source_lifecycle")
    stage_counts = dict((await session.execute(
        select(IngestionStage.status, func.count()).group_by(IngestionStage.status)
    )).all())
    active_stage = aliased(IngestionStage)
    active_stage_exists = select(1).select_from(active_stage).where(
        active_stage.run_id == IngestionStage.run_id,
        active_stage.status.in_(("pending", "queued", "running", "retrying")),
    ).exists()
    failed_progress_exists = select(1).where(
        ObservationNormalization.stage_id == IngestionStage.id,
        ObservationNormalization.disposition == "failed",
    ).exists()
    latest_event = aliased(EventOutbox)
    latest_event_id = (select(latest_event.id).where(
        latest_event.payload["stage_id"].astext == cast(IngestionStage.id, String),
    ).order_by(latest_event.created_at.desc()).limit(1).correlate(IngestionStage).scalar_subquery())
    latest_event_matches = select(1).select_from(EventOutbox).where(
        EventOutbox.id == latest_event_id,
        EventOutbox.payload["source_generation"].astext == cast(source_lifecycle.c.generation, String),
    ).exists()
    active_collection_exists = select(1).select_from(SourceIngestionState).where(
        SourceIngestionState.source_id == source_lifecycle.c.id,
        SourceIngestionState.collection_lease_token.is_not(None),
        or_(SourceIngestionState.lease_expires_at.is_(None), SourceIngestionState.lease_expires_at > now),
    ).exists()
    other_run_lease_exists = select(1).select_from(SourceIngestionState).where(
        SourceIngestionState.source_id == source_lifecycle.c.id,
        SourceIngestionState.lease_run_id.is_not(None),
        SourceIngestionState.lease_run_id != IngestionRun.id,
        or_(SourceIngestionState.lease_expires_at.is_(None), SourceIngestionState.lease_expires_at > now),
    ).exists()
    failed_candidate = aliased(IngestionStage)
    retry_order = case(*((IngestionStage.stage_key == key, rank)
                         for key, rank in _RUN_RETRY_ORDER.items()), else_=100)
    candidate_order = case(*((failed_candidate.stage_key == key, rank)
                             for key, rank in _RUN_RETRY_ORDER.items()), else_=100)
    earlier_failed_exists = select(1).select_from(failed_candidate).where(
        failed_candidate.run_id == IngestionStage.run_id,
        failed_candidate.status == "failed",
        or_(candidate_order < retry_order,
            and_(candidate_order == retry_order, failed_candidate.stage_key < IngestionStage.stage_key)),
    ).exists()
    retryable = int(await session.scalar(
        select(func.count()).select_from(IngestionStage)
        .join(IngestionRun, IngestionRun.id == IngestionStage.run_id)
        .join(source_lifecycle, source_lifecycle.c.id == IngestionRun.source_id)
        .where(
            IngestionStage.status == "failed", source_lifecycle.c.status == "active",
            not_(active_stage_exists), not_(earlier_failed_exists), latest_event_matches,
            or_(IngestionStage.stage_key != "normalize", not_(failed_progress_exists)),
            not_(active_collection_exists),
            or_(IngestionStage.stage_key.not_in(("receive", "collect_web")), not_(other_run_lease_exists)),
        )
    ) or 0)
    event_delivery = dict((await session.execute(
        select(EventOutbox.status, func.count()).group_by(EventOutbox.status)
    )).all())
    return {"ingestion_stages": stage_counts, "retryable_ingestion_stages": retryable,
            "event_delivery": event_delivery}


async def get_run_meta_by_id(session: AsyncSession, run_id: UUID) -> _RunMeta | None:
    """Return one metadata-only ingestion-run projection by its indexed primary key."""
    row = await session.get(IngestionRun, run_id)
    if row is None:
        return None
    return _RunMeta(kind="ingestion", id=str(row.id), status=row.status, error_code=row.error_code,
                    created_at=row.created_at, updated_at=row.updated_at,
                    finished_at=row.updated_at if row.status in {"succeeded", "failed", "needs_ocr"} else None)
