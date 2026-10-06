"""Source-owned orchestration for generation-fenced source purge receipts and Source Memory coverage."""

from datetime import UTC, datetime, timedelta
import logging
from typing import cast
from uuid import UUID, uuid5

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.events import DomainEvent
from core.realtime import commit_with_replay, make_knowledge_change, make_source_change
from modules.ingestion import public as ingestion
from modules.knowledge.documents import public as documents
from modules.memory.public import (
    SourceCopiedEvidenceScope,
    invalidate_memory_cache,
    lock_export_privacy,
    purge_source_copied_evidence_page,
)
from modules.sources.models import Source, SourcePurgeOperation
from modules.sources.public import SOURCE_MEMORY_TERMINAL_CODES, pending_source_coverage_ids

logger = logging.getLogger(__name__)
_CONTINUATION_DELAY = timedelta(seconds=5)
_MEMORY_PAGE_DELAY = timedelta(seconds=1)
_RETRY_DELAY = timedelta(seconds=30)
_coverage_reconcile_cursor: UUID | None = None


def _memory_complete(operation: SourcePurgeOperation) -> bool:
    """Source Memory coverage is complete only after a full exhausted sweep and cache eviction."""
    return operation.memory_status == "succeeded" and not operation.memory_cache_pending


def _memory_unavailable(operation: SourcePurgeOperation) -> bool:
    """Durable unavailable coverage: a failed stage whose code never improves without new evidence."""
    return operation.memory_status == "failed" and operation.memory_error_code in SOURCE_MEMORY_TERMINAL_CODES


def _memory_snapshot(operation: SourcePurgeOperation) -> tuple[object, ...]:
    """Capture the Memory-stage fields recovery compares under lock before writing an error."""
    cursor = operation.memory_cursor
    return (
        operation.memory_status, operation.memory_error_code,
        dict(cursor) if isinstance(cursor, dict) else cursor,
        operation.memory_unresolved_count, operation.memory_cache_pending,
    )


def _coverage_event_id(operation_id: UUID) -> UUID:
    """Derive the stable per-operation Source Memory coverage outbox identity."""
    return uuid5(operation_id, "source-memory-coverage")


async def _arm_coverage_event(session: AsyncSession, operation_id: UUID, *, now: datetime) -> bool:
    """Publish or reopen the operation's coverage event in the caller's transaction.

    A pending or queued event keeps its schedule; only a delivered/failed one is reopened, so a
    reconciler pass cannot shorten a retry delay. Returns whether the outbox changed.
    """
    event_id = _coverage_event_id(operation_id)
    event = await ingestion.get_event_delivery(session, event_id)
    if event is None:
        await ingestion.publish_event(session, DomainEvent(
            id=event_id, type="source.purge.coverage", version=1, occurred_at=now,
            producer="modules.sources", payload={"operation_id": str(operation_id)},
        ))
        return True
    if event.status in {"delivered", "failed"}:
        await ingestion.set_event_delivery(session, event_id, "pending", next_attempt_at=now)
        return True
    return False


async def _settle_operation(session: AsyncSession, operation: SourcePurgeOperation) -> bool:
    """Recompute the aggregate status from fresh owner reads and return active raw/Chat work.

    Full-copy success needs all of: Documents capture recorded for this operation; every retained
    same-source receipt (linked or historical) with all required stages complete; Source Memory
    coverage exhausted with zero unresolved and its cache eviction done. The Documents aggregate
    is a read-only detached projection; nothing is cached as complete between calls. Terminal
    unavailable Memory coverage wins the error code because no retry can change it.
    """
    progress = await documents.source_cleanup_progress(
        session,
        operation.id,
        source_id=operation.source_id,
        capture_recorded=operation.documents_status == "deleted",
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
        # Empty source is complete only after this transaction recorded Documents deletion.
        operation.status = "succeeded"
        operation.error_code = None
    return progress.active_copy_work


async def process_source_purge(ctx: dict[str, object], event_id: str) -> None:
    """Capture and delete a Source once, then publish Documents-owned aggregate progress.

    Source is locked before its purge receipt whenever both are needed. The Documents call
    captures bounded per-document receipts and exact immutable identities in this transaction;
    its raw and Chat consumers do filesystem work under URI-before-receipt locks and wake this
    worker through operation-only outbox events. Source never snapshots or unlinks raw files.
    Active raw/Chat work receives a five-second continuation; Documents retries ordinary stage
    failures after thirty seconds and emits a fresh wakeup (also for historical NULL-linked
    receipts of the same Source) when that stage changes. This canonical phase never touches Memory
    rows or the Memory privacy lock: once Documents capture is recorded it only arms the separate
    Source Memory coverage event, in the same transaction as the status update.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    identifier = UUID(event_id)
    async with factory() as session:
        event = await ingestion.get_event_delivery(session, identifier)
        if event is None or event.status == "delivered":
            return
        try:
            operation_id = UUID(str(event.payload["operation_id"]))
        except (KeyError, TypeError, ValueError):
            await ingestion.set_event_delivery(session, identifier, "failed")
            await session.commit()
            return

        hint = await session.get(SourcePurgeOperation, operation_id)
        if hint is None:
            await ingestion.set_event_delivery(session, identifier, "failed")
            await session.commit()
            return
        # Source-before-operation is the common lifecycle lock order for mutation and replay.
        source = await session.scalar(
            select(Source).where(Source.id == hint.source_id).with_for_update()
        )
        operation = await session.scalar(
            select(SourcePurgeOperation).where(SourcePurgeOperation.id == operation_id)
            .with_for_update().execution_options(populate_existing=True)
        )
        if operation is None:
            await ingestion.set_event_delivery(session, identifier, "failed")
            await session.commit()
            return
        if operation.status == "succeeded" and _memory_complete(operation):
            await ingestion.set_event_delivery(session, identifier, "delivered")
            await session.commit()
            return
        if source is None or source.generation != operation.generation:
            operation.status = "failed"
            if operation.documents_status != "unavailable":
                operation.documents_status = "failed"
                operation.error_code = "source_generation_changed"
                operation.pending_owner_codes = ["documents"]
            await ingestion.set_event_delivery(session, identifier, "delivered")
            drafts = [make_source_change(
                source.id, source.generation, source.status, operation_id=operation_id,
            )] if source is not None else []
            await commit_with_replay(session, drafts)
            return

        if operation.documents_status in {"failed", "unavailable"}:
            # Migration marked already-cascaded legacy scopes truthfully unavailable.
            operation.status = "failed"
            operation.error_code = operation.error_code or "evidence_identity_unavailable"
            await ingestion.set_event_delivery(session, identifier, "delivered")
            await commit_with_replay(session, [make_source_change(
                source.id, source.generation, source.status, operation_id=operation_id,
            )])
            return

        drafts = []
        if operation.documents_status == "queued":
            try:
                timeline_drafts = await documents.delete_source_documents(
                    session,
                    source.id,
                    source_purge_operation_id=operation.id,
                )
            except ValueError as exc:
                if str(exc) != "Source graph cleanup exceeds its atomic document limit":
                    raise
                operation.documents_status = "failed"
                operation.status = "failed"
                operation.error_code = "source_document_limit_exceeded"
                operation.pending_owner_codes = ["documents"]
                await ingestion.set_event_delivery(session, identifier, "delivered")
                await commit_with_replay(session, [make_source_change(
                    source.id, source.generation, source.status, operation_id=operation_id,
                )])
                return
            operation.documents_status = "deleted"
            await ingestion.cancel_and_purge_source_ingestion(session, source.id)
            drafts = [
                make_source_change(source.id, source.generation, source.status, operation_id=operation_id),
                make_knowledge_change(source.id, deleted=True),
                *timeline_drafts,
            ]

        active_copy_work = await _settle_operation(session, operation)
        if operation.status == "running" and active_copy_work:
            await ingestion.set_event_delivery(
                session, identifier, "pending",
                next_attempt_at=datetime.now(UTC) + _CONTINUATION_DELAY,
            )
        else:
            # Failed child stages retry on their own schedule and other owner stages requeue
            # progress events when they finish; neither needs a Source polling loop.
            await ingestion.set_event_delivery(session, identifier, "delivered")
        if not _memory_complete(operation) and not _memory_unavailable(operation):
            # Same transaction as the status write: the outbox row and the stage state cannot diverge.
            await _arm_coverage_event(session, operation.id, now=datetime.now(UTC))

        if not drafts:
            drafts = [make_source_change(
                source.id, source.generation, source.status, operation_id=operation_id,
            )]
        await commit_with_replay(session, drafts)


async def _evict_memory_cache_after_commit(
    factory: async_sessionmaker[AsyncSession], redis: Redis, operation_id: UUID, event_id: UUID,
) -> None:
    """Evict committed Memory state, clear the durable marker, then freshly re-settle the aggregate.

    Runs after the page transaction committed. Eviction failure leaves ``memory_cache_pending``
    set, which keeps the operation incomplete; the coverage event retries it. Lock order here is
    privacy -> operation only.
    """
    try:
        await invalidate_memory_cache(redis)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Source Memory cache eviction deferred (%s)", type(exc).__name__)
        return
    async with factory() as session:
        await lock_export_privacy(session)
        operation = await session.scalar(select(SourcePurgeOperation).where(
            SourcePurgeOperation.id == operation_id,
        ).with_for_update().execution_options(populate_existing=True))
        if operation is None or not operation.memory_cache_pending:
            await session.rollback()
            return
        operation.memory_cache_pending = False
        await _settle_operation(session, operation)
        if operation.memory_status == "failed" and not _memory_unavailable(operation):
            await ingestion.set_event_delivery(
                session, event_id, "pending", next_attempt_at=datetime.now(UTC) + _RETRY_DELAY,
            )
        elif operation.memory_status in {"queued", "running"}:
            await ingestion.set_event_delivery(
                session, event_id, "pending", next_attempt_at=datetime.now(UTC) + _MEMORY_PAGE_DELAY,
            )
        else:
            await ingestion.set_event_delivery(session, event_id, "delivered")
        source = await session.get(Source, operation.source_id)
        await commit_with_replay(session, [make_source_change(
            source.id, source.generation, source.status, operation_id=operation_id,
        )] if source is not None else [])


async def _recover_memory_coverage(
    factory: async_sessionmaker[AsyncSession], identifier: UUID,
    attempt: tuple[object, ...] | None, *, reset: bool,
) -> None:
    """Record a retryable Memory-stage failure only if the stage is unchanged since this attempt.

    The comparison happens under privacy -> operation locks, so a newer success, cursor or status
    is never overwritten. A malformed cursor (``reset``) restarts the sweep and recounts from zero.
    Without an attempt snapshot nothing is written; the dispatcher reclaims stale queued work.
    """
    async with factory() as session:
        event = await ingestion.get_event_delivery(session, identifier)
        if event is None or event.status == "delivered":
            return
        try:
            operation_id = UUID(str(event.payload["operation_id"]))
        except (KeyError, TypeError, ValueError):
            await ingestion.set_event_delivery(session, identifier, "failed")
            await session.commit()
            return
        if await session.get(SourcePurgeOperation, operation_id) is None:
            await ingestion.set_event_delivery(session, identifier, "failed")
            await session.commit()
            return
        await lock_export_privacy(session)
        operation = await session.scalar(select(SourcePurgeOperation).where(
            SourcePurgeOperation.id == operation_id,
        ).with_for_update().execution_options(populate_existing=True))
        drafts = []
        if (operation is not None and attempt is not None
                and _memory_snapshot(operation) == attempt
                and operation.memory_status != "succeeded" and not _memory_unavailable(operation)):
            code = "memory_cursor_reset" if reset else "memory_cleanup_failed"
            operation.memory_status = "failed"
            operation.memory_error_code = code
            if reset:
                operation.memory_cursor = None
                operation.memory_unresolved_count = 0
            operation.status = "failed"
            operation.error_code = code
            await ingestion.set_event_delivery(
                session, identifier, "pending", next_attempt_at=datetime.now(UTC) + _RETRY_DELAY,
            )
            source = await session.get(Source, operation.source_id)
            if source is not None:
                drafts = [make_source_change(
                    source.id, source.generation, source.status, operation_id=operation.id,
                )]
        await commit_with_replay(session, drafts)


async def process_source_memory_coverage(ctx: dict[str, object], event_id: str) -> None:
    """Sweep Source-local Memory copied evidence, then re-settle the whole-Source aggregate.

    Runs only after the canonical phase recorded Documents deletion. Lock order is the Memory
    privacy lock, then the Source fence, then the purge operation, then Memory rows (taken inside
    the owner hook); no Documents raw or receipt row is locked and this phase never runs while the
    canonical phase holds Source. Each delivery handles one <=100-row page and commits the scrub,
    cursor/status and outbox continuation atomically. Cache eviction follows the commit under a
    persisted ``memory_cache_pending`` marker; full-copy success additionally waits for that
    eviction and a fresh aggregate recheck. A Source that no longer matches the recorded purge
    generation is terminally unavailable; retryable failures retry after thirty seconds and
    permanently unavailable coverage is never retried automatically.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    redis = cast(Redis, ctx["redis"])
    identifier = UUID(event_id)
    attempt: tuple[object, ...] | None = None
    evict_after_commit: UUID | None = None
    try:
        async with factory() as session:
            event = await ingestion.get_event_delivery(session, identifier)
            if event is None or event.status == "delivered":
                return
            try:
                operation_id = UUID(str(event.payload["operation_id"]))
            except (KeyError, TypeError, ValueError):
                await ingestion.set_event_delivery(session, identifier, "failed")
                await session.commit()
                return
            hint = await session.get(SourcePurgeOperation, operation_id)
            if hint is None:
                await ingestion.set_event_delivery(session, identifier, "failed")
                await session.commit()
                return
            if hint.documents_status != "deleted" or (hint.status == "succeeded" and _memory_complete(hint)):
                # Canonical capture not recorded yet (the canonical phase re-arms us) or nothing left.
                await ingestion.set_event_delivery(session, identifier, "delivered")
                await session.commit()
                return
            attempt = _memory_snapshot(hint)
            await lock_export_privacy(session)
            source = await session.scalar(
                select(Source).where(Source.id == hint.source_id).with_for_update()
                .execution_options(populate_existing=True)
            )
            operation = await session.scalar(
                select(SourcePurgeOperation).where(SourcePurgeOperation.id == operation_id)
                .with_for_update().execution_options(populate_existing=True)
            )
            if operation is None or source is None or operation.documents_status != "deleted":
                await ingestion.set_event_delivery(session, identifier, "delivered")
                await session.commit()
                return

            before = (
                operation.status, operation.error_code, operation.pending_child_count,
                operation.failed_child_count, list(operation.pending_owner_codes or []),
                _memory_snapshot(operation),
            )
            next_attempt_at: datetime | None = None
            if source.generation != operation.generation:
                # The recorded purge generation can no longer identify this Source's copies.
                operation.memory_status = "failed"
                operation.memory_error_code = "evidence_identity_unavailable"
                operation.memory_cursor = None
            elif operation.memory_cache_pending:
                # Never sweep further, or report success, while a prior page awaits eviction.
                evict_after_commit = operation.id
                next_attempt_at = datetime.now(UTC) + _RETRY_DELAY
            elif operation.memory_status != "succeeded" and not _memory_unavailable(operation):
                cursor_state = operation.memory_cursor or {}
                if not isinstance(cursor_state, dict) or set(cursor_state) - {"owner_cursor"}:
                    raise ValueError("Stored Source Memory cursor is malformed")
                owner_cursor = cursor_state.get("owner_cursor")
                if owner_cursor is not None and not isinstance(owner_cursor, str):
                    raise ValueError("Stored Source Memory cursor is malformed")
                operation.memory_status = "running"
                operation.memory_error_code = None
                progress = await purge_source_copied_evidence_page(
                    session,
                    SourceCopiedEvidenceScope(
                        operation_id=operation.id, source_id=source.id, generation=operation.generation,
                    ),
                    cursor=owner_cursor, limit=100,
                )
                # Count and cursor commit together, so a retry cannot double count a page.
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
                    evict_after_commit = operation.id
                    next_attempt_at = next_attempt_at or datetime.now(UTC) + _RETRY_DELAY

            await _settle_operation(session, operation)
            if next_attempt_at is not None:
                await ingestion.set_event_delivery(
                    session, identifier, "pending", next_attempt_at=next_attempt_at,
                )
            else:
                await ingestion.set_event_delivery(session, identifier, "delivered")
            if next_attempt_at is None and before == (
                operation.status, operation.error_code, operation.pending_child_count,
                operation.failed_child_count, list(operation.pending_owner_codes or []),
                _memory_snapshot(operation),
            ):
                # Nothing observable changed: skip the replay row and realtime push.
                await session.commit()
                return
            await commit_with_replay(session, [make_source_change(
                source.id, source.generation, source.status, operation_id=operation.id,
            )])
        if evict_after_commit is not None:
            await _evict_memory_cache_after_commit(factory, redis, evict_after_commit, identifier)
    except ValueError:
        await _recover_memory_coverage(factory, identifier, attempt, reset=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Source Memory coverage deferred (%s)", type(exc).__name__)
        await _recover_memory_coverage(factory, identifier, attempt, reset=False)


async def reconcile_source_coverage(ctx: dict[str, object]) -> int:
    """Fairly re-arm one bounded keyset page of unfinished Source coverage operations.

    Selection is the Sources public queue of canonical-complete operations whose full-copy status
    can still change, including historically succeeded operations migrated to the new stage and
    operations waiting on historical Documents receipts that never had a linked wakeup. Terminal
    unavailable coverage is excluded by that query, so it cannot starve queued work. Each pass
    reopens only delivered/failed events and preserves any pending retry schedule.
    """
    global _coverage_reconcile_cursor
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    now = datetime.now(UTC)
    async with factory() as session:
        operation_ids = await pending_source_coverage_ids(session, after=_coverage_reconcile_cursor, limit=100)
        if not operation_ids and _coverage_reconcile_cursor is not None:
            _coverage_reconcile_cursor = None
            operation_ids = await pending_source_coverage_ids(session, limit=100)
        if operation_ids:
            _coverage_reconcile_cursor = operation_ids[-1]
        enqueued = 0
        for operation_id in operation_ids:
            if await _arm_coverage_event(session, operation_id, now=now):
                enqueued += 1
        if enqueued:
            await session.commit()
        else:
            await session.rollback()
        return enqueued
