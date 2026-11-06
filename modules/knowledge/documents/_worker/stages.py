"""Agent, chat, memory and copied-evidence stage advancers and attempt recovery behind ``modules.knowledge.documents.worker``."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.workspaces.schemas import InternalJobScope
from modules.knowledge.documents import public as documents
from modules.knowledge.documents._worker.admit import (
    _admit_cleanup,
    _agent_preflight,
    _AgentPreflight,
    _Attempt,
    _commit,
    _cursor_state_agent,
    _hint,
    _lock_claim,
    _PageSchedule,
    _publish_wakeups,
    _receipt_query,
    _ReceiptSnapshot,
    _requeue,
    _settle,
)
from modules.knowledge.documents._worker.cleanup import (
    _advance_brief_cleanup,
    _advance_materialization_cleanup,
    _advance_memory_cleanup,
)
from modules.knowledge.documents._worker.progress import (
    _CONTINUATION_DELAY,
    _RAW_URI_UNAVAILABLE,
    _RETRY_DELAY,
    _attempt_progress_snapshot,
    _copied_stage_terminal,
    _memory_terminal,
    _receipt_fully_settled,
)
from modules.knowledge.documents._worker.stage import rewrite_recovered_stage
from modules.knowledge.documents.models import DocumentCleanupOperation
from modules.memory.public import (
    lock_export_privacy_in_uow,
)


async def _advance_agent_stage(
    session: AsyncSession,
    operation: DocumentCleanupOperation,
    pre: _AgentPreflight | None,
    *,
    attempt: _Attempt,
    schedule: _PageSchedule,
    job_scope: InternalJobScope,
    flag: bool,
) -> bool:
    """Purge one Agent page under the receipt lock; ``False`` means the lease preflight went stale.

    The caller rolls back (dropping receipt/privacy locks and the stale advisory lease) and
    requeues on ``False``. Otherwise the Agent cursor/status/counters are advanced in place and
    the page may request a continuation. No-op unless the Agent stage is queued/running on a
    captured evidence scope. Raises ``ValueError`` for a malformed cursor or an incomplete page
    without a continuation cursor.
    """
    if not (
        operation.evidence_scope_status == "captured"
        and operation.agent_status in {"queued", "running"}
    ):
        return True
    if pre is None:
        raise ValueError("Agent cleanup lease preflight is unavailable")
    cursor_state = _cursor_state_agent(operation.agent_cursor)
    reference_value = cursor_state.get("reference_after")
    candidate_cursor = cursor_state.get("candidate_after")
    if not isinstance(reference_value, (str, type(None))) or not isinstance(
        candidate_cursor, (str, type(None))
    ):
        raise ValueError("Stored Agent cleanup cursor is malformed")
    reference_after = UUID(reference_value) if reference_value else None
    from modules.agents import public as agents

    attempt.stage = "agent"
    agent_progress = await agents.purge_document_copied_evidence_page(
        session,
        pre.evidence,
        cursor=candidate_cursor,
        limit=100,
        preflight=pre.result,
        scope=job_scope,
        multi_workspace_enabled=flag,
    )
    if agent_progress.preflight_stale:
        return False
    operation.agent_unresolved_count = agent_progress.unavailable_count
    operation.agent_waiting_for_lease = agent_progress.lease_pending
    operation.agent_error_code = None
    if agent_progress.complete:
        if pre.evidence.next_cursor is not None:
            operation.agent_status = "running"
            operation.agent_cursor = {
                "v": 1,
                "reference_after": str(pre.evidence.next_cursor),
                "candidate_after": None,
                "phase": "discover",
            }
            schedule.next_attempt_at = datetime.now(UTC) + _CONTINUATION_DELAY
        else:
            operation.agent_cursor = None
            operation.agent_waiting_for_lease = False
            if agent_progress.unavailable_count:
                operation.agent_status = "failed"
                operation.agent_error_code = "evidence_identity_unavailable"
            else:
                operation.agent_status = "succeeded"
            _hint(attempt.hints, operation, "agent")
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
        schedule.next_attempt_at = datetime.now(UTC) + _CONTINUATION_DELAY
    return True


def _fail_uncaptured_scope(operation: DocumentCleanupOperation) -> None:
    """Mark Chat, copied and Agent stages terminally failed when evidence identities are unavailable."""
    operation.chat_status = "failed"
    operation.chat_error_code = "evidence_identity_unavailable"
    operation.copied_status = "failed"
    operation.copied_error_code = "evidence_identity_unavailable"
    operation.error_code = "evidence_identity_unavailable"
    operation.agent_status = "failed"
    operation.agent_error_code = "evidence_identity_unavailable"
    operation.agent_cursor = None
    operation.agent_waiting_for_lease = False


async def _advance_chat_stage(
    session: AsyncSession,
    operation: DocumentCleanupOperation,
    *,
    attempt: _Attempt,
    schedule: _PageSchedule,
    job_scope: InternalJobScope,
    flag: bool,
) -> None:
    """Purge one Chat page of copied evidence and advance the shared ``copied_cursor``.

    Marks the stage running, reads the next bounded evidence-identity page, and either completes
    Chat, moves the reference cursor, or stores the inner Chat cursor (each continuation schedules
    the continuation delay). Raises ``ValueError`` for a malformed stored cursor, an unavailable
    evidence scope, or an incomplete page without a continuation cursor.
    """
    attempt.stage = "chat"
    operation.chat_status = "running"
    operation.chat_error_code = None
    cursor_state = operation.copied_cursor or {}
    if not isinstance(cursor_state, dict):
        cursor_state = {}
    reference_after = (
        UUID(str(cursor_state["reference_after"])) if cursor_state.get("reference_after") else None
    )
    chat_cursor = cursor_state.get("chat_cursor")
    if chat_cursor is not None and not isinstance(chat_cursor, str):
        raise ValueError("Stored Chat cleanup cursor is malformed")
    evidence = await documents.list_document_cleanup_evidence_scope(
        session,
        operation.id,
        after=reference_after,
        limit=100,
        scope=job_scope,
        multi_workspace_enabled=flag,
    )
    if evidence is None:
        raise ValueError("Document cleanup evidence scope is unavailable")
    from modules.chat import public as chat

    progress = await chat.purge_document_copied_evidence_page(
        session,
        evidence,
        cursor=chat_cursor,
        limit=100,
        scope=job_scope,
        multi_workspace_enabled=flag,
    )
    if progress.complete:
        if evidence.next_cursor is None:
            operation.chat_status = "succeeded"
            operation.chat_error_code = None
            operation.copied_cursor = None
        else:
            operation.copied_cursor = {
                "reference_after": str(evidence.next_cursor),
                "chat_cursor": None,
            }
            schedule.next_attempt_at = datetime.now(UTC) + _CONTINUATION_DELAY
    else:
        if progress.next_cursor is None:
            raise ValueError("Chat cleanup page is incomplete without a continuation cursor")
        operation.copied_cursor = {
            "reference_after": str(reference_after) if reference_after else None,
            "chat_cursor": progress.next_cursor,
        }
        schedule.next_attempt_at = datetime.now(UTC) + _CONTINUATION_DELAY


async def _advance_memory_stage(
    session: AsyncSession,
    operation: DocumentCleanupOperation,
    *,
    attempt: _Attempt,
    schedule: _PageSchedule,
    job_scope: InternalJobScope,
    flag: bool,
) -> None:
    """Run one Memory page once Chat succeeded and Memory is not yet final.

    A pending cache eviction owns the retry (no Memory page this delivery). Otherwise a page runs;
    changed rows request postcommit cache eviction and a non-terminal page defers a continuation.
    """
    if not (operation.chat_status == "succeeded" and not _memory_terminal(operation)):
        return
    if operation.memory_cache_pending:
        schedule.defer(_RETRY_DELAY)
        return
    attempt.stage = "memory"
    operation.memory_status = "running"
    operation.memory_error_code = None
    changed, terminal = await _advance_memory_cleanup(
        session,
        operation,
        job_scope=job_scope,
        flag=flag,
    )
    if changed:
        attempt.evict = True
    if not terminal:
        schedule.defer(_CONTINUATION_DELAY)


def _collect_stage_failures(operation: DocumentCleanupOperation, schedule: _PageSchedule) -> None:
    """Fold per-stage failures into receipt error codes and schedule retries for retryable ones."""
    if operation.raw_status == "failed":
        operation.error_code = operation.error_code or "file_cleanup_failed"
        if operation.error_code != _RAW_URI_UNAVAILABLE:
            schedule.defer(_RETRY_DELAY)
    if operation.chat_status == "failed" and not schedule.terminal_scope_failure:
        operation.copied_status = "failed"
        operation.copied_error_code = operation.chat_error_code or "chat_cleanup_failed"
        operation.error_code = operation.copied_error_code
        schedule.defer(_RETRY_DELAY)

    if operation.agent_status == "running":
        schedule.defer(_CONTINUATION_DELAY)
    elif operation.agent_status == "failed":
        operation.copied_error_code = operation.agent_error_code or "agent_cleanup_failed"
        operation.error_code = operation.copied_error_code

    for stage in ("materialization", "brief"):
        if getattr(operation, f"{stage}_status") == "failed":
            operation.copied_error_code = (
                getattr(operation, f"{stage}_error_code") or f"{stage}_cleanup_failed"
            )
            operation.error_code = operation.copied_error_code


def _finalize_aggregate_status(operation: DocumentCleanupOperation) -> None:
    """Set the receipt/copied aggregate: failed if any owner failed, succeeded only if all did."""
    local_failed = (
        operation.raw_status == "failed"
        or operation.chat_status == "failed"
        or operation.memory_status == "failed"
        or operation.agent_status == "failed"
        or operation.materialization_status == "failed"
        or operation.brief_status == "failed"
    )
    if local_failed:
        operation.status = "failed"
        operation.copied_status = "failed"
    elif (
        operation.raw_status in {"not_present", "retained_shared", "succeeded"}
        and operation.chat_status == "succeeded"
        and operation.memory_status == "succeeded"
        and not operation.memory_cache_pending
        and operation.agent_status == "succeeded"
        and operation.materialization_status == "succeeded"
        and operation.brief_status == "succeeded"
    ):
        # Aggregate success only when every required owner stage genuinely completed.
        operation.status = "succeeded"
        operation.copied_status = "succeeded"
        operation.copied_error_code = None
        operation.error_code = None
    else:
        operation.status = "running"
        operation.copied_status = "running"


async def _advance_copied_stages(
    session: AsyncSession,
    operation: DocumentCleanupOperation,
    pre: _AgentPreflight | None,
    *,
    attempt: _Attempt,
    job_scope: InternalJobScope,
    flag: bool,
) -> _PageSchedule | None:
    """Run one page of every copied-owner stage in lock order; ``None`` means the preflight went stale.

    Order: Agent, Chat (or terminal scope failure), Memory, Materialization, saved briefs, then
    failure folding and the aggregate status. Each stage records itself in ``attempt.stage`` so a
    failure is recovered against the right stage, and contributes to the shared retry schedule.
    """
    operation.status = "running"
    operation.error_code = None
    operation.copied_status = "running"
    schedule = _PageSchedule()

    if not await _advance_agent_stage(
        session,
        operation,
        pre,
        attempt=attempt,
        schedule=schedule,
        job_scope=job_scope,
        flag=flag,
    ):
        return None

    if operation.evidence_scope_status != "captured":
        _fail_uncaptured_scope(operation)
        schedule.terminal_scope_failure = True
    elif operation.chat_status != "succeeded":
        await _advance_chat_stage(
            session, operation, attempt=attempt, schedule=schedule, job_scope=job_scope, flag=flag
        )

    await _advance_memory_stage(
        session, operation, attempt=attempt, schedule=schedule, job_scope=job_scope, flag=flag
    )

    # Materialization (Notifications/Automations) and saved-brief stages keep their own
    # cursors and run under the same privacy -> receipt locks; one page each per delivery.
    if not _copied_stage_terminal(
        operation.materialization_status, operation.materialization_error_code
    ):
        attempt.stage = "materialization"
        operation.materialization_error_code = None
        if not await _advance_materialization_cleanup(
            session, operation, job_scope=job_scope, flag=flag
        ):
            schedule.defer(_CONTINUATION_DELAY)
    if not _copied_stage_terminal(operation.brief_status, operation.brief_error_code):
        attempt.stage = "brief"
        operation.brief_error_code = None
        if not await _advance_brief_cleanup(session, operation, job_scope=job_scope, flag=flag):
            schedule.defer(_CONTINUATION_DELAY)

    _collect_stage_failures(operation, schedule)
    _finalize_aggregate_status(operation)

    if (
        operation.chat_status in {"succeeded", "failed"}
        or operation.memory_status in {"succeeded", "failed"}
        or operation.agent_status in {"succeeded", "failed"}
        or operation.materialization_status in {"succeeded", "failed"}
        or operation.brief_status in {"succeeded", "failed"}
    ):
        # Snapshotted after the aggregate is final so Source observes the settled state.
        _hint(attempt.hints, operation, "aggregate")

    if operation.memory_cache_pending:
        attempt.evict = True
        schedule.defer(_RETRY_DELAY)
    attempt.post_progress = _attempt_progress_snapshot(operation)
    return schedule


async def _advance_copied_cleanup(
    factory: async_sessionmaker[AsyncSession],
    flag: bool,
    identifier: UUID,
    attempt: _Attempt,
) -> bool:
    """Run one privacy -> receipt -> event locked page of every copied-owner stage; True if committed.

    Lock/commit order: admission, unlocked receipt hint, Agent lease preflight (before any shared
    lock), Memory consent (privacy) lock, receipt row lock, event claim lock, then the stage
    pages, one event settle (CAS on the claim stamp) and one replay commit. A moved receipt or a
    stale preflight rolls back and requeues; a lost claim/CAS rolls back and returns False.
    """
    async with factory() as session:
        admitted = await _admit_cleanup(session, identifier, flag)
        if admitted is None:
            await session.rollback()
            return False
        attempt.admitted = admitted
        job_scope = admitted.scope
        operation_hint = await session.scalar(_receipt_query(admitted))
        if operation_hint is None:
            await session.rollback()
            return False
        # Agent lease preparation must happen before the shared privacy or receipt lock.
        snapshot = _ReceiptSnapshot.of(operation_hint)
        pre = await _agent_preflight(
            session,
            operation_hint,
            attempt,
            operation_id=admitted.identity.operation_id,
            job_scope=job_scope,
            flag=flag,
        )
        if pre is not None and pre.result.blocked:
            # No privacy or receipt row was locked; schedule retry from the durable cursor.
            if await _settle(
                session, admitted, "pending", flag, next_attempt_at=datetime.now(UTC) + _RETRY_DELAY
            ):
                await _commit(session, admitted, flag)
            else:
                await session.rollback()
            return False

        # Shared Memory consent lock precedes URI, receipt, Chat and Memory owner locks.
        await lock_export_privacy_in_uow(
            session,
            scope=job_scope,
            multi_workspace_enabled=flag,
            access_fence=admitted.original,
        )
        operation = await session.scalar(_receipt_query(admitted, lock=True))
        if operation is None or not await _lock_claim(session, admitted, flag):
            await session.rollback()
            return False

        attempt.progress = _attempt_progress_snapshot(operation)
        if attempt.stage == "agent":
            attempt.stage = (
                "unknown"  # Preflight and lease checks passed; later stages set their own.
            )

        scope_stale = snapshot.differs_from(operation)
        if pre is not None:
            locked_agent_evidence = await documents.list_document_cleanup_evidence_scope(
                session,
                operation.id,
                after=pre.reference_after,
                limit=100,
                scope=job_scope,
                multi_workspace_enabled=flag,
            )
            scope_stale = scope_stale or locked_agent_evidence != pre.evidence
        if scope_stale:
            # The preflight is detached, so discard its advisory lease and repeat from the
            # receipt's current cursor before touching owner rows.
            await session.rollback()
            await _requeue(factory, flag, admitted, _CONTINUATION_DELAY)
            return False

        if _receipt_fully_settled(operation):
            # Duplicate deliveries must not reopen a settled receipt; an unsettled one re-settles below.
            if await _settle(session, admitted, "delivered", flag):
                await _commit(session, admitted, flag)
                attempt.progress = None
                return True
            await session.rollback()
            return False

        schedule = await _advance_copied_stages(
            session, operation, pre, attempt=attempt, job_scope=job_scope, flag=flag
        )
        if schedule is None:
            # Drop receipt/privacy locks and any successful stale lease before retrying.
            await session.rollback()
            await _requeue(factory, flag, admitted, _CONTINUATION_DELAY)
            return False

        if schedule.next_attempt_at is not None:
            settled = await _settle(
                session, admitted, "pending", flag, next_attempt_at=schedule.next_attempt_at
            )
        else:
            # The event is complete for Documents and Chat even while other copy owners remain pending.
            settled = await _settle(session, admitted, "delivered", flag)
        if not settled:
            await session.rollback()
            attempt.hints.clear()
            attempt.evict = False
            return False
        await _commit(session, admitted, flag)
    attempt.progress, attempt.stage = (
        None,
        "unknown",
    )  # Committed: later failures own no stage recovery.
    return True


async def _recover_attempt(
    factory: async_sessionmaker[AsyncSession],
    flag: bool,
    identifier: UUID,
    attempt: _Attempt,
    *,
    malformed: bool,
) -> None:
    """Write a stage error only when fresh admission and the receipt still equal this attempt's snapshot.

    Everything compared is a plain-scalar snapshot taken before the rollback; a missing snapshot
    means no mutation and the dispatcher's stale-claim recovery reclaims the work.
    """
    snapshot, progress, stage = attempt.admitted, attempt.progress, attempt.stage
    if snapshot is None or progress is None:
        return
    hints: list[documents.SourceCleanupWakeup] = []
    code = "cursor_reset" if malformed else "cleanup_failed"
    async with factory() as session:
        admitted = await _admit_cleanup(session, identifier, flag)
        if admitted is None or admitted != snapshot:
            await session.rollback()
            return
        await lock_export_privacy_in_uow(
            session,
            scope=admitted.scope,
            multi_workspace_enabled=flag,
            access_fence=admitted.original,
        )
        operation = await session.scalar(_receipt_query(admitted, lock=True))
        if (
            operation is None
            or _attempt_progress_snapshot(operation) != progress
            or not await _lock_claim(session, admitted, flag)
        ):
            await session.rollback()
            return
        changed = rewrite_recovered_stage(operation, stage, code, malformed, hints, _hint)
        if not changed or not await _settle(
            session,
            admitted,
            "pending",
            flag,
            next_attempt_at=datetime.now(UTC) + _RETRY_DELAY,
        ):
            await session.rollback()
            return
        await _commit(session, admitted, flag)
    await _publish_wakeups(factory, flag, hints)
