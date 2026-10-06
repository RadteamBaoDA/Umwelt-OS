"""Documents-owned bounded cleanup consumer for raw files and Chat evidence copies."""

from datetime import UTC, datetime, timedelta
import hashlib
import json
import logging
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from redis.asyncio import Redis

from core.config import Settings
from core.storage import storage_path
from modules.ingestion import public as ingestion
from modules.knowledge.documents import public as documents
from modules.knowledge.documents.models import DocumentCleanupOperation
from modules.memory.public import invalidate_memory_cache, lock_export_privacy, purge_document_copied_evidence_page
from core.events import DomainEvent
from uuid import uuid5

logger = logging.getLogger(__name__)
_RETRY_DELAY = timedelta(seconds=30)
_CONTINUATION_DELAY = timedelta(seconds=1)
_MEMORY_RECONCILE_LIMIT = 100
_agent_reconcile_cursor: UUID | None = None


def _source_cleanup_progress_key(operation: DocumentCleanupOperation, stage: str) -> str:
    """Build a bounded deterministic Source event key from non-sensitive child-stage state."""
    state = (
        operation.raw_status,
        operation.error_code,
        operation.evidence_scope_status,
        operation.copied_status,
        operation.copied_error_code,
        operation.chat_status,
        operation.chat_error_code,
        operation.memory_status,
        operation.memory_error_code,
        operation.memory_unresolved_count,
        operation.memory_cache_pending,
        operation.agent_status,
        operation.agent_error_code,
        operation.agent_unresolved_count,
        operation.agent_waiting_for_lease,
    )
    fingerprint = hashlib.sha256(
        json.dumps(state, separators=(",", ":"), ensure_ascii=True).encode(),
    ).hexdigest()[:32]
    return f"document-cleanup:{stage}:{fingerprint}"


def _attempt_progress_snapshot(operation: DocumentCleanupOperation) -> tuple[object, ...]:
    """Capture the receipt fields recovery compares before writing stage errors.

    Callers may pass an unlocked read (Agent preflight) or the locked receipt; recovery
    re-compares under the receipt lock so a changed receipt is never overwritten.
    """
    return (
        operation.raw_status,
        operation.evidence_scope_status,
        operation.chat_status,
        operation.copied_status,
        dict(operation.copied_cursor) if isinstance(operation.copied_cursor, dict) else operation.copied_cursor,
        operation.memory_status,
        operation.memory_error_code,
        dict(operation.memory_cursor) if isinstance(operation.memory_cursor, dict) else operation.memory_cursor,
        operation.memory_unresolved_count,
        operation.memory_cache_pending,
        operation.agent_status,
        operation.agent_error_code,
        dict(operation.agent_cursor) if isinstance(operation.agent_cursor, dict) else operation.agent_cursor,
        operation.agent_unresolved_count,
        operation.agent_waiting_for_lease,
    )


async def _advance_raw_document_cleanup(
    factory: async_sessionmaker[AsyncSession], settings: Settings, event_id: UUID,
) -> None:
    """Run raw URI cleanup in its own URI-before-receipt transaction, separate from copy owners."""
    async with factory() as session:
        event = await ingestion.get_event_delivery(session, event_id)
        if event is None or event.status == "delivered":
            return
        try:
            operation_id = UUID(str(event.payload["operation_id"]))
        except (KeyError, TypeError, ValueError):
            return
        hint = await session.scalar(select(DocumentCleanupOperation).where(
            DocumentCleanupOperation.id == operation_id,
        ))
        if hint is None or hint.raw_status in {"not_present", "retained_shared", "succeeded"}:
            await session.rollback()
            return
        await lock_export_privacy(session)
        if hint.raw_uri:
            await documents.lock_raw_uri_identity(session, hint.raw_uri)
        operation = await session.scalar(select(DocumentCleanupOperation).where(
            DocumentCleanupOperation.id == operation_id,
        ).with_for_update().execution_options(populate_existing=True))
        if operation is None or operation.raw_status in {"not_present", "retained_shared", "succeeded"}:
            await session.rollback()
            return
        try:
            if operation.raw_uri is None:
                operation.raw_status = "not_present"
            elif await documents.raw_uri_is_referenced(session, operation.raw_uri):
                operation.raw_status = "retained_shared"
            else:
                storage_path(settings.data_dir, operation.raw_uri).unlink(missing_ok=True)
                operation.raw_status = "succeeded"
            operation.error_code = None
        except (OSError, ValueError):
            operation.raw_status = "failed"
            operation.error_code = "file_cleanup_failed"
        await documents.publish_source_cleanup_wakeup(
            session, operation,
            progress_key=_source_cleanup_progress_key(operation, "raw"),
        )
        await session.commit()


async def _advance_memory_cleanup(
    session: AsyncSession, operation: DocumentCleanupOperation,
) -> tuple[bool, bool]:
    """Flush one Memory-owned identity page and update only its bounded receipt state.

    Returns whether payload rows changed and whether the stage reached a terminal result. The
    caller commits this cursor with the page mutations and performs cache eviction afterward.
    """
    if operation.evidence_scope_status != "captured":
        operation.memory_status = "failed"
        operation.memory_error_code = "evidence_identity_unavailable"
        operation.memory_cursor = None
        return False, True
    cursor_state = operation.memory_cursor or {}
    if not isinstance(cursor_state, dict) or set(cursor_state) - {"reference_after", "owner_cursor"}:
        raise ValueError("Stored Memory cleanup cursor is malformed")
    reference_value = cursor_state.get("reference_after")
    owner_cursor = cursor_state.get("owner_cursor")
    if reference_value is not None and not isinstance(reference_value, str):
        raise ValueError("Stored Memory reference cursor is malformed")
    if owner_cursor is not None and not isinstance(owner_cursor, str):
        raise ValueError("Stored Memory owner cursor is malformed")
    reference_after = UUID(reference_value) if reference_value else None
    scope = await documents.list_document_cleanup_evidence_scope(
        session, operation.id, after=reference_after, limit=100,
    )
    if scope is None:
        raise ValueError("Document cleanup evidence scope is unavailable")
    progress = await purge_document_copied_evidence_page(
        session, scope, cursor=owner_cursor, limit=100,
    )
    # A provenance reference outside this bounded page can still be an exact match in a later
    # captured reference page. Keep uncertainty provisional until the final reference page so
    # an early page cannot permanently overcount a record that a later page will scrub.
    if scope.next_cursor is None:
        operation.memory_unresolved_count += progress.unresolved_count
        operation.memory_error_code = progress.unresolved_reason or None
    if progress.changed:
        operation.memory_cache_pending = True
    if not progress.complete:
        if progress.next_cursor is None:
            raise ValueError("Memory cleanup page is incomplete without a continuation cursor")
        operation.memory_status = "running"
        operation.memory_cursor = {
            "reference_after": str(reference_after) if reference_after else None,
            "owner_cursor": progress.next_cursor,
        }
        return progress.changed, False
    if scope.next_cursor is not None:
        operation.memory_status = "running"
        operation.memory_cursor = {
            "reference_after": str(scope.next_cursor),
            "owner_cursor": None,
        }
        return progress.changed, False
    operation.memory_cursor = None
    if operation.memory_unresolved_count:
        operation.memory_status = "failed"
        operation.memory_error_code = "legacy_provenance_unresolved"
    else:
        operation.memory_status = "succeeded"
        operation.memory_error_code = None
    return progress.changed, True


async def _evict_memory_cache_after_commit(
    factory: async_sessionmaker[AsyncSession], redis: Redis, operation_id: UUID, event_id: UUID,
) -> None:
    """Evict committed Memory state, clear its durable marker, and wake Source aggregation."""
    try:
        await invalidate_memory_cache(redis)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Memory cleanup cache eviction deferred (%s)", type(exc).__name__)
        return
    async with factory() as session:
        await lock_export_privacy(session)
        operation = await session.scalar(select(DocumentCleanupOperation).where(
            DocumentCleanupOperation.id == operation_id,
        ).with_for_update().execution_options(populate_existing=True))
        if operation is not None and operation.memory_cache_pending:
            operation.memory_cache_pending = False
            # Source's aggregate must observe the independent cache obligation clearing,
            # even when the Memory stage itself had already reached a terminal status.
            await documents.publish_source_cleanup_wakeup(
                session, operation,
                progress_key=_source_cleanup_progress_key(operation, "memory-cache"),
            )
            if (operation.raw_status in {"not_present", "retained_shared", "succeeded"}
                    and operation.chat_status == "succeeded"
                    and operation.memory_status in {"succeeded", "failed"}
                    and operation.agent_status in {"succeeded", "failed"}):
                await ingestion.set_event_delivery(session, event_id, "delivered")
            elif (operation.raw_status in {"not_present", "retained_shared", "succeeded"}
                    and operation.chat_status == "succeeded"
                    and (operation.memory_status == "running" or operation.agent_status == "running")):
                await ingestion.set_event_delivery(
                    session, event_id, "pending",
                    next_attempt_at=datetime.now(UTC) + _CONTINUATION_DELAY,
                )
            await session.commit()
        else:
            await session.rollback()


async def reconcile_document_memory_cleanup(ctx: dict[str, object]) -> int:
    """Requeue at most 100 captured historical receipts whose Chat stage already succeeded."""
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    now = datetime.now(UTC)
    async with factory() as session:
        operation_ids = await documents.pending_document_memory_cleanup_ids(
            session, limit=_MEMORY_RECONCILE_LIMIT,
        )
        enqueued = 0
        for operation_id in operation_ids:
            event_id = uuid5(operation_id, "document-cleanup-requested")
            event = await ingestion.get_event_delivery(session, event_id)
            if event is None:
                await ingestion.publish_event(session, DomainEvent(
                    id=event_id,
                    type="document.cleanup.requested",
                    version=1,
                    occurred_at=now,
                    producer="modules.knowledge.documents",
                    payload={"operation_id": str(operation_id)},
                ))
                enqueued += 1
            elif event.status in {"delivered", "failed"}:
                await ingestion.set_event_delivery(
                    session, event_id, "pending", next_attempt_at=now,
                )
                enqueued += 1
        if enqueued:
            await session.commit()
        else:
            await session.rollback()
        return enqueued


async def reconcile_document_agent_cleanup(ctx: dict[str, object]) -> int:
    """Fairly scan one bounded UUID page of receipts with a nonterminal Agent stage."""
    global _agent_reconcile_cursor
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    now = datetime.now(UTC)
    async with factory() as session:
        operation_ids = await documents.pending_document_agent_cleanup_ids(
            session, after=_agent_reconcile_cursor, limit=100,
        )
        if not operation_ids and _agent_reconcile_cursor is not None:
            _agent_reconcile_cursor = None
            operation_ids = await documents.pending_document_agent_cleanup_ids(session, limit=100)
        if operation_ids:
            _agent_reconcile_cursor = operation_ids[-1]
        enqueued = 0
        for operation_id in operation_ids:
            event_id = uuid5(operation_id, "document-cleanup-requested")
            event = await ingestion.get_event_delivery(session, event_id)
            if event is None:
                await ingestion.publish_event(session, DomainEvent(
                    id=event_id, type="document.cleanup.requested", version=1,
                    occurred_at=now, producer="modules.knowledge.documents",
                    payload={"operation_id": str(operation_id)},
                ))
                enqueued += 1
            elif event.status in {"delivered", "failed"}:
                await ingestion.set_event_delivery(session, event_id, "pending", next_attempt_at=now)
                enqueued += 1
        if enqueued:
            await session.commit()
        else:
            await session.rollback()
        return enqueued


async def process_document_cleanup(ctx: dict[str, object], event_id: str) -> None:
    """Advance raw and copied-evidence cleanup stages after canonical deletion commits.

    Raw URI removal and its receipt commit in a separate transaction before copied-owner work.
    For Agent cleanup, detached scope discovery and the exact nonblocking run lease are prepared
    before the shared privacy lock and receipt lock; the owner hook then follows its documented
    Agent row order in the caller's unit of work. Chat and Memory consume detached identities,
    and stage cursors plus Source wakeups commit with owner changes. Memory cache eviction follows
    commit under a durable retry marker. Incomplete pages reuse their deterministic child event.
    Error recovery compares durable stage state to this attempt's snapshot before writing.
    Recovery without that snapshot leaves event delivery untouched; the dispatcher reclaims stale
    queued work after its bounded interval instead of overwriting a newer delivery schedule.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    settings = cast(Settings, ctx["settings"])
    redis = cast(Redis, ctx["redis"])
    identifier = UUID(event_id)
    attempt_progress: tuple[object, ...] | None = None
    evict_after_commit: UUID | None = None
    attempt_stage = "unknown"

    try:
        await _advance_raw_document_cleanup(factory, settings, identifier)
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

            operation_hint = await session.scalar(select(DocumentCleanupOperation).where(
                DocumentCleanupOperation.id == operation_id,
            ))
            if operation_hint is None:
                await ingestion.set_event_delivery(session, identifier, "failed")
                await session.commit()
                return
            # Agent lease preparation must happen before the shared privacy or receipt lock.
            # The same session retains a successful transaction advisory lease through commit.
            agent_scope = None
            agent_preflight = None
            agent_reference_after = None
            operation_id_snapshot = operation_hint.id
            source_id_snapshot = operation_hint.source_id
            document_id_snapshot = operation_hint.document_id
            evidence_scope_snapshot = operation_hint.evidence_scope_status
            agent_status_snapshot = operation_hint.agent_status
            agent_cursor_snapshot = (
                dict(operation_hint.agent_cursor)
                if isinstance(operation_hint.agent_cursor, dict) else operation_hint.agent_cursor
            )
            if operation_hint.agent_status in {"queued", "running"} and operation_hint.evidence_scope_status == "captured":
                # Preflight failures (bad cursor, lease DB errors) need Agent-stage recovery. The
                # unlocked snapshot lets recovery compare under the receipt lock before writing.
                attempt_stage = "agent"
                attempt_progress = _attempt_progress_snapshot(operation_hint)
                cursor_state = operation_hint.agent_cursor or {
                    "v": 1, "reference_after": None, "candidate_after": None, "phase": "discover",
                }
                if not isinstance(cursor_state, dict) or set(cursor_state) != {
                    "v", "reference_after", "candidate_after", "phase",
                } or cursor_state.get("v") != 1 or cursor_state.get("phase") not in {"discover", "finalize"}:
                    raise ValueError("Stored Agent cleanup cursor is malformed")
                reference_value = cursor_state.get("reference_after")
                candidate_cursor = cursor_state.get("candidate_after")
                if reference_value is not None and not isinstance(reference_value, str):
                    raise ValueError("Stored Agent reference cursor is malformed")
                if candidate_cursor is not None and not isinstance(candidate_cursor, str):
                    raise ValueError("Stored Agent candidate cursor is malformed")
                reference_after = UUID(reference_value) if reference_value else None
                agent_reference_after = reference_after
                agent_scope = await documents.list_document_cleanup_evidence_scope(
                    session, operation_id, after=reference_after, limit=100,
                )
                if agent_scope is None:
                    raise ValueError("Document cleanup evidence scope is unavailable")
                from modules.agents import public as agents

                agent_preflight = await agents.preflight_document_copied_evidence_lease(
                    session, agent_scope, cursor=candidate_cursor, limit=100,
                )
                if agent_preflight.blocked:
                    # No privacy or receipt row was locked; schedule retry from the durable cursor.
                    await ingestion.set_event_delivery(
                        session, identifier, "pending", next_attempt_at=datetime.now(UTC) + _RETRY_DELAY,
                    )
                    await session.commit()
                    return

            # Shared Memory consent lock precedes URI, receipt, Chat and Memory owner locks.
            await lock_export_privacy(session)
            operation = await session.scalar(select(DocumentCleanupOperation).where(
                DocumentCleanupOperation.id == operation_id,
            ).with_for_update().execution_options(populate_existing=True))
            if operation is None:
                await ingestion.set_event_delivery(session, identifier, "failed")
                await session.commit()
                return

            attempt_progress = _attempt_progress_snapshot(operation)
            if attempt_stage == "agent":
                attempt_stage = "unknown"  # Preflight and lease checks passed; later stages set their own.

            scope_stale = (
                operation.id != operation_id_snapshot
                or operation.source_id != source_id_snapshot
                or operation.document_id != document_id_snapshot
                or operation.evidence_scope_status != evidence_scope_snapshot
                or operation.agent_status != agent_status_snapshot
                or operation.agent_cursor != agent_cursor_snapshot
            )
            if agent_scope is not None:
                locked_agent_scope = await documents.list_document_cleanup_evidence_scope(
                    session, operation.id, after=agent_reference_after, limit=100,
                )
                scope_stale = scope_stale or locked_agent_scope != agent_scope
            if scope_stale:
                # The preflight is detached, so discard its advisory lease and repeat from the
                # receipt's current cursor before touching owner rows.
                await session.rollback()
                async with factory() as retry_session:
                    retry_event = await ingestion.get_event_delivery(retry_session, identifier)
                    if retry_event is not None and retry_event.status != "delivered":
                        await ingestion.set_event_delivery(
                            retry_session, identifier, "pending",
                            next_attempt_at=datetime.now(UTC) + _CONTINUATION_DELAY,
                        )
                        await retry_session.commit()
                return

            memory_terminal = operation.memory_status == "succeeded" or (
                operation.memory_status == "failed" and operation.memory_error_code in {
                    "legacy_provenance_unresolved", "evidence_identity_unavailable",
                }
            )
            agent_terminal = operation.agent_status in {"succeeded", "failed"}
            if (operation.raw_status in {"not_present", "retained_shared", "succeeded"}
                    and operation.chat_status == "succeeded" and memory_terminal and agent_terminal
                    and not operation.memory_cache_pending):
                # Duplicate deliveries must not reopen a receipt whose required active stages finished.
                await ingestion.set_event_delivery(session, identifier, "delivered")
                await session.commit()
                return

            operation.status = "running"
            operation.error_code = None
            operation.copied_status = "running"
            next_attempt_at: datetime | None = None
            terminal_scope_failure = False

            if (operation.evidence_scope_status == "captured"
                    and operation.agent_status in {"queued", "running"}):
                if agent_scope is None or agent_preflight is None:
                    raise ValueError("Agent cleanup lease preflight is unavailable")
                cursor_state = operation.agent_cursor or {
                    "v": 1, "reference_after": None, "candidate_after": None, "phase": "discover",
                }
                reference_value = cursor_state.get("reference_after")
                candidate_cursor = cursor_state.get("candidate_after")
                reference_after = UUID(reference_value) if reference_value else None
                from modules.agents import public as agents

                attempt_stage = "agent"
                agent_progress = await agents.purge_document_copied_evidence_page(
                    session, agent_scope, cursor=candidate_cursor, limit=100,
                    preflight=agent_preflight,
                )
                if agent_progress.preflight_stale:
                    # Drop receipt/privacy locks and any successful stale lease before retrying.
                    await session.rollback()
                    async with factory() as retry_session:
                        retry_event = await ingestion.get_event_delivery(retry_session, identifier)
                        if retry_event is not None and retry_event.status != "delivered":
                            await ingestion.set_event_delivery(
                                retry_session, identifier, "pending",
                                next_attempt_at=datetime.now(UTC) + _CONTINUATION_DELAY,
                            )
                            await retry_session.commit()
                    return
                operation.agent_unresolved_count = agent_progress.unavailable_count
                operation.agent_waiting_for_lease = agent_progress.lease_pending
                operation.agent_error_code = None
                if agent_progress.complete:
                    if agent_scope.next_cursor is not None:
                        operation.agent_status = "running"
                        operation.agent_cursor = {
                            "v": 1, "reference_after": str(agent_scope.next_cursor),
                            "candidate_after": None, "phase": "discover",
                        }
                        next_attempt_at = datetime.now(UTC) + _CONTINUATION_DELAY
                    else:
                        operation.agent_cursor = None
                        operation.agent_waiting_for_lease = False
                        if agent_progress.unavailable_count:
                            operation.agent_status = "failed"
                            operation.agent_error_code = "evidence_identity_unavailable"
                        else:
                            operation.agent_status = "succeeded"
                        await documents.publish_source_cleanup_wakeup(
                            session, operation,
                            progress_key=_source_cleanup_progress_key(operation, "agent"),
                        )
                else:
                    if agent_progress.next_cursor is None:
                        raise ValueError("Agent cleanup page is incomplete without a continuation cursor")
                    operation.agent_status = "running"
                    operation.agent_cursor = {
                        "v": 1,
                        "reference_after": str(reference_after) if reference_after else None,
                        "candidate_after": agent_progress.next_cursor,
                        "phase": "finalize" if agent_progress.lease_pending else "discover",
                    }
                    next_attempt_at = datetime.now(UTC) + _CONTINUATION_DELAY

            if operation.evidence_scope_status != "captured":
                operation.chat_status = "failed"
                operation.chat_error_code = "evidence_identity_unavailable"
                operation.copied_status = "failed"
                operation.copied_error_code = "evidence_identity_unavailable"
                operation.error_code = "evidence_identity_unavailable"
                operation.agent_status = "failed"
                operation.agent_error_code = "evidence_identity_unavailable"
                operation.agent_cursor = None
                operation.agent_waiting_for_lease = False
                terminal_scope_failure = True
            elif operation.chat_status != "succeeded":
                attempt_stage = "chat"
                operation.chat_status = "running"
                operation.chat_error_code = None
                cursor_state = operation.copied_cursor or {}
                if not isinstance(cursor_state, dict):
                    cursor_state = {}
                reference_after = UUID(str(cursor_state["reference_after"])) if cursor_state.get("reference_after") else None
                chat_cursor = cursor_state.get("chat_cursor")
                if chat_cursor is not None and not isinstance(chat_cursor, str):
                    raise ValueError("Stored Chat cleanup cursor is malformed")
                scope = await documents.list_document_cleanup_evidence_scope(
                    session, operation.id, after=reference_after, limit=100,
                )
                if scope is None:
                    raise ValueError("Document cleanup evidence scope is unavailable")
                from modules.chat import public as chat

                progress = await chat.purge_document_copied_evidence_page(
                    session, scope, cursor=chat_cursor, limit=100,
                )
                if progress.complete:
                    if scope.next_cursor is None:
                        operation.chat_status = "succeeded"
                        operation.chat_error_code = None
                        operation.copied_cursor = None
                    else:
                        operation.copied_cursor = {
                            "reference_after": str(scope.next_cursor),
                            "chat_cursor": None,
                        }
                        next_attempt_at = datetime.now(UTC) + _CONTINUATION_DELAY
                else:
                    if progress.next_cursor is None:
                        raise ValueError("Chat cleanup page is incomplete without a continuation cursor")
                    operation.copied_cursor = {
                        "reference_after": str(reference_after) if reference_after else None,
                        "chat_cursor": progress.next_cursor,
                    }
                    next_attempt_at = datetime.now(UTC) + _CONTINUATION_DELAY

            if operation.chat_status == "succeeded" and operation.memory_status not in {
                "succeeded",
            } and not (
                operation.memory_status == "failed"
                and operation.memory_error_code in {
                    "legacy_provenance_unresolved", "evidence_identity_unavailable",
                }
            ):
                if operation.memory_cache_pending:
                    next_attempt_at = next_attempt_at or datetime.now(UTC) + _RETRY_DELAY
                else:
                    attempt_stage = "memory"
                    operation.memory_status = "running"
                    operation.memory_error_code = None
                    changed, terminal = await _advance_memory_cleanup(session, operation)
                    if changed:
                        evict_after_commit = operation.id
                    if not terminal:
                        next_attempt_at = next_attempt_at or datetime.now(UTC) + _CONTINUATION_DELAY

            if operation.raw_status == "failed":
                operation.error_code = operation.error_code or "file_cleanup_failed"
                next_attempt_at = next_attempt_at or datetime.now(UTC) + _RETRY_DELAY
            if operation.chat_status == "failed" and not terminal_scope_failure:
                operation.copied_status = "failed"
                operation.copied_error_code = operation.chat_error_code or "chat_cleanup_failed"
                operation.error_code = operation.copied_error_code
                next_attempt_at = next_attempt_at or datetime.now(UTC) + _RETRY_DELAY

            if operation.agent_status == "running":
                next_attempt_at = next_attempt_at or datetime.now(UTC) + _CONTINUATION_DELAY
            elif operation.agent_status == "failed":
                operation.copied_error_code = operation.agent_error_code or "agent_cleanup_failed"
                operation.error_code = operation.copied_error_code

            if (operation.chat_status in {"succeeded", "failed"}
                    or operation.memory_status in {"succeeded", "failed"}
                    or operation.agent_status in {"succeeded", "failed"}):
                await documents.publish_source_cleanup_wakeup(
                    session, operation,
                    progress_key=_source_cleanup_progress_key(operation, "aggregate"),
                )

            if (operation.raw_status == "failed" or operation.chat_status == "failed"
                    or operation.memory_status == "failed" or operation.agent_status == "failed"):
                operation.status = "failed"
            else:
                # Other copied-evidence owners are not implemented by this worker.
                operation.status = "running"
            local_failed = (operation.raw_status == "failed" or operation.chat_status == "failed"
                            or operation.memory_status == "failed" or operation.agent_status == "failed")
            operation.copied_status = "failed" if local_failed else "running"

            if operation.memory_cache_pending:
                evict_after_commit = operation.id
                next_attempt_at = next_attempt_at or datetime.now(UTC) + _RETRY_DELAY

            if next_attempt_at is not None:
                await ingestion.set_event_delivery(
                    session, identifier, "pending", next_attempt_at=next_attempt_at,
                )
            else:
                # The event is complete for Documents and Chat even while other copy owners remain pending.
                await ingestion.set_event_delivery(session, identifier, "delivered")
            await session.commit()
        if evict_after_commit is not None:
            await _evict_memory_cache_after_commit(factory, redis, evict_after_commit, identifier)
    except ValueError:
        # A malformed local continuation restarts idempotently from the first exact identity page.
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
            operation_hint = await session.scalar(select(DocumentCleanupOperation).where(
                DocumentCleanupOperation.id == operation_id,
            ))
            if operation_hint is None:
                await ingestion.set_event_delivery(session, identifier, "failed")
                await session.commit()
                return
            await lock_export_privacy(session)
            recovery_operation = await session.scalar(select(DocumentCleanupOperation).where(
                DocumentCleanupOperation.id == operation_id,
            ).with_for_update().execution_options(populate_existing=True))
            progress_unchanged = (
                recovery_operation is not None
                and attempt_progress is not None
                and recovery_operation.raw_status == attempt_progress[0]
                and recovery_operation.evidence_scope_status == attempt_progress[1]
                and recovery_operation.chat_status == attempt_progress[2]
                and recovery_operation.copied_status == attempt_progress[3]
                and recovery_operation.copied_cursor == attempt_progress[4]
                and recovery_operation.memory_status == attempt_progress[5]
                and recovery_operation.memory_error_code == attempt_progress[6]
                and recovery_operation.memory_cursor == attempt_progress[7]
                and recovery_operation.memory_unresolved_count == attempt_progress[8]
                and recovery_operation.memory_cache_pending == attempt_progress[9]
                and recovery_operation.agent_status == attempt_progress[10]
                and recovery_operation.agent_error_code == attempt_progress[11]
                and recovery_operation.agent_cursor == attempt_progress[12]
                and recovery_operation.agent_unresolved_count == attempt_progress[13]
                and recovery_operation.agent_waiting_for_lease == attempt_progress[14]
            )
            if (recovery_operation is not None and progress_unchanged and attempt_stage == "agent"
                    and recovery_operation.agent_status != "succeeded"
                    and not (recovery_operation.agent_status == "failed"
                             and recovery_operation.agent_error_code == "evidence_identity_unavailable")):
                recovery_operation.agent_status = "queued"
                recovery_operation.agent_error_code = "agent_cursor_reset"
                recovery_operation.agent_cursor = None
                recovery_operation.agent_waiting_for_lease = False
                recovery_operation.copied_status = "failed"
                recovery_operation.copied_error_code = "agent_cursor_reset"
                recovery_operation.status = "failed"
                recovery_operation.error_code = "agent_cursor_reset"
                await documents.publish_source_cleanup_wakeup(
                    session, recovery_operation,
                    progress_key=_source_cleanup_progress_key(recovery_operation, "agent-recovery"),
                )
                await ingestion.set_event_delivery(
                    session, identifier, "pending", next_attempt_at=datetime.now(UTC) + _RETRY_DELAY,
                )
            elif (recovery_operation is not None and progress_unchanged
                    and attempt_stage == "memory" and recovery_operation.memory_status not in {"succeeded"}
                    and recovery_operation.memory_error_code not in {
                        "legacy_provenance_unresolved", "evidence_identity_unavailable",
                    }):
                recovery_operation.memory_status = "failed"
                recovery_operation.memory_error_code = "memory_cursor_reset"
                recovery_operation.memory_cursor = None
                recovery_operation.memory_unresolved_count = 0
                recovery_operation.copied_status = "failed"
                recovery_operation.error_code = "memory_cursor_reset"
                recovery_operation.copied_error_code = "memory_cursor_reset"
                recovery_operation.status = "failed"
                await documents.publish_source_cleanup_wakeup(
                    session, recovery_operation,
                    progress_key=_source_cleanup_progress_key(recovery_operation, "memory-recovery"),
                )
                await ingestion.set_event_delivery(
                    session, identifier, "pending", next_attempt_at=datetime.now(UTC) + _RETRY_DELAY,
                )
            elif recovery_operation is not None and progress_unchanged and recovery_operation.chat_status != "succeeded":
                recovery_operation.copied_cursor = None
                recovery_operation.chat_status = "failed"
                recovery_operation.chat_error_code = "chat_cursor_reset"
                recovery_operation.copied_status = "failed"
                recovery_operation.copied_error_code = "chat_cursor_reset"
                recovery_operation.status = "failed"
                recovery_operation.error_code = "chat_cursor_reset"
                await documents.publish_source_cleanup_wakeup(
                    session, recovery_operation,
                    progress_key=_source_cleanup_progress_key(recovery_operation, "chat-recovery"),
                )
                await ingestion.set_event_delivery(
                    session, identifier, "pending", next_attempt_at=datetime.now(UTC) + _RETRY_DELAY,
                )
            elif (
                progress_unchanged
                and recovery_operation is not None
                and recovery_operation.raw_status not in {"not_present", "retained_shared", "succeeded"}
            ):
                await ingestion.set_event_delivery(
                    session, identifier, "pending", next_attempt_at=datetime.now(UTC) + _RETRY_DELAY,
                )
            # Unknown snapshots, missing receipts and terminal stages do not own a delivery rewrite.
            await session.commit()
    except Exception as exc:
        logger.warning("Document copied-evidence cleanup deferred (%s)", type(exc).__name__)
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
            operation_hint = await session.scalar(select(DocumentCleanupOperation).where(
                DocumentCleanupOperation.id == operation_id,
            ))
            if operation_hint is None:
                await ingestion.set_event_delivery(session, identifier, "failed")
                await session.commit()
                return
            await lock_export_privacy(session)
            recovery_operation = await session.scalar(select(DocumentCleanupOperation).where(
                DocumentCleanupOperation.id == operation_id,
            ).with_for_update().execution_options(populate_existing=True))
            progress_unchanged = (
                recovery_operation is not None
                and attempt_progress is not None
                and recovery_operation.raw_status == attempt_progress[0]
                and recovery_operation.evidence_scope_status == attempt_progress[1]
                and recovery_operation.chat_status == attempt_progress[2]
                and recovery_operation.copied_status == attempt_progress[3]
                and recovery_operation.copied_cursor == attempt_progress[4]
                and recovery_operation.memory_status == attempt_progress[5]
                and recovery_operation.memory_error_code == attempt_progress[6]
                and recovery_operation.memory_cursor == attempt_progress[7]
                and recovery_operation.memory_unresolved_count == attempt_progress[8]
                and recovery_operation.memory_cache_pending == attempt_progress[9]
                and recovery_operation.agent_status == attempt_progress[10]
                and recovery_operation.agent_error_code == attempt_progress[11]
                and recovery_operation.agent_cursor == attempt_progress[12]
                and recovery_operation.agent_unresolved_count == attempt_progress[13]
                and recovery_operation.agent_waiting_for_lease == attempt_progress[14]
            )
            if (recovery_operation is not None and progress_unchanged and attempt_stage == "agent"
                    and recovery_operation.agent_status != "succeeded"
                    and not (recovery_operation.agent_status == "failed"
                             and recovery_operation.agent_error_code == "evidence_identity_unavailable")):
                recovery_operation.agent_status = "queued"
                recovery_operation.agent_error_code = "agent_cleanup_failed"
                recovery_operation.copied_status = "failed"
                recovery_operation.copied_error_code = "agent_cleanup_failed"
                recovery_operation.status = "failed"
                recovery_operation.error_code = "agent_cleanup_failed"
                await documents.publish_source_cleanup_wakeup(
                    session, recovery_operation,
                    progress_key=_source_cleanup_progress_key(recovery_operation, "agent-recovery"),
                )
                await ingestion.set_event_delivery(
                    session, identifier, "pending", next_attempt_at=datetime.now(UTC) + _RETRY_DELAY,
                )
            elif (recovery_operation is not None and progress_unchanged and attempt_stage == "memory"
                    and recovery_operation.memory_status not in {"succeeded"}
                    and recovery_operation.memory_error_code not in {
                        "legacy_provenance_unresolved", "evidence_identity_unavailable",
                    }):
                recovery_operation.memory_status = "failed"
                recovery_operation.memory_error_code = "memory_cleanup_failed"
                recovery_operation.copied_status = "failed"
                recovery_operation.copied_error_code = "memory_cleanup_failed"
                recovery_operation.status = "failed"
                recovery_operation.error_code = "memory_cleanup_failed"
                await documents.publish_source_cleanup_wakeup(
                    session, recovery_operation,
                    progress_key=_source_cleanup_progress_key(recovery_operation, "memory-recovery"),
                )
                await ingestion.set_event_delivery(
                    session, identifier, "pending", next_attempt_at=datetime.now(UTC) + _RETRY_DELAY,
                )
            elif recovery_operation is not None and progress_unchanged and recovery_operation.chat_status != "succeeded":
                recovery_operation.chat_status = "failed"
                recovery_operation.chat_error_code = "chat_cleanup_failed"
                recovery_operation.copied_status = "failed"
                recovery_operation.copied_error_code = "chat_cleanup_failed"
                recovery_operation.status = "failed"
                recovery_operation.error_code = "chat_cleanup_failed"
                await documents.publish_source_cleanup_wakeup(
                    session, recovery_operation,
                    progress_key=_source_cleanup_progress_key(recovery_operation, "chat-recovery"),
                )
                await ingestion.set_event_delivery(
                    session, identifier, "pending", next_attempt_at=datetime.now(UTC) + _RETRY_DELAY,
                )
            elif (
                progress_unchanged
                and recovery_operation is not None
                and recovery_operation.raw_status not in {"not_present", "retained_shared", "succeeded"}
            ):
                await ingestion.set_event_delivery(
                    session, identifier, "pending", next_attempt_at=datetime.now(UTC) + _RETRY_DELAY,
                )
            # Unknown snapshots, missing receipts and terminal stages do not own a delivery rewrite.
            await session.commit()
