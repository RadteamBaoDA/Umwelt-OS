"""Source-owned purge orchestration; durable principal identity precedes content and effects.

Documents/Memory transaction-local scope hooks and Ingestion delivery claims are mandatory
owner contracts. Missing integration fails closed; this module supplies no legacy fallback.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid5

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.events import DomainEvent
from core.realtime import ReplayDraft, commit_with_replay, make_knowledge_change, make_source_change
from core.workspaces.access import read_access_fence
from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.ingestion import public as ingestion
from modules.ingestion.schemas import EventDelivery
from modules.knowledge.documents import public as documents
from modules.memory import public as memory
from modules.memory.public import SourceCopiedEvidenceScope
from modules.sources import public as sources
from modules.sources.models import Source, SourcePurgeOperation
from modules.sources.schemas import SourceFence

logger = logging.getLogger(__name__)
_CONTINUATION_DELAY = timedelta(seconds=5)
_MEMORY_PAGE_DELAY = timedelta(seconds=1)
_RETRY_DELAY = timedelta(seconds=30)
_PURGE_EVENTS = ("source.purge.requested", "source.purge.progressed")
_COVERAGE_EVENTS = ("source.purge.coverage",)


def _configured_flag(ctx: dict[str, object]) -> bool:
    """Read the actual worker Settings gate; absent/nonboolean configuration cannot authorize."""
    flag = cast(Settings, ctx["settings"]).multi_workspace_enabled
    if type(flag) is not bool:
        raise TypeError("The configured multi-workspace feature flag must be a boolean")
    return flag


def _memory_complete(operation: SourcePurgeOperation) -> bool:
    """Source Memory coverage is complete only after a full exhausted sweep and cache eviction."""
    return operation.memory_status == "succeeded" and not operation.memory_cache_pending


def _memory_unavailable(operation: SourcePurgeOperation) -> bool:
    """Durable unavailable coverage: a failed stage whose code never improves without new evidence."""
    return operation.memory_status == "failed" and operation.memory_error_code in sources.SOURCE_MEMORY_TERMINAL_CODES


def _memory_snapshot(operation: SourcePurgeOperation) -> tuple[object, ...]:
    """Capture canonical capture marker and Memory fields for retry/cache CAS under lock."""
    cursor = operation.memory_cursor
    return (
        operation.memory_status, operation.memory_error_code,
        dict(cursor) if isinstance(cursor, dict) else cursor,
        operation.memory_unresolved_count, operation.memory_cache_pending, operation.documents_status,
    )


def _coverage_event_id(operation_id: UUID) -> UUID:
    """Derive the stable per-operation Source Memory coverage outbox identity."""
    return uuid5(operation_id, "source-memory-coverage")


def _purge_payload(operation_id: UUID, scope: InternalJobScope) -> dict[str, object]:
    """Encode all six captured retained identities without rebasing actor, epoch or generation."""
    if scope.source_id is None or scope.source_generation is None:
        raise ValueError("Source purge requires an exact retained Source subject")
    return {
        "operation_id": str(operation_id), "workspace_id": str(scope.workspace_id),
        "actor_user_id": scope.actor_user_id, "membership_revision": scope.membership_revision,
        "source_id": str(scope.source_id), "source_generation": scope.source_generation,
    }


def _event_operation_id(
    event: EventDelivery, scope: InternalJobScope, event_types: tuple[str, ...],
) -> UUID:
    """Validate the envelope and all canonical, strictly typed retained payload fields.

    Requested/coverage events are Source-produced; progress wakeups are Documents-produced.
    No old operation-only body, extra key, bool-as-int or noncanonical UUID is accepted.
    Ingestion retains type/version/producer and durable dispatch metadata in its owner DTO.
    """
    producer = "modules.knowledge.documents" if event.type == "source.purge.progressed" else "modules.sources"
    if event.type not in event_types or type(event.version) is not int or event.version != 1 or event.producer != producer:
        raise ValueError("Source purge event envelope is invalid")
    if (event.workspace_id != scope.workspace_id or event.actor_user_id != scope.actor_user_id
            or event.membership_revision != scope.membership_revision):
        raise ValueError("Source purge event principal differs from its retained subject")
    payload = event.payload
    fields = {"operation_id", "workspace_id", "actor_user_id", "membership_revision", "source_id", "source_generation"}
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ValueError("Source purge event requires exact retained identity")
    for field in ("operation_id", "workspace_id", "source_id"):
        value = payload[field]
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError("Source purge UUID must be canonical")
    operation_id = UUID(payload["operation_id"])
    expected = _purge_payload(operation_id, scope)
    if any(type(payload[key]) is not type(value) or payload[key] != value for key, value in expected.items()):
        raise ValueError("Source purge payload differs from its retained subject")
    return operation_id


async def _admit_event(
    session: AsyncSession, event_id: UUID, *, multi_workspace_enabled: bool,
    event_types: tuple[str, ...], expected_scope: InternalJobScope | None = None,
    expected_fence: AccessFence | None = None,
) -> tuple[UUID, InternalJobScope, AccessFence, EventDelivery] | None:
    """Resolve/admit retained outbox identity before content, then compare Source receipt.

    Ingestion acquires real account/workspace/membership locks from identity-only discovery.
    The Source retained nonlocking capture hook compares the exact operation and its captured
    configuration epoch; no earlier lock is reacquired. Missing lineage, a legacy NULL capture
    or a changed epoch cannot become a current-owner job (None, no mutation). A caller that
    admitted before a rollback passes its snapshot fence as expected_fence; any drift is a no-op.
    Dispatcher owns quarantine of unadmitted/malformed events without protected body access.
    """
    scope = await ingestion.resolve_ingestion_event_scope(
        session, event_id, multi_workspace_enabled=multi_workspace_enabled,
    )
    if scope is None or expected_scope is not None and scope != expected_scope:
        return None
    event = await ingestion.get_event_delivery(
        session, event_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if event is None or event.status == "delivered":
        return None
    operation_id = _event_operation_id(event, scope, event_types)
    capture = await sources.read_source_purge_job_capture(
        session, operation_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if capture is None:
        return None
    try:
        retained = InternalJobScope(
            workspace_id=capture.workspace_id, actor_user_id=capture.actor_user_id,
            membership_revision=capture.membership_revision, source_id=capture.source_id,
            source_generation=capture.source_generation,
        )
    except ValueError:
        return None
    if retained != scope:
        return None
    access_fence = await read_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if access_fence.configuration_revision != capture.configuration_revision:
        return None
    if expected_fence is not None and access_fence != expected_fence:
        return None
    return operation_id, scope, access_fence, event


async def _lock_operation(
    session: AsyncSession, operation_id: UUID, *, scope: InternalJobScope,
    multi_workspace_enabled: bool, access_fence: AccessFence, memory_phase: bool,
    prepare_documents: bool = False,
) -> tuple[Source | None, SourcePurgeOperation | None]:
    """Lock own Source then exact retained receipt under already-held access admission.

    Memory adds actor-scoped privacy before Source. Canonical preparation locks only sorted
    Documents/URI identities, then Ingestion's complete credentials/state/history sets before
    the purge operation and sorted event union; later application consumes these prepared
    rows. A missing/changed live Source remains separate for truthful retained
    unavailable coverage. No foreign ORM, I/O, commit or authorization-lock reentry.
    """
    if memory_phase:
        await memory.lock_export_privacy_in_uow(
            session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
        )
    source = await session.scalar(select(Source).where(
        Source.id == scope.source_id, Source.workspace_id == scope.workspace_id,
    ).with_for_update().execution_options(populate_existing=True))
    if prepare_documents and source is not None and source.generation == scope.source_generation:
        await documents.lock_source_documents_for_purge_in_uow(
            session, source.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence, source_fence=_source_fence(source),
        )
        await ingestion.prepare_source_ingestion_purge_in_uow(
            session, source.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence, source_fence=_source_fence(source),
        )
    operation = await session.scalar(select(SourcePurgeOperation).where(
        SourcePurgeOperation.id == operation_id,
        SourcePurgeOperation.workspace_id == scope.workspace_id,
        SourcePurgeOperation.actor_user_id == scope.actor_user_id,
        SourcePurgeOperation.membership_revision == scope.membership_revision,
        SourcePurgeOperation.source_id == scope.source_id,
        SourcePurgeOperation.generation == scope.source_generation,
    ).with_for_update().execution_options(populate_existing=True))
    return source, operation


async def _lock_delivery(
    session: AsyncSession, event_id: UUID, operation_id: UUID, *, scope: InternalJobScope,
    multi_workspace_enabled: bool, event_types: tuple[str, ...],
) -> EventDelivery | None:
    """Lock/revalidate the sorted Ingestion purge/run/coverage union after earlier owner roots.

    Canonical preparation already holds all seven Ingestion row sets; Memory/recovery never
    mutates those roots. Owner hook locks existing union rows in one UUID order, returns only
    requested envelope/dispatched_at and acquires no earlier locks or commits. Recompare
    payload and operation under lock. Pending status is valid
    only for postcommit eviction/re-arm, never for an initial worker execution claim.
    """
    event = await ingestion.lock_source_purge_event_in_uow(
        session, event_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        event_types=event_types,
    )
    if event is None or _event_operation_id(event, scope, event_types) != operation_id:
        return None
    return event


def _source_fence(source: Source) -> SourceFence:
    """Detach the actual Source already locked by this owner; construction grants no authority."""
    return SourceFence(id=source.id, workspace_id=source.workspace_id, status=source.status,
                       generation=source.generation, local_only=source.local_only)


def _source_drafts(source: Source | None, operation_id: UUID, scope: InternalJobScope) -> list[ReplayDraft]:
    """Build only scoped invalidation from an admitted own Source, including unavailable cleanup."""
    return [make_source_change(source.id, source.generation, source.status,
                               operation_id=operation_id, scope=scope)] if source is not None else []


async def _arm_coverage_event(
    session: AsyncSession, operation_id: UUID, *, now: datetime, scope: InternalJobScope,
    multi_workspace_enabled: bool,
) -> bool:
    """Create/reopen full-identity coverage under held access/Source/operation locks.

    Pending/queued rows retain retry schedule and claim. Mismatching retained identity cannot
    be rewritten into a current epoch. Flush only; external dispatcher enqueues after durable
    identity commit. Missing older context is never emitted as an operation-only payload.
    """
    if await sources.read_source_purge_job_identity(
        session, operation_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    ) != scope:
        return False
    event_id = _coverage_event_id(operation_id)
    event = await ingestion.get_event_delivery(
        session, event_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if event is None:
        await ingestion.publish_event(session, DomainEvent(
            id=event_id, type="source.purge.coverage", version=1, occurred_at=now,
            producer="modules.sources", payload=_purge_payload(operation_id, scope),
        ), scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        return True
    event = await _lock_delivery(session, event_id, operation_id, scope=scope,
                                 multi_workspace_enabled=multi_workspace_enabled, event_types=_COVERAGE_EVENTS)
    if event is not None and event.status in {"delivered", "failed"}:
        return await ingestion.set_event_delivery(
            session, event_id, "pending", next_attempt_at=now,
            scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
    return False


async def _settle_operation(
    session: AsyncSession, operation: SourcePurgeOperation, *, scope: InternalJobScope,
    multi_workspace_enabled: bool,
) -> bool:
    """Recompute fresh scoped Documents+Memory coverage; return active raw/Chat copy work.

    Caller holds exact retained operation. Documents aggregate includes same-workspace,
    same-Source historical receipts without earlier locks. Success needs canonical capture,
    every required retained stage, exhausted Memory sweep and completed cache eviction.
    Terminal unavailable coverage wins; no content/raw URI/child receipt is returned.
    """
    progress = await documents.source_cleanup_progress(
        session, operation.id, source_id=operation.source_id,
        capture_recorded=operation.documents_status == "deleted",
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    operation.pending_child_count = progress.pending_count
    operation.failed_child_count = progress.failed_count
    owners = list(progress.pending_owner_codes)
    if not _memory_complete(operation) and "memory" not in owners:
        owners.append("memory")
    operation.pending_owner_codes = owners
    documents_failed = progress.failed_count + progress.historical_failed_count
    if _memory_unavailable(operation):
        operation.status = "failed"
        operation.error_code = operation.memory_error_code
    elif documents_failed:
        operation.status = "failed"
        operation.error_code = "document_cleanup_failed"
    elif operation.memory_status == "failed":
        operation.status = "failed"
        operation.error_code = operation.memory_error_code or "memory_cleanup_failed"
    elif not progress.all_required_complete or not _memory_complete(operation):
        operation.status = "running"
        operation.error_code = None
    else:
        operation.status = "succeeded"
        operation.error_code = None
    return progress.active_copy_work


async def process_source_purge(ctx: dict[str, object], event_id: str) -> None:
    """Delete canonical Source content once under exact durable scope/generation/claim.

    Identity/admission precedes Source->Documents/URI->Ingestion roots/children->operation->
    sorted event union and original queued dispatch CAS. Documents captures bounded child
    receipts and deletes before Ingestion applies cancellation to prepared rows. No Memory privacy, raw
    unlink, Redis enqueue or external I/O. Copy continuation is five seconds; replay uses
    the early held AccessFence. Canonical capture and coverage arming commit atomically.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    flag = _configured_flag(ctx)
    identifier = UUID(event_id)
    async with factory() as session:
        admitted = await _admit_event(session, identifier, multi_workspace_enabled=flag, event_types=_PURGE_EVENTS)
        if admitted is None:
            return
        operation_id, scope, access_fence, captured_delivery = admitted
        if captured_delivery.status != "queued" or not isinstance(captured_delivery.dispatched_at, datetime):
            return
        source, operation = await _lock_operation(session, operation_id, scope=scope,
            multi_workspace_enabled=flag, access_fence=access_fence, memory_phase=False, prepare_documents=True)
        if operation is None:
            return
        event = await _lock_delivery(session, identifier, operation_id, scope=scope,
            multi_workspace_enabled=flag, event_types=_PURGE_EVENTS)
        if event is None or event.status != "queued" or event.dispatched_at != captured_delivery.dispatched_at:
            return
        if source is None or source.generation != operation.generation:
            operation.status = "failed"
            if operation.documents_status != "unavailable":
                operation.documents_status = "failed"
                operation.error_code = "source_generation_changed"
                operation.pending_owner_codes = ["documents"]
            await ingestion.set_event_delivery(session, identifier, "delivered", scope=scope, multi_workspace_enabled=flag)
            await commit_with_replay(session, _source_drafts(source, operation_id, scope), scope=scope,
                                     multi_workspace_enabled=flag, access_fence=access_fence)
            return
        if operation.documents_status in {"failed", "unavailable"}:
            operation.status = "failed"
            operation.error_code = operation.error_code or "evidence_identity_unavailable"
            await ingestion.set_event_delivery(session, identifier, "delivered", scope=scope, multi_workspace_enabled=flag)
            await commit_with_replay(session, _source_drafts(source, operation_id, scope), scope=scope,
                                     multi_workspace_enabled=flag, access_fence=access_fence)
            return
        drafts = _source_drafts(source, operation_id, scope)
        source_fence = _source_fence(source)
        if operation.documents_status == "queued":
            try:
                timeline_drafts = await documents.delete_source_documents_in_uow(
                    session, source.id, source_purge_operation_id=operation.id,
                    scope=scope, multi_workspace_enabled=flag, access_fence=access_fence,
                    source_fence=source_fence,
                )
            except documents.DocumentCleanupPreparationLimitError:
                operation.documents_status = "failed"
                operation.status = "failed"
                operation.error_code = "source_cleanup_dependency_limit_exceeded"
                operation.pending_owner_codes = ["documents"]
                await ingestion.set_event_delivery(session, identifier, "delivered", scope=scope, multi_workspace_enabled=flag)
                await commit_with_replay(session, drafts, scope=scope, multi_workspace_enabled=flag, access_fence=access_fence)
                return
            except ValueError as exc:
                if str(exc) != "Source graph cleanup exceeds its atomic document limit":
                    raise
                operation.documents_status = "failed"
                operation.status = "failed"
                operation.error_code = "source_document_limit_exceeded"
                operation.pending_owner_codes = ["documents"]
                await ingestion.set_event_delivery(session, identifier, "delivered", scope=scope, multi_workspace_enabled=flag)
                await commit_with_replay(session, drafts, scope=scope, multi_workspace_enabled=flag, access_fence=access_fence)
                return
            operation.documents_status = "deleted"
            await ingestion.cancel_and_purge_source_ingestion(
                session, source.id, scope=scope, multi_workspace_enabled=flag,
                access_fence=access_fence, source_fence=source_fence,
            )
            drafts.extend([make_knowledge_change(source.id, deleted=True, scope=scope), *timeline_drafts])
        active_copy_work = await _settle_operation(session, operation, scope=scope, multi_workspace_enabled=flag)
        if operation.status == "running" and active_copy_work:
            await ingestion.set_event_delivery(session, identifier, "pending",
                next_attempt_at=datetime.now(UTC) + _CONTINUATION_DELAY, scope=scope, multi_workspace_enabled=flag)
        else:
            await ingestion.set_event_delivery(session, identifier, "delivered", scope=scope, multi_workspace_enabled=flag)
        if not _memory_complete(operation) and not _memory_unavailable(operation):
            await _arm_coverage_event(session, operation.id, now=datetime.now(UTC), scope=scope, multi_workspace_enabled=flag)
        await commit_with_replay(session, drafts, scope=scope, multi_workspace_enabled=flag, access_fence=access_fence)


async def _evict_memory_cache_after_commit(
    factory: async_sessionmaker[AsyncSession], redis: Redis, operation_id: UUID, event_id: UUID, *,
    scope: InternalJobScope, multi_workspace_enabled: bool, dispatched_at: datetime,
    attempt: tuple[object, ...], access_fence: AccessFence,
) -> None:
    """Evict the admitted actor cache outside SQL locks, then CAS the exact pending stage.

    Preparation/publication resolve original event/operation identity; a newer dispatch,
    snapshot or generation cannot lose its pending marker. End SQL admission/privacy/Source/
    operation/delivery transactions before Redis. Failure leaves durable marker for retry.
    """
    async with factory() as session:
        admitted = await _admit_event(session, event_id, multi_workspace_enabled=multi_workspace_enabled,
                                      event_types=_COVERAGE_EVENTS, expected_scope=scope,
                                      expected_fence=access_fence)
        if admitted is None or admitted[0] != operation_id:
            return
        _, _, access_fence, captured_delivery = admitted
        if captured_delivery.status != "pending" or captured_delivery.dispatched_at != dispatched_at:
            return
        source, operation = await _lock_operation(session, operation_id, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, memory_phase=True)
        event = await _lock_delivery(session, event_id, operation_id, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, event_types=_COVERAGE_EVENTS)
        if (operation is None or source is None or source.generation != scope.source_generation
                or event is None or event.status != "pending" or event.dispatched_at != dispatched_at
                or _memory_snapshot(operation) != attempt or not operation.memory_cache_pending):
            return
        await session.rollback()
    try:
        await memory.invalidate_memory_cache(redis, scope=scope)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Source Memory cache eviction deferred (%s)", type(exc).__name__)
        return
    async with factory() as session:
        admitted = await _admit_event(session, event_id, multi_workspace_enabled=multi_workspace_enabled,
                                      event_types=_COVERAGE_EVENTS, expected_scope=scope,
                                      expected_fence=access_fence)
        if admitted is None or admitted[0] != operation_id:
            return
        _, _, access_fence, captured_delivery = admitted
        if captured_delivery.status != "pending" or captured_delivery.dispatched_at != dispatched_at:
            return
        source, operation = await _lock_operation(session, operation_id, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, memory_phase=True)
        event = await _lock_delivery(session, event_id, operation_id, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, event_types=_COVERAGE_EVENTS)
        if (operation is None or source is None or source.generation != scope.source_generation
                or event is None or event.status != "pending" or event.dispatched_at != dispatched_at
                or _memory_snapshot(operation) != attempt or not operation.memory_cache_pending):
            return
        operation.memory_cache_pending = False
        await _settle_operation(session, operation, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        delay = None
        if operation.memory_status == "failed" and not _memory_unavailable(operation):
            delay = _RETRY_DELAY
        elif operation.memory_status in {"queued", "running"}:
            delay = _MEMORY_PAGE_DELAY
        await ingestion.set_event_delivery(session, event_id, "pending" if delay is not None else "delivered",
            next_attempt_at=datetime.now(UTC) + delay if delay is not None else None,
            scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        await commit_with_replay(session, _source_drafts(source, operation_id, scope), scope=scope,
                                 multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)


async def process_source_memory_coverage(ctx: dict[str, object], event_id: str) -> None:
    """Sweep <=100 Source-local Memory copies per exact admitted delivery.

    Canonical capture must be deleted. Lock auth/workspace, actor privacy, Source, retained
    operation and outbox before Memory rows; hooks cannot reenter early locks. Scrub/cursor/
    count/continuation commit atomically. Cache eviction follows with original epoch,
    generation, dispatch and stage CAS; success additionally waits all retained receipts.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    redis = cast(Redis, ctx["redis"])
    flag = _configured_flag(ctx)
    identifier = UUID(event_id)
    scope: InternalJobScope | None = None
    snapshot_fence: AccessFence | None = None
    dispatched_at: datetime | None = None
    attempt: tuple[object, ...] | None = None
    eviction: tuple[UUID, tuple[object, ...]] | None = None
    reset_cursor = False
    try:
        async with factory() as session:
            admitted = await _admit_event(session, identifier, multi_workspace_enabled=flag, event_types=_COVERAGE_EVENTS)
            if admitted is None:
                return
            operation_id, scope, access_fence, captured_delivery = admitted
            snapshot_fence = access_fence
            if captured_delivery.status != "queued" or not isinstance(captured_delivery.dispatched_at, datetime):
                return
            source, operation = await _lock_operation(session, operation_id, scope=scope,
                multi_workspace_enabled=flag, access_fence=access_fence, memory_phase=True)
            if operation is None:
                return
            event = await _lock_delivery(session, identifier, operation_id, scope=scope,
                                         multi_workspace_enabled=flag, event_types=_COVERAGE_EVENTS)
            if event is None or event.status != "queued" or event.dispatched_at != captured_delivery.dispatched_at:
                return
            dispatched_at = event.dispatched_at
            if operation.documents_status != "deleted":
                await ingestion.set_event_delivery(session, identifier, "delivered", scope=scope, multi_workspace_enabled=flag)
                await session.commit()
                return
            attempt = _memory_snapshot(operation)
            before = (operation.status, operation.error_code, operation.pending_child_count,
                      operation.failed_child_count, list(operation.pending_owner_codes or []), attempt)
            next_attempt_at: datetime | None = None
            if source is None or source.generation != operation.generation:
                operation.memory_status = "failed"
                operation.memory_error_code = "evidence_identity_unavailable"
                operation.memory_cursor = None
            elif operation.memory_cache_pending:
                next_attempt_at = datetime.now(UTC) + _RETRY_DELAY
            elif operation.memory_status != "succeeded" and not _memory_unavailable(operation):
                cursor_state = operation.memory_cursor if operation.memory_cursor is not None else {}
                if not isinstance(cursor_state, dict) or set(cursor_state) - {"owner_cursor"}:
                    reset_cursor = True
                    raise ValueError("Stored Source Memory cursor is malformed")
                owner_cursor = cursor_state.get("owner_cursor")
                if owner_cursor is not None and not isinstance(owner_cursor, str):
                    reset_cursor = True
                    raise ValueError("Stored Source Memory cursor is malformed")
                operation.memory_status = "running"
                operation.memory_error_code = None
                progress = await memory.purge_source_copied_evidence_page_in_uow(
                    session, SourceCopiedEvidenceScope(operation_id=operation.id,
                        source_id=source.id, generation=operation.generation),
                    scope=scope, multi_workspace_enabled=flag, access_fence=access_fence,
                    source_fence=_source_fence(source), cursor=owner_cursor, limit=100,
                )
                operation.memory_unresolved_count += progress.unresolved_count
                if progress.changed:
                    operation.memory_cache_pending = True
                if not progress.complete:
                    if progress.next_cursor is None:
                        raise ValueError("Source Memory page is incomplete without a continuation cursor")
                    operation.memory_cursor = {"owner_cursor": progress.next_cursor}
                    next_attempt_at = datetime.now(UTC) + _MEMORY_PAGE_DELAY
                else:
                    operation.memory_cursor = None
                    if operation.memory_unresolved_count:
                        operation.memory_status = "failed"
                        operation.memory_error_code = "legacy_provenance_unresolved"
                    else:
                        operation.memory_status = "succeeded"
                        operation.memory_error_code = None
                if operation.memory_cache_pending:
                    next_attempt_at = next_attempt_at or datetime.now(UTC) + _RETRY_DELAY
            await _settle_operation(session, operation, scope=scope, multi_workspace_enabled=flag)
            await ingestion.set_event_delivery(session, identifier, "pending" if next_attempt_at is not None else "delivered",
                next_attempt_at=next_attempt_at, scope=scope, multi_workspace_enabled=flag)
            if operation.memory_cache_pending and next_attempt_at is not None:
                eviction = operation.id, _memory_snapshot(operation)
            after = (operation.status, operation.error_code, operation.pending_child_count,
                     operation.failed_child_count, list(operation.pending_owner_codes or []), _memory_snapshot(operation))
            if next_attempt_at is None and before == after:
                await session.commit()
                return
            await commit_with_replay(session, _source_drafts(source, operation_id, scope), scope=scope,
                                     multi_workspace_enabled=flag, access_fence=access_fence)
        if eviction is not None and scope is not None and dispatched_at is not None and snapshot_fence is not None:
            await _evict_memory_cache_after_commit(factory, redis, eviction[0], identifier, scope=scope,
                multi_workspace_enabled=flag, dispatched_at=dispatched_at, attempt=eviction[1],
                access_fence=snapshot_fence)
    except HTTPException:
        # Permission loss is never rewritten as cursor failure or an upgraded job epoch.
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("Source Memory coverage deferred (%s)", type(exc).__name__)
        reset_cursor = reset_cursor or isinstance(exc, ValueError) and str(exc) == "Source Memory cleanup cursor is invalid"
        await _recover_memory_coverage(factory, identifier, attempt, reset=reset_cursor, scope=scope,
                                       multi_workspace_enabled=flag, dispatched_at=dispatched_at,
                                       access_fence=snapshot_fence)


async def reconcile_source_coverage(ctx: dict[str, object]) -> int:
    """Page global IDs fairly, then admit/arm each exact workspace independently.

    Global SQL returns IDs only, bounded100 and ordered by stable UUID metadata. Context
    cursor is a scheduling hint, never authority; wrap on exhaustion and advance past denied
    IDs. Release each workspace transaction before the next. Only admitted scoped pending
    IDs/exact retained operations reach content/outbox locks. No Redis/network enqueue.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    flag = _configured_flag(ctx)
    cursor = ctx.get("source_coverage_metadata_cursor")
    after = cursor if isinstance(cursor, UUID) else None
    async with factory() as session:
        statement = select(SourcePurgeOperation.id).where(
            SourcePurgeOperation.documents_status == "deleted", SourcePurgeOperation.status != "succeeded",
            ~and_(SourcePurgeOperation.memory_status == "failed",
                  SourcePurgeOperation.memory_error_code.in_(sources.SOURCE_MEMORY_TERMINAL_CODES)),
            sources.coverage_settled_exclusion(),
        ).order_by(SourcePurgeOperation.id).limit(100)
        operation_ids = tuple((await session.scalars(statement.where(SourcePurgeOperation.id > after)
                                                     if after is not None else statement)).all())
        if not operation_ids and after is not None:
            operation_ids = tuple((await session.scalars(statement)).all())
        await session.rollback()
    ctx["source_coverage_metadata_cursor"] = operation_ids[-1] if operation_ids else None
    enqueued = 0
    for operation_id in operation_ids:
        try:
            async with factory() as session:
                scope = await sources.resolve_source_purge_job_scope(session, operation_id, multi_workspace_enabled=flag)
                if scope is None:
                    continue
                # Intersect exact original subject; a scoped page cannot strand a later global ID.
                predecessor = UUID(int=operation_id.int - 1) if operation_id.int else None
                eligible = await sources.pending_source_coverage_ids(session, scope=scope,
                    multi_workspace_enabled=flag, after=predecessor, limit=1)
                if operation_id not in eligible:
                    continue
                access_fence = await read_access_fence(session, scope=scope, multi_workspace_enabled=flag)
                _, operation = await _lock_operation(session, operation_id, scope=scope,
                    multi_workspace_enabled=flag, access_fence=access_fence, memory_phase=False)
                if operation is None or operation.documents_status != "deleted" or operation.status == "succeeded" or _memory_unavailable(operation):
                    continue
                changed = await _arm_coverage_event(session, operation_id, now=datetime.now(UTC),
                                                   scope=scope, multi_workspace_enabled=flag)
                if changed:
                    await session.commit()
                    enqueued += 1
                else:
                    await session.rollback()
        except HTTPException as exc:
            if exc.status_code not in {401, 403, 404, 409}:
                raise
            # Denied old actor/epoch consumes a slot but cannot starve later operation IDs.
            continue
    return enqueued


async def _recover_memory_coverage(
    factory: async_sessionmaker[AsyncSession], identifier: UUID, attempt: tuple[object, ...] | None, *,
    reset: bool, scope: InternalJobScope | None, multi_workspace_enabled: bool, dispatched_at: datetime | None,
    access_fence: AccessFence | None = None,
) -> None:
    """Persist retry failure only for original admitted epoch, dispatch and exact stage snapshot.

    Missing attempt/claim does not mutate. Ordered admission->privacy->Source->operation
    precedes outbox. New dispatch/success/cursor wins. Reset counts only for malformed stored
    cursor; permission, payload or downstream errors never silently become a cursor reset.
    """
    if scope is None or attempt is None or dispatched_at is None or access_fence is None:
        return
    async with factory() as session:
        admitted = await _admit_event(session, identifier, multi_workspace_enabled=multi_workspace_enabled,
                                      event_types=_COVERAGE_EVENTS, expected_scope=scope,
                                      expected_fence=access_fence)
        if admitted is None:
            return
        operation_id, _, access_fence, captured_delivery = admitted
        if captured_delivery.status != "queued" or captured_delivery.dispatched_at != dispatched_at:
            return
        source, operation = await _lock_operation(session, operation_id, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, memory_phase=True)
        event = await _lock_delivery(session, identifier, operation_id, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, event_types=_COVERAGE_EVENTS)
        if (operation is None or source is None or source.generation != scope.source_generation
                or event is None or event.status != "queued" or event.dispatched_at != dispatched_at
                or _memory_snapshot(operation) != attempt or operation.memory_status == "succeeded"
                or _memory_unavailable(operation)):
            return
        code = "memory_cursor_reset" if reset else "memory_cleanup_failed"
        operation.memory_status = "failed"
        operation.memory_error_code = code
        if reset:
            operation.memory_cursor = None
            operation.memory_unresolved_count = 0
        operation.status = "failed"
        operation.error_code = code
        await ingestion.set_event_delivery(session, identifier, "pending", next_attempt_at=datetime.now(UTC) + _RETRY_DELAY,
                                           scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        await commit_with_replay(session, _source_drafts(source, operation_id, scope), scope=scope,
                                 multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
