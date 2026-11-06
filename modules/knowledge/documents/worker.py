"""Documents-owned bounded cleanup consumer for raw files and copied-evidence owner stages."""

import logging
from typing import cast
from uuid import UUID

from redis.asyncio import Redis

from core.config import Settings
from core.worker_context import worker_multi_workspace_flag as _worker_flag
from core.worker_context import worker_session_factory
from modules.ingestion import (
    public as ingestion,  # noqa: F401  # shared module object; tests patch worker.ingestion
)
from modules.knowledge.documents import public as documents
from modules.knowledge.documents._worker.admit import (
    _admit_cleanup as _admit_cleanup,
)
from modules.knowledge.documents._worker.admit import (
    _Admitted as _Admitted,
)
from modules.knowledge.documents._worker.admit import (
    _agent_preflight as _agent_preflight,
)
from modules.knowledge.documents._worker.admit import (
    _AgentPreflight as _AgentPreflight,
)
from modules.knowledge.documents._worker.admit import (
    _Attempt as _Attempt,
)
from modules.knowledge.documents._worker.admit import (
    _commit as _commit,
)
from modules.knowledge.documents._worker.admit import (
    _cursor_state_agent as _cursor_state_agent,
)
from modules.knowledge.documents._worker.admit import (
    _hint as _hint,
)
from modules.knowledge.documents._worker.admit import (
    _lock_claim as _lock_claim,
)
from modules.knowledge.documents._worker.admit import (
    _PageSchedule as _PageSchedule,
)
from modules.knowledge.documents._worker.admit import (
    _publish_wakeups as _publish_wakeups,
)
from modules.knowledge.documents._worker.admit import (
    _receipt_query as _receipt_query,
)
from modules.knowledge.documents._worker.admit import (
    _ReceiptSnapshot as _ReceiptSnapshot,
)
from modules.knowledge.documents._worker.admit import (
    _requeue as _requeue,
)
from modules.knowledge.documents._worker.admit import (
    _settle as _settle,
)
from modules.knowledge.documents._worker.cleanup import (
    _advance_brief_cleanup as _advance_brief_cleanup,
)
from modules.knowledge.documents._worker.cleanup import (
    _advance_materialization_cleanup as _advance_materialization_cleanup,
)
from modules.knowledge.documents._worker.cleanup import (
    _advance_memory_cleanup as _advance_memory_cleanup,
)
from modules.knowledge.documents._worker.cleanup import (
    _advance_raw_document_cleanup as _advance_raw_document_cleanup,
)
from modules.knowledge.documents._worker.cleanup import (
    _evict_memory_cache_after_commit as _evict_memory_cache_after_commit,
)
from modules.knowledge.documents._worker.cleanup import (
    _reopen_cleanup_event as _reopen_cleanup_event,
)
from modules.knowledge.documents._worker.cleanup import (
    _reopen_page as _reopen_page,
)
from modules.knowledge.documents._worker.progress import (
    _CONTINUATION_DELAY as _CONTINUATION_DELAY,
)
from modules.knowledge.documents._worker.progress import (
    _COPIED_STAGE_TERMINAL_CODES as _COPIED_STAGE_TERMINAL_CODES,
)
from modules.knowledge.documents._worker.progress import (
    _MATERIALIZATION_PHASES as _MATERIALIZATION_PHASES,
)
from modules.knowledge.documents._worker.progress import (
    _RAW_URI_UNAVAILABLE as _RAW_URI_UNAVAILABLE,
)
from modules.knowledge.documents._worker.progress import (
    _RETRY_DELAY as _RETRY_DELAY,
)
from modules.knowledge.documents._worker.progress import (
    _attempt_progress_snapshot as _attempt_progress_snapshot,
)
from modules.knowledge.documents._worker.progress import (
    _copied_stage_terminal as _copied_stage_terminal,
)
from modules.knowledge.documents._worker.progress import (
    _memory_terminal as _memory_terminal,
)
from modules.knowledge.documents._worker.progress import (
    _optional_uuid as _optional_uuid,
)
from modules.knowledge.documents._worker.progress import (
    _raw_stage_done as _raw_stage_done,
)
from modules.knowledge.documents._worker.progress import (
    _receipt_fully_settled as _receipt_fully_settled,
)
from modules.knowledge.documents._worker.progress import (
    _source_cleanup_progress_key as _source_cleanup_progress_key,
)
from modules.knowledge.documents._worker.stages import (
    _advance_agent_stage as _advance_agent_stage,
)
from modules.knowledge.documents._worker.stages import (
    _advance_chat_stage as _advance_chat_stage,
)
from modules.knowledge.documents._worker.stages import (
    _advance_copied_cleanup as _advance_copied_cleanup,
)
from modules.knowledge.documents._worker.stages import (
    _advance_copied_stages as _advance_copied_stages,
)
from modules.knowledge.documents._worker.stages import (
    _advance_memory_stage as _advance_memory_stage,
)
from modules.knowledge.documents._worker.stages import (
    _collect_stage_failures as _collect_stage_failures,
)
from modules.knowledge.documents._worker.stages import (
    _fail_uncaptured_scope as _fail_uncaptured_scope,
)
from modules.knowledge.documents._worker.stages import (
    _finalize_aggregate_status as _finalize_aggregate_status,
)
from modules.knowledge.documents._worker.stages import (
    _recover_attempt as _recover_attempt,
)
from modules.sources import (
    public as sources,  # noqa: F401  # shared module object; tests patch worker.sources
)

logger = logging.getLogger(__name__)
_MEMORY_RECONCILE_LIMIT = 100
_memory_reconcile_cursor: UUID | None = None
_agent_reconcile_cursor: UUID | None = None
_copied_stage_reconcile_cursor: UUID | None = None


async def reconcile_document_memory_cleanup(ctx: dict[str, object]) -> int:
    """Requeue at most 100 captured historical receipts whose Chat stage already succeeded.

    Identity-only discovery excludes legacy NULL-epoch receipts; each ID is admitted and
    published in its own session. Like all cleanup work it is not module-gated.
    """
    factory = worker_session_factory(ctx)
    global _memory_reconcile_cursor
    flag = _worker_flag(ctx)
    async with factory() as session:
        operation_ids = await documents.pending_document_memory_cleanup_ids(
            session,
            after=_memory_reconcile_cursor,
            limit=_MEMORY_RECONCILE_LIMIT,
        )
        if not operation_ids and _memory_reconcile_cursor is not None:
            _memory_reconcile_cursor = None
            operation_ids = await documents.pending_document_memory_cleanup_ids(
                session,
                limit=_MEMORY_RECONCILE_LIMIT,
            )
        if operation_ids:
            _memory_reconcile_cursor = operation_ids[-1]
        await session.rollback()
    return await _reopen_page(factory, flag, operation_ids)


async def reconcile_document_agent_cleanup(ctx: dict[str, object]) -> int:
    """Fairly scan one bounded UUID page of receipts with a nonterminal Agent stage."""
    global _agent_reconcile_cursor
    factory = worker_session_factory(ctx)
    flag = _worker_flag(ctx)
    async with factory() as session:
        operation_ids = await documents.pending_document_agent_cleanup_ids(
            session,
            after=_agent_reconcile_cursor,
            limit=100,
        )
        if not operation_ids and _agent_reconcile_cursor is not None:
            _agent_reconcile_cursor = None
            operation_ids = await documents.pending_document_agent_cleanup_ids(session, limit=100)
        if operation_ids:
            _agent_reconcile_cursor = operation_ids[-1]
        await session.rollback()
    return await _reopen_page(factory, flag, operation_ids)


async def reconcile_document_copied_stage_cleanup(ctx: dict[str, object]) -> int:
    """Fairly scan one bounded UUID page of receipts with a nonterminal materialization/brief stage.

    Reopens a delivered or failed deterministic cleanup event idempotently (including events the
    dispatcher quarantined before this envelope was accepted) and preserves the schedule of a
    pending one. Terminal unavailable stages are excluded by the Documents query.
    """
    global _copied_stage_reconcile_cursor
    factory = worker_session_factory(ctx)
    flag = _worker_flag(ctx)
    async with factory() as session:
        operation_ids = await documents.pending_document_copied_stage_cleanup_ids(
            session,
            after=_copied_stage_reconcile_cursor,
            limit=100,
        )
        if not operation_ids and _copied_stage_reconcile_cursor is not None:
            _copied_stage_reconcile_cursor = None
            operation_ids = await documents.pending_document_copied_stage_cleanup_ids(
                session, limit=100
            )
        if operation_ids:
            _copied_stage_reconcile_cursor = operation_ids[-1]
        await session.rollback()
    return await _reopen_page(factory, flag, operation_ids)


async def process_document_cleanup(ctx: dict[str, object], event_id: str) -> None:
    """Advance raw and copied-evidence cleanup stages after canonical deletion commits.

    Cleanup is intentionally NOT module-gated: a disabled module must still finish deleting data,
    so ``module_is_enabled`` is never consulted here or in the reconcilers.

    Every transaction first admits the claimed event under the receipt's ORIGINAL retained
    authority (never a rebased actor, membership or configuration); missing, legacy or stale
    authority performs no mutation. Raw URI removal commits in its own admission -> privacy ->
    URI -> receipt -> event transaction before copied-owner work. For Agent cleanup, detached
    scope discovery and the exact nonblocking run lease are prepared before the shared privacy and
    receipt locks. Stage cursors commit with owner changes; Source wakeups publish only after
    commit from fresh sessions; Memory cache eviction follows commit under a durable retry marker
    and re-admission. Error recovery compares plain-scalar snapshots to durable state before
    writing; without a snapshot the dispatcher reclaims stale queued work.
    """
    factory = worker_session_factory(ctx)
    settings = cast(Settings, ctx["settings"])
    redis = cast(Redis, ctx["redis"])
    flag = _worker_flag(ctx)
    identifier = UUID(event_id)
    attempt = _Attempt()

    try:
        if not await _advance_raw_document_cleanup(factory, settings, flag, identifier):
            return
        if await _advance_copied_cleanup(factory, flag, identifier, attempt):
            await _publish_wakeups(factory, flag, attempt.hints)
            if attempt.evict:
                await _evict_memory_cache_after_commit(factory, redis, flag, attempt)
    except ValueError:
        # A malformed local continuation restarts idempotently from the first exact identity page.
        await _recover_attempt(factory, flag, identifier, attempt, malformed=True)
    except Exception as exc:  # noqa: BLE001  # boundary: failure logged, caller degrades safely
        logger.warning("Document copied-evidence cleanup deferred (%s)", type(exc).__name__)
        await _recover_attempt(factory, flag, identifier, attempt, malformed=False)
