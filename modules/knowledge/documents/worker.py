"""Documents-owned bounded cleanup consumer for raw files and copied-evidence owner stages."""

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
_copied_stage_reconcile_cursor: UUID | None = None
# Failures with these codes are truthful terminal limitations (unavailable lineage); they never retry.
_COPIED_STAGE_TERMINAL_CODES = frozenset({
    "evidence_identity_unavailable", "legacy_provenance_unresolved", "legacy_coverage_unavailable",
})
_MATERIALIZATION_PHASES = ("notifications", "triggers", "runs")


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
        operation.materialization_status,
        operation.materialization_error_code,
        operation.materialization_unresolved_count,
        operation.brief_status,
        operation.brief_error_code,
        operation.brief_unresolved_count,
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
        operation.materialization_status,
        operation.materialization_error_code,
        dict(operation.materialization_cursor) if isinstance(operation.materialization_cursor, dict) else operation.materialization_cursor,
        operation.materialization_unresolved_count,
        operation.brief_status,
        operation.brief_error_code,
        dict(operation.brief_cursor) if isinstance(operation.brief_cursor, dict) else operation.brief_cursor,
        operation.brief_unresolved_count,
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


def _copied_stage_terminal(status: str, error_code: str | None) -> bool:
    """Return whether a materialization/brief stage is finished, including unavailable limits."""
    return status == "succeeded" or (status == "failed" and error_code in _COPIED_STAGE_TERMINAL_CODES)


def _optional_uuid(value: object, label: str) -> UUID | None:
    """Parse one stored cursor component, rejecting anything but a canonical UUID string or null."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Stored {label} cursor is malformed")
    parsed = UUID(value)
    if str(parsed) != value:
        raise ValueError(f"Stored {label} cursor is malformed")
    return parsed


async def _advance_materialization_cleanup(
    session: AsyncSession, operation: DocumentCleanupOperation,
) -> bool:
    """Flush one Notifications/Automations page for this receipt and return whether it is terminal.

    Runs inside the caller's privacy -> receipt lock transaction; owner hooks only lock their own
    rows (stable UUID keyset, <=100) and never the already-deleted Document. The private cursor
    ``{v, phase, reference_after, owner_after}`` walks notifications, then automation triggers, then
    automation runs for each <=100-identity reference page; it is independent of the Chat, Memory
    and Agent cursors. Owner "unavailable" IDs (only reported on the final reference page) add to
    ``materialization_unresolved_count``; that count and the cursor commit together, so a retry
    cannot double count. Unresolved legacy lineage ends ``failed`` and is never relabeled clean.
    """
    if operation.evidence_scope_status != "captured":
        operation.materialization_status = "failed"
        operation.materialization_error_code = "evidence_identity_unavailable"
        operation.materialization_cursor = None
        return True
    state = operation.materialization_cursor or {
        "v": 1, "phase": "notifications", "reference_after": None, "owner_after": None,
    }
    if (not isinstance(state, dict) or set(state) != {"v", "phase", "reference_after", "owner_after"}
            or state["v"] != 1 or state["phase"] not in _MATERIALIZATION_PHASES):
        raise ValueError("Stored materialization cleanup cursor is malformed")
    phase = str(state["phase"])
    reference_after = _optional_uuid(state["reference_after"], "materialization reference")
    owner_after = _optional_uuid(state["owner_after"], "materialization owner")
    scope = await documents.list_document_cleanup_evidence_scope(
        session, operation.id, after=reference_after, limit=100,
    )
    if scope is None:
        raise ValueError("Document cleanup evidence scope is unavailable")
    version_ids = tuple(dict.fromkeys(ref.document_version_id for ref in scope.references))
    final_page = scope.next_cursor is None
    if phase == "notifications":
        from modules.notifications import public as notifications

        progress = await notifications.scrub_document_evidence(
            session, operation_id=operation.id, document_id=scope.document_id,
            version_ids=version_ids, final_reference_page=final_page, after=owner_after, limit=100,
        )
    else:
        from modules.automations import public as automations

        hook = automations.scrub_document_triggers if phase == "triggers" else automations.scrub_document_runs
        progress = await hook(
            session, operation_id=operation.id, document_id=scope.document_id,
            source_id=scope.source_id, version_ids=version_ids,
            final_reference_page=final_page, after=owner_after, limit=100,
        )
    # Only terminal (final-page) unavailable rows are reported; provisional IDs are revisited later.
    operation.materialization_unresolved_count += len(progress.unavailable_ids)
    operation.materialization_status = "running"
    if not progress.complete:
        if progress.next_cursor is None:
            raise ValueError("Materialization cleanup page is incomplete without a continuation cursor")
        operation.materialization_cursor = {
            "v": 1, "phase": phase,
            "reference_after": str(reference_after) if reference_after else None,
            "owner_after": str(progress.next_cursor),
        }
        return False
    if phase != "runs":
        operation.materialization_cursor = {
            "v": 1, "phase": _MATERIALIZATION_PHASES[_MATERIALIZATION_PHASES.index(phase) + 1],
            "reference_after": str(reference_after) if reference_after else None, "owner_after": None,
        }
        return False
    if scope.next_cursor is not None:
        operation.materialization_cursor = {
            "v": 1, "phase": "notifications",
            "reference_after": str(scope.next_cursor), "owner_after": None,
        }
        return False
    operation.materialization_cursor = None
    if operation.materialization_unresolved_count:
        operation.materialization_status = "failed"
        operation.materialization_error_code = "legacy_provenance_unresolved"
    else:
        operation.materialization_status = "succeeded"
        operation.materialization_error_code = None
    return True


async def _advance_brief_cleanup(
    session: AsyncSession, operation: DocumentCleanupOperation,
) -> bool:
    """Flush one Dashboard saved-brief page for this receipt and return whether it is terminal.

    Dashboard keys captured prompt dependencies by the detached ``document_id`` alone, so the
    stage needs neither the evidence reference pages nor a live Document. Phase ``sidecars`` scrubs
    briefs whose captured manifest names the Document; phase ``legacy`` then reports (read-only)
    briefs with prose but no manifest, whose Document dependencies are unknowable. Each legacy
    brief adds one to ``brief_unresolved_count`` and the stage ends ``failed`` with
    ``legacy_coverage_unavailable``: reads already withhold such briefs, but they are never
    reported clean and the terminal code prevents a retry loop. Only legacy briefs generated at or
    after the receipt's recorded earliest-version time count (earlier ones cannot mention the
    Document); an unknown time (historical receipt) counts them all. The cursor is ``{v, phase, after}``.
    """
    state = operation.brief_cursor or {"v": 1, "phase": "sidecars", "after": None}
    if (not isinstance(state, dict) or set(state) != {"v", "phase", "after"}
            or state["v"] != 1 or state["phase"] not in {"sidecars", "legacy"}):
        raise ValueError("Stored brief cleanup cursor is malformed")
    phase = str(state["phase"])
    after = _optional_uuid(state["after"], "brief")
    from modules.dashboard import public as dashboard

    operation.brief_status = "running"
    if phase == "sidecars":
        progress = await dashboard.clean_document_brief_evidence(
            session, operation.document_id, after_brief_id=after, limit=100,
        )
        operation.brief_cursor = {
            "v": 1, "phase": "sidecars" if progress.next_cursor else "legacy",
            "after": str(progress.next_cursor) if progress.next_cursor else None,
        }
        return False
    coverage = await dashboard.legacy_brief_coverage(
        session, after_brief_id=after, limit=100, not_before=operation.earliest_version_created_at,
    )
    operation.brief_unresolved_count += len(coverage.candidate_ids)
    if coverage.next_cursor is not None:
        operation.brief_cursor = {"v": 1, "phase": "legacy", "after": str(coverage.next_cursor)}
        return False
    operation.brief_cursor = None
    if operation.brief_unresolved_count:
        operation.brief_status = "failed"
        operation.brief_error_code = "legacy_coverage_unavailable"
    else:
        operation.brief_status = "succeeded"
        operation.brief_error_code = None
    return True


def _fail_copied_stage(
    operation: DocumentCleanupOperation, stage: str, code: str, *, reset: bool,
) -> None:
    """Record a retryable failure of one materialization/brief stage; a reset also restarts its cursor.

    A cursor reset zeroes the stage's unresolved count because the restart recounts from scratch.
    """
    setattr(operation, f"{stage}_status", "failed")
    setattr(operation, f"{stage}_error_code", code)
    if reset:
        setattr(operation, f"{stage}_cursor", None)
        setattr(operation, f"{stage}_unresolved_count", 0)
    operation.copied_status = "failed"
    operation.copied_error_code = code
    operation.status = "failed"
    operation.error_code = code


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
                    and operation.agent_status in {"succeeded", "failed"}
                    and _copied_stage_terminal(operation.materialization_status, operation.materialization_error_code)
                    and _copied_stage_terminal(operation.brief_status, operation.brief_error_code)):
                await ingestion.set_event_delivery(session, event_id, "delivered")
            elif (operation.raw_status in {"not_present", "retained_shared", "succeeded"}
                    and operation.chat_status == "succeeded"
                    and (operation.memory_status == "running" or operation.agent_status == "running"
                         or not _copied_stage_terminal(operation.materialization_status, operation.materialization_error_code)
                         or not _copied_stage_terminal(operation.brief_status, operation.brief_error_code))):
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


async def reconcile_document_copied_stage_cleanup(ctx: dict[str, object]) -> int:
    """Fairly scan one bounded UUID page of receipts with a nonterminal materialization/brief stage.

    Reopens a delivered or failed deterministic cleanup event idempotently and preserves the
    schedule of a pending one. Terminal unavailable stages are excluded by the Documents query.
    """
    global _copied_stage_reconcile_cursor
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    now = datetime.now(UTC)
    async with factory() as session:
        operation_ids = await documents.pending_document_copied_stage_cleanup_ids(
            session, after=_copied_stage_reconcile_cursor, limit=100,
        )
        if not operation_ids and _copied_stage_reconcile_cursor is not None:
            _copied_stage_reconcile_cursor = None
            operation_ids = await documents.pending_document_copied_stage_cleanup_ids(session, limit=100)
        if operation_ids:
            _copied_stage_reconcile_cursor = operation_ids[-1]
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
                    and _copied_stage_terminal(operation.materialization_status, operation.materialization_error_code)
                    and _copied_stage_terminal(operation.brief_status, operation.brief_error_code)
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

            # Materialization (Notifications/Automations) and saved-brief stages keep their own
            # cursors and run under the same privacy -> receipt locks; one page each per delivery.
            if not _copied_stage_terminal(operation.materialization_status, operation.materialization_error_code):
                attempt_stage = "materialization"
                operation.materialization_error_code = None
                if not await _advance_materialization_cleanup(session, operation):
                    next_attempt_at = next_attempt_at or datetime.now(UTC) + _CONTINUATION_DELAY
            if not _copied_stage_terminal(operation.brief_status, operation.brief_error_code):
                attempt_stage = "brief"
                operation.brief_error_code = None
                if not await _advance_brief_cleanup(session, operation):
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

            for stage in ("materialization", "brief"):
                if getattr(operation, f"{stage}_status") == "failed":
                    operation.copied_error_code = getattr(operation, f"{stage}_error_code") or f"{stage}_cleanup_failed"
                    operation.error_code = operation.copied_error_code

            local_failed = (
                operation.raw_status == "failed" or operation.chat_status == "failed"
                or operation.memory_status == "failed" or operation.agent_status == "failed"
                or operation.materialization_status == "failed" or operation.brief_status == "failed"
            )
            if local_failed:
                operation.status = "failed"
                operation.copied_status = "failed"
            elif (operation.raw_status in {"not_present", "retained_shared", "succeeded"}
                    and operation.chat_status == "succeeded" and operation.memory_status == "succeeded"
                    and not operation.memory_cache_pending and operation.agent_status == "succeeded"
                    and operation.materialization_status == "succeeded"
                    and operation.brief_status == "succeeded"):
                # Aggregate success only when every required owner stage genuinely completed.
                operation.status = "succeeded"
                operation.copied_status = "succeeded"
                operation.copied_error_code = None
                operation.error_code = None
            else:
                operation.status = "running"
                operation.copied_status = "running"

            if (operation.chat_status in {"succeeded", "failed"}
                    or operation.memory_status in {"succeeded", "failed"}
                    or operation.agent_status in {"succeeded", "failed"}
                    or operation.materialization_status in {"succeeded", "failed"}
                    or operation.brief_status in {"succeeded", "failed"}):
                # Published after the aggregate is final so Source observes the settled state.
                await documents.publish_source_cleanup_wakeup(
                    session, operation,
                    progress_key=_source_cleanup_progress_key(operation, "aggregate"),
                )

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
                and _attempt_progress_snapshot(recovery_operation) == attempt_progress
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
                    and attempt_stage in {"materialization", "brief"}):
                _fail_copied_stage(recovery_operation, attempt_stage, f"{attempt_stage}_cursor_reset", reset=True)
                await documents.publish_source_cleanup_wakeup(
                    session, recovery_operation,
                    progress_key=_source_cleanup_progress_key(recovery_operation, f"{attempt_stage}-recovery"),
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
                and _attempt_progress_snapshot(recovery_operation) == attempt_progress
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
            elif (recovery_operation is not None and progress_unchanged
                    and attempt_stage in {"materialization", "brief"}):
                _fail_copied_stage(recovery_operation, attempt_stage, f"{attempt_stage}_cleanup_failed", reset=False)
                await documents.publish_source_cleanup_wakeup(
                    session, recovery_operation,
                    progress_key=_source_cleanup_progress_key(recovery_operation, f"{attempt_stage}-recovery"),
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
