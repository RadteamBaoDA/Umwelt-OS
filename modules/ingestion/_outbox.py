"""Outbox delivery, ready-event reads, normalization scheduling and cleanup-event settlement (internal)."""

from collections.abc import Collection
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid5

from sqlalchemy import String, cast, func, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ConflictError, NotFoundError
from core.events import DomainEvent
from core.ids import uuid7
from core.workspaces.schemas import AccessFence, Scope
from modules.ingestion._event_publisher import (
    NORMALIZATION_VERSION,
    _ready_document_payload,
    publish_event,
)
from modules.ingestion._scope import (
    _SOURCE_PURGE_PRODUCERS,
    _admit_ingestion_scope,
    _cleanup_event_scope,
    _event_scope,
    _materialization_scope,
    _run_scope,
    _source_purge_event_subject,
)
from modules.ingestion.models import (
    EventOutbox,
    IngestionBatch,
    IngestionRun,
    IngestionStage,
    ObservationNormalization,
    SourceObservation,
)
from modules.ingestion.schemas import (
    EventDelivery,
)
from modules.knowledge.documents import public as documents
from modules.knowledge.documents.public import DocumentCleanupPreparationLimitError
from modules.sources import public as sources
from modules.sources.public import SourceFence


@dataclass(frozen=True)
class NewsDocumentReadyEvent:
    """Detached fixed-shape News readiness event owned by Ingestion."""

    id: UUID
    workspace_id: UUID
    actor_user_id: int
    membership_revision: int
    version: int
    status: str
    payload: dict[str, Any]
    valid_payload: bool


@dataclass(frozen=True)
class ReadyDocumentProvenance:
    """Detached proof joining one retained ready event to its exact Document version."""

    event_id: UUID
    workspace_id: UUID
    actor_user_id: int
    membership_revision: int
    source_id: UUID
    document_id: UUID
    document_version_id: UUID
    source_generation: int
    version_number: int


async def lock_news_document_ready_event(
    session: AsyncSession,
    event_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> NewsDocumentReadyEvent | None:
    """Lock one News readiness outbox row and return only its bounded event payload.

    The event table remains private to Ingestion. Invalid or oversized payloads
    return a detached DTO with valid_payload false so the consumer can terminally
    fail the receipt without parsing arbitrary or unbounded JSON fields.
    """
    await _admit_ingestion_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    event = await session.scalar(
        select(EventOutbox)
        .where(
            EventOutbox.id == event_id,
            EventOutbox.type == "news.document.ready",
            *_event_scope(scope),
        )
        .with_for_update()
    )
    if event is None:
        return None
    validated = _ready_document_payload(event)
    valid = validated is not None
    detached = validated if validated is not None else {}
    return NewsDocumentReadyEvent(
        workspace_id=event.workspace_id,
        actor_user_id=event.actor_user_id,
        membership_revision=event.membership_revision,
        id=event.id,
        version=event.version,
        status=event.status,
        payload=detached,
        valid_payload=bool(valid),
    )


async def mark_news_document_ready_event_delivered(
    session: AsyncSession,
    event_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> bool:
    """Flush News event acknowledgement without committing the caller's transaction."""
    await _admit_ingestion_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    event = await session.scalar(
        select(EventOutbox)
        .where(
            EventOutbox.id == event_id,
            EventOutbox.type == "news.document.ready",
            *_event_scope(scope),
        )
        .with_for_update()
    )
    if event is None:
        return False
    event.status = "delivered"
    await session.flush()
    return True


async def fail_news_document_ready_event(
    session: AsyncSession,
    event_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> bool:
    """Flush a terminal invalid News receipt state while leaving commit to the caller."""
    await _admit_ingestion_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    event = await session.scalar(
        select(EventOutbox)
        .where(
            EventOutbox.id == event_id,
            EventOutbox.type == "news.document.ready",
            *_event_scope(scope),
        )
        .with_for_update()
    )
    if event is None:
        return False
    event.status = "failed"
    await session.flush()
    return True


async def schedule_normalization(
    session: AsyncSession,
    run: IngestionRun,
    batch: IngestionBatch,
    source_generation: int,
    received_at: datetime,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> IngestionStage | None:
    """Create or reuse normalization work and idempotent observation progress.

    Compare supplied run/batch/source/generation against scoped owner rows before any
    observation query. Empty batches produce no stage; a new stage emits one durable
    request event. Caller holds earlier admission/Source/run locks and owns commit.
    """
    await _admit_ingestion_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    authorized_run = await session.scalar(
        select(IngestionRun).where(
            IngestionRun.id == run.id,
            IngestionRun.batch_id == batch.id,
            IngestionRun.source_id == batch.source_id,
            *_run_scope(scope),
        )
    )
    authorized_batch = await session.scalar(
        select(IngestionBatch.id).where(
            IngestionBatch.id == batch.id,
            IngestionBatch.source_id == run.source_id,
            IngestionBatch.source_generation == source_generation,
        )
    )
    if authorized_run is None or authorized_batch is None:
        raise NotFoundError(detail="Normalization lineage not found")
    observations = list(
        (
            await session.scalars(
                select(SourceObservation)
                .where(
                    SourceObservation.batch_id == batch.id,
                    SourceObservation.source_id == run.source_id,
                )
                .order_by(SourceObservation.id)
            )
        ).all()
    )
    if not observations:
        return None
    stage = await session.scalar(
        select(IngestionStage).where(
            IngestionStage.run_id == run.id, IngestionStage.stage_key == "normalize"
        )
    )
    if stage is None:
        stage = IngestionStage(run_id=run.id, stage_key="normalize", status="pending")
        session.add(stage)
        await session.flush()
        event = DomainEvent(
            id=uuid7(),
            type="ingestion.normalize.requested",
            version=1,
            occurred_at=received_at,
            producer="modules.ingestion",
            payload={
                "run_id": str(run.id),
                "stage_id": str(stage.id),
                "source_generation": source_generation,
                "normalization_version": NORMALIZATION_VERSION,
            },
        )
        await publish_event(
            session, event, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
    existing_ids = set(
        (
            await session.scalars(
                select(ObservationNormalization.observation_id).where(
                    ObservationNormalization.stage_id == stage.id,
                    *_materialization_scope(scope),
                    ObservationNormalization.normalization_version == NORMALIZATION_VERSION,
                )
            )
        ).all()
    )
    for observation in observations:
        if observation.id not in existing_ids:
            session.add(
                ObservationNormalization(
                    workspace_id=scope.workspace_id,
                    observation_id=observation.id,
                    source_id=observation.source_id,
                    run_id=run.id,
                    stage_id=stage.id,
                    source_generation=source_generation,
                    normalization_version=NORMALIZATION_VERSION,
                )
            )
        if observation.received_at is None:
            observation.received_at = received_at
    await session.flush()
    return stage


async def prepare_document_materializations_in_uow(
    session: AsyncSession,
    document_id: UUID,
    *,
    source_id: UUID,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
) -> None:
    """Lock the Document's materializations in id order before ``tombstone_document_materializations``.

    Individual-delete path only, so an oversized set raises before any mutation. The Document
    owner holds admission, Source and earlier owner locks; this commits nothing.
    """
    actual = await _admit_ingestion_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if (
        actual != access_fence
        or source_fence.id != source_id
        or source_fence.workspace_id != scope.workspace_id
    ):
        raise ConflictError(detail="Cleanup authority changed")
    ids = list(
        (
            await session.scalars(
                select(ObservationNormalization.id)
                .where(
                    ObservationNormalization.document_id == document_id,
                    *_materialization_scope(scope),
                )
                .order_by(ObservationNormalization.id)
                .limit(10_001)
                .with_for_update(of=ObservationNormalization)
            )
        ).all()
    )
    if len(ids) > 10_000:
        raise DocumentCleanupPreparationLimitError("ingestion")


async def tombstone_document_materializations(
    session: AsyncSession,
    document_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Tombstone materializations through exact retained run/stage/batch/workspace lineage.

    The Document owner holds earlier admission/Source/deletion locks. No earlier lock
    acquisition or commit occurs; nullable document/version IDs retain owner scope. Rows were
    locked by prepare_document_materializations_in_uow.
    """
    await _admit_ingestion_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    await session.execute(
        update(ObservationNormalization)
        .where(ObservationNormalization.document_id == document_id, *_materialization_scope(scope))
        .values(
            disposition="skipped",
            error_code="document_deleted",
            document_id=None,
            document_version_id=None,
        )
    )


async def get_event_delivery(
    session: AsyncSession,
    event_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> EventDelivery | None:
    """Read scoped outbox identity with a defensive payload copy and no row locks.

    Current owner admission and retained actor/membership must match; foreign IDs
    return None. Event type/version/producer and nullable dispatch timestamp are captured,
    but this DTO does not prove held locks, a current dispatch claim or a still-live Source.
    """
    await _admit_ingestion_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    event = await session.scalar(
        select(EventOutbox).where(EventOutbox.id == event_id, *_event_scope(scope))
    )
    if event is None:
        return None
    return EventDelivery(
        id=event.id,
        workspace_id=event.workspace_id,
        actor_user_id=event.actor_user_id,
        membership_revision=event.membership_revision,
        type=event.type,
        version=event.version,
        producer=event.producer,
        dispatched_at=event.dispatched_at,
        status=event.status,
        payload=deepcopy(event.payload),
    )


async def lock_source_purge_event_in_uow(
    session: AsyncSession,
    event_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    event_types: tuple[str, ...],
) -> EventDelivery | None:
    """Lock the complete existing purge/run/coverage event union in one ascending UUID order.

    Caller holds admission, optional Memory privacy, Source and exact retained operation;
    canonical cancellation also holds its seven prepared Ingestion row sets. Validate the
    requested six-field subject nonlocking before acquiring any outbox lock. Historical run
    events include all epochs/generations but only this workspace/actor and Source lineage;
    their bodies are never loaded into Python. Include an existing deterministic coverage
    row so later arming acquires no omitted lower UUID. Fresh requested/coverage validation
    rejects collisions or a changed subject. Caller still compares original queued status/
    dispatched_at before effects. No early locks, mutation, commit or I/O; total SQL work
    grows with Source history while Python discovery stays bounded.
    """
    if (
        not isinstance(event_types, tuple)
        or not event_types
        or any(
            not isinstance(event_type, str) or event_type not in _SOURCE_PURGE_PRODUCERS
            for event_type in event_types
        )
    ):
        raise ValueError("Only explicit Source purge event types are allowed")
    await _admit_ingestion_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    event = await session.scalar(
        select(EventOutbox)
        .where(
            EventOutbox.id == event_id,
            EventOutbox.type.in_(event_types),
            *_event_scope(scope),
        )
        .execution_options(populate_existing=True)
    )
    if event is None:
        return None
    subject = _source_purge_event_subject(
        event.type,
        event.version,
        event.producer,
        event.payload,
        scope=scope,
    )
    if subject is None:
        return None
    operation_id, retained = subject
    actual = await sources.read_source_purge_job_identity(
        session,
        operation_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if actual != retained:
        return None
    captured_envelope = (event.type, event.version, event.producer)
    coverage_id = uuid5(operation_id, "source-memory-coverage")
    coverage_exists = (
        await session.scalar(select(EventOutbox.id).where(EventOutbox.id == coverage_id))
        is not None
    )
    if coverage_exists:
        coverage = await session.scalar(
            select(EventOutbox)
            .where(
                EventOutbox.id == coverage_id,
                *_event_scope(retained),
            )
            .execution_options(populate_existing=True)
        )
        if (
            coverage is None
            or coverage.type != "source.purge.coverage"
            or _source_purge_event_subject(
                coverage.type, coverage.version, coverage.producer, coverage.payload, scope=retained
            )
            != subject
        ):
            raise ConflictError(detail="Source coverage event identity changed")
    run_ids = select(cast(IngestionRun.id, String)).where(
        IngestionRun.source_id == retained.source_id,
        IngestionRun.workspace_id == retained.workspace_id,
    )
    # Sort and lock actual rows inside PostgreSQL; COUNT consumes the complete lock CTE.
    # Do not lock the requested row first, or materialize historical event bodies/IDs in Python.
    locked_events = (
        select(EventOutbox.id)
        .where(
            EventOutbox.workspace_id == retained.workspace_id,
            EventOutbox.actor_user_id == retained.actor_user_id,
            or_(
                EventOutbox.id == event_id,
                EventOutbox.id == coverage_id,
                EventOutbox.payload["run_id"].astext.in_(run_ids),
            ),
        )
        .order_by(EventOutbox.id)
        .with_for_update()
        .cte("source_purge_event_locks")
        .prefix_with("MATERIALIZED")
    )
    await session.scalar(select(func.count()).select_from(locked_events))
    event = await session.scalar(
        select(EventOutbox)
        .where(
            EventOutbox.id == event_id,
            EventOutbox.type.in_(event_types),
            *_event_scope(scope),
        )
        .execution_options(populate_existing=True)
    )
    if event is None or (event.type, event.version, event.producer) != captured_envelope:
        return None
    fresh_subject = _source_purge_event_subject(
        event.type, event.version, event.producer, event.payload, scope=scope
    )
    if (
        fresh_subject != subject
        or await sources.read_source_purge_job_identity(
            session,
            operation_id,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        != retained
    ):
        return None
    if coverage_exists:
        coverage = await session.scalar(
            select(EventOutbox)
            .where(
                EventOutbox.id == coverage_id,
                *_event_scope(retained),
            )
            .execution_options(populate_existing=True)
        )
        if (
            coverage is None
            or coverage.type != "source.purge.coverage"
            or _source_purge_event_subject(
                coverage.type, coverage.version, coverage.producer, coverage.payload, scope=retained
            )
            != subject
        ):
            raise ConflictError(detail="Source coverage event identity changed")
    return EventDelivery(
        id=event.id,
        workspace_id=event.workspace_id,
        actor_user_id=event.actor_user_id,
        membership_revision=event.membership_revision,
        type=event.type,
        version=event.version,
        producer=event.producer,
        dispatched_at=event.dispatched_at,
        status=event.status,
        payload=deepcopy(event.payload),
    )


async def set_event_delivery(
    session: AsyncSession,
    event_id: UUID,
    status: Literal["failed", "pending", "delivered"],
    *,
    next_attempt_at: datetime | None = None,
    from_statuses: Collection[str] = ("pending", "queued"),
    scope: Scope,
    multi_workspace_enabled: bool,
) -> bool:
    """Compare-and-set exact scoped outbox status/retry time without earlier locks or commit.

    Caller holds ordered admission/domain locks; nonlocking revision revalidation
    cannot rebase a retained epoch. The row only transitions from ``from_statuses`` (default the
    non-terminal ``pending``/``queued``), so a stale or duplicate call cannot overwrite a terminal
    ``delivered``/``failed`` status; deliberate reopen callers pass the terminal statuses explicitly.
    Foreign rows and rows not in an expected prior status return False (no write).
    """
    await _admit_ingestion_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    values: dict[str, object] = {"status": status}
    if next_attempt_at is not None:
        values["next_attempt_at"] = next_attempt_at
    result = await session.execute(
        update(EventOutbox)
        .where(
            EventOutbox.id == event_id,
            EventOutbox.status.in_(tuple(from_statuses)),
            *_event_scope(scope),
        )
        .values(**values)
        .returning(EventOutbox.id)
    )
    return result.scalar_one_or_none() is not None


def _event_delivery(event: EventOutbox) -> EventDelivery:
    """Detach an outbox row into the immutable ``EventDelivery`` DTO (payload deep-copied; no lock or I/O)."""
    return EventDelivery(
        id=event.id,
        workspace_id=event.workspace_id,
        actor_user_id=event.actor_user_id,
        membership_revision=event.membership_revision,
        type=event.type,
        version=event.version,
        producer=event.producer,
        dispatched_at=event.dispatched_at,
        status=event.status,
        payload=deepcopy(event.payload),
    )


async def read_document_cleanup_event_operation_id(
    session: AsyncSession, event_id: UUID
) -> UUID | None:
    """Identity-only discovery of the operation id in an exact cleanup envelope (before admission).

    Reads only the bounded id text of the deterministic cleanup event; a reshaped event or a
    mismatched deterministic id returns None. It grants no authority: callers still resolve scope.
    """
    row = (
        await session.execute(
            select(
                func.left(EventOutbox.payload["operation_id"].astext, 37),
            ).where(EventOutbox.id == event_id, EventOutbox.type == "document.cleanup.requested")
        )
    ).scalar_one_or_none()
    try:
        operation_id = UUID(row)
    except (TypeError, ValueError):
        return None
    if str(operation_id) != row or event_id != uuid5(operation_id, "document-cleanup-requested"):
        return None
    return operation_id


async def get_document_cleanup_event(
    session: AsyncSession,
    event_id: UUID,
    *,
    operation_id: UUID,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> EventDelivery | None:
    """Read one exact Documents cleanup event nonlocking; foreign or reshaped rows return None."""
    await _admit_ingestion_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    event = await session.scalar(
        select(EventOutbox)
        .where(
            *_cleanup_event_scope(event_id, operation_id, scope),
        )
        .execution_options(populate_existing=True)
    )
    return None if event is None else _event_delivery(event)


async def lock_document_cleanup_event_in_uow(
    session: AsyncSession,
    event_id: UUID,
    *,
    operation_id: UUID,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> EventDelivery | None:
    """Lock that single cleanup outbox row after the caller's privacy/URI/receipt locks."""
    await _admit_ingestion_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    event = await session.scalar(
        select(EventOutbox)
        .where(
            *_cleanup_event_scope(event_id, operation_id, scope),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return None if event is None else _event_delivery(event)


async def settle_document_cleanup_event_in_uow(
    session: AsyncSession,
    event_id: UUID,
    status: Literal["failed", "pending", "delivered"],
    *,
    operation_id: UUID,
    dispatched_at: datetime | None,
    next_attempt_at: datetime | None = None,
    expected_status: Literal["queued", "pending", "delivered", "failed"] = "queued",
    scope: Scope,
    multi_workspace_enabled: bool,
) -> bool:
    """Compare-and-set a cleanup event from its claimed state (default queued); False means the claim was lost.

    ``expected_status`` lets the cache-completion and reconciler paths act on a pending, delivered
    or failed row; the exact ``dispatched_at`` claim stamp is always part of the comparison.
    """
    await _admit_ingestion_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    values: dict[str, object] = {"status": status}
    if next_attempt_at is not None:
        values["next_attempt_at"] = next_attempt_at
    result = await session.execute(
        update(EventOutbox)
        .where(
            *_cleanup_event_scope(event_id, operation_id, scope),
            EventOutbox.status == expected_status,
            EventOutbox.dispatched_at.is_not_distinct_from(dispatched_at),
        )
        .values(**values)
        .returning(EventOutbox.id)
    )
    return result.scalar_one_or_none() is not None


async def mark_event_delivered(
    session: AsyncSession,
    event_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> bool:
    """Acknowledge the scoped outbox row without committing or acquiring earlier locks.

    Callers hold ordered admission/Source/domain locks and own replay/final commit;
    this is the Ingestion owner seam for atomic worker settlement, not Redis proof.
    """
    return await set_event_delivery(
        session, event_id, "delivered", scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )


async def list_ready_events_after(
    session: AsyncSession,
    position: tuple[datetime, UUID] | None,
    limit: int = 100,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[tuple[datetime, UUID, str, dict[str, Any] | None]]:
    """Read-only keyset page of ``document.version.ready`` outbox rows for the automations sweep.

    Ordered by ``(created_at, id)`` strictly after the caller's ``(created_at, id)`` tuple
    ``position``. The caller (the sweep) keeps that position in its workspace-keyed cursor row,
    so the position is not an opaque token: the workspace predicate comes from ``_event_scope``
    and precedes the ordering and LIMIT, which means a position taken from another workspace can
    only skip rows that belong to the caller's own scope. Owner admission precedes any query.
    Returns validated private metadata projection including the immutable version identity,
    never content. It never changes delivery status, so the single outbox consumer is unaffected.
    """
    await _admit_ingestion_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if not 1 <= limit <= 100:
        raise ValueError("Ready-event page size must be between 1 and 100")
    stmt = select(EventOutbox).where(
        EventOutbox.type == "document.version.ready", *_event_scope(scope)
    )
    if position is not None:
        stmt = stmt.where(tuple_(EventOutbox.created_at, EventOutbox.id) > tuple_(*position))
    rows = (
        await session.scalars(stmt.order_by(EventOutbox.created_at, EventOutbox.id).limit(limit))
    ).all()
    # A malformed row keeps its slot with a None payload so the sweep cursor still advances past it.
    return [(row.created_at, row.id, str(row.id), _ready_document_payload(row)) for row in rows]


async def resolve_ready_event_provenance(
    session: AsyncSession,
    event_id: UUID,
    *,
    document_id: UUID | None = None,
    source_id: UUID | None = None,
    accepted_version_ids: tuple[UUID, ...] = (),
    allow_retained_receipt: bool = False,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> ReadyDocumentProvenance | None:
    """Resolve one strict historical event against a live Documents locator or detached cleanup receipt.

    Detached version IDs are accepted only when the caller supplies both receipt-owned document and
    source IDs. The bounded receipt path remains usable after canonical rows have cascaded away.
    """
    await _admit_ingestion_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if len(accepted_version_ids) > 100:
        raise ValueError("Ready-event receipt membership exceeds its bound")
    if (document_id is None) != (source_id is None):
        raise ValueError("Ready-event receipt matching requires both document and source IDs")
    event = await session.scalar(
        select(EventOutbox).where(EventOutbox.id == event_id, *_event_scope(scope))
    )
    if event is None:
        return None
    payload = _ready_document_payload(event)
    if payload is None:
        return None
    event_source = UUID(payload["source_id"])
    event_document = UUID(payload["document_id"])
    version_id = UUID(payload["document_version_id"])
    if document_id is not None and (
        event_document != document_id
        or event_source != source_id
        or version_id not in accepted_version_ids
    ):
        return None
    locator = await documents.review_version_locator(
        session,
        version_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if locator is None and (allow_retained_receipt or document_id is not None):
        # Retained cleanup lookup filters receipt workspace and exact source/version before LIMIT;
        # supplied version membership alone cannot authorize a foreign receipt.
        retained_document = await documents.cleanup_evidence_version_document(
            session,
            version_id,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            source_id=event_source,
        )
        if retained_document != event_document:
            return None
    elif locator is None or locator != (event_document, event_source):
        return None
    return ReadyDocumentProvenance(
        workspace_id=event.workspace_id,
        actor_user_id=event.actor_user_id,
        membership_revision=event.membership_revision,
        event_id=event.id,
        source_id=event_source,
        document_id=event_document,
        document_version_id=version_id,
        source_generation=payload["source_generation"],
        version_number=payload["version_number"],
    )
