"""Unit tests for Documents cleanup aggregates, retained-receipt fallback and Memory cache retry."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from modules.knowledge.documents import public, worker


class _Session:
    """Capture the one statement and return a canned row/scalar."""

    def __init__(self, row=None, scalar=None) -> None:
        self.row, self.scalar_value, self.statement = row, scalar, None

    async def execute(self, statement):
        self.statement = statement
        return SimpleNamespace(one=lambda: self.row)

    async def scalar(self, statement):
        self.statement = statement
        return self.scalar_value


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


async def _progress(row, *, capture_recorded=True):
    return await public.source_cleanup_progress(
        _Session(row=row), uuid4(), source_id=uuid4(), capture_recorded=capture_recorded,
    )


# Row order: child, historical, pending, failed, hist_pending, hist_failed, raw, chat, memory,
# agent, materialization, brief, linked_active.
_CLEAN = (3, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)


async def test_aggregate_requires_every_stage_of_every_receipt_to_succeed() -> None:
    assert (await _progress(_CLEAN)).all_required_complete is True
    for index in (2, 3, 4, 5):  # linked/historical pending and failed
        row = list(_CLEAN)
        row[index] = 1
        assert (await _progress(tuple(row))).all_required_complete is False, index


async def test_aggregate_incomplete_until_capture_recorded_and_names_owners() -> None:
    progress = await _progress(_CLEAN, capture_recorded=False)
    assert progress.all_required_complete is False
    assert progress.pending_owner_codes == ("documents",)
    waiting = (1, 0, 1, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1)
    progress = await _progress(waiting)
    assert progress.pending_owner_codes == (
        "raw", "chat", "memory", "agents", "notifications", "automations", "dashboard",
    )
    assert progress.active_copy_work is True


async def test_aggregate_sql_checks_every_stage_and_the_cache_obligation() -> None:
    session = _Session(row=_CLEAN)
    await public.source_cleanup_progress(session, uuid4(), source_id=uuid4(), capture_recorded=True)
    sql = _sql(session.statement)
    for column in (
        "raw_status", "chat_status", "memory_status", "agent_status", "materialization_status",
        "brief_status", "copied_status", "memory_cache_pending", "evidence_scope_status",
    ):
        assert column in sql, column


async def test_retained_receipt_fallback_resolves_version_document_from_cleanup_evidence() -> None:
    document_id, version_id = uuid4(), uuid4()
    session = _Session(scalar=document_id)
    assert await public.cleanup_evidence_version_document(session, version_id) == document_id
    sql = _sql(session.statement)
    assert "document_cleanup_evidence_reference" in sql and "document_cleanup_operation" in sql
    assert "reference_kind" in sql and "FOR UPDATE" not in sql  # immutable receipt read, no lock
    params = session.statement.compile().params.values()
    assert version_id in params and "version" in params
    assert await public.cleanup_evidence_version_document(_Session(scalar=None), version_id) is None


def _factory(operation):
    session = MagicMock()
    session.scalar = AsyncMock(return_value=operation)
    session.commit, session.rollback = AsyncMock(), AsyncMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=None)
    return MagicMock(return_value=context), session


async def test_cache_eviction_failure_keeps_marker_for_retry() -> None:
    factory, session = _factory(SimpleNamespace(memory_cache_pending=True))
    with patch.object(worker, "invalidate_memory_cache", AsyncMock(side_effect=RuntimeError("redis"))):
        await worker._evict_memory_cache_after_commit(factory, MagicMock(), uuid4(), uuid4())
    factory.assert_not_called()  # marker untouched, nothing committed
    session.commit.assert_not_called()


async def test_cache_eviction_success_clears_marker_wakes_source_and_reschedules_event() -> None:
    operation = SimpleNamespace(memory_cache_pending=True)
    factory, session = _factory(operation)
    event_id = uuid4()
    with patch.object(worker, "invalidate_memory_cache", AsyncMock()), \
            patch.object(worker, "lock_export_privacy", AsyncMock()), \
            patch.object(worker, "_source_cleanup_progress_key", return_value="memory-cache"), \
            patch.object(worker.documents, "publish_source_cleanup_wakeup", AsyncMock()) as wake, \
            patch.object(worker.ingestion, "set_event_delivery", AsyncMock()) as deliver:
        await worker._evict_memory_cache_after_commit(factory, MagicMock(), uuid4(), event_id)
    assert operation.memory_cache_pending is False
    wake.assert_awaited_once()
    # Never delivered here: the main path must re-settle the aggregate first.
    assert deliver.await_args.args[1:3] == (event_id, "pending")
    assert deliver.await_args.kwargs["next_attempt_at"] is not None
    session.commit.assert_awaited_once()


async def test_cache_eviction_already_cleared_is_a_noop_rollback() -> None:
    factory, session = _factory(SimpleNamespace(memory_cache_pending=False))
    with patch.object(worker, "invalidate_memory_cache", AsyncMock()), \
            patch.object(worker, "lock_export_privacy", AsyncMock()), \
            patch.object(worker.ingestion, "set_event_delivery", AsyncMock()) as deliver:
        await worker._evict_memory_cache_after_commit(factory, MagicMock(), uuid4(), uuid4())
    deliver.assert_not_awaited()
    session.rollback.assert_awaited_once()
