"""P2/P3 phase-review regressions."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from core.workspaces.schemas import WorkspaceContext

WS = uuid4()
OWNER = WorkspaceContext(user_id=7, workspace_id=WS, role="owner", membership_revision=1)
MEMBER = WorkspaceContext(user_id=8, workspace_id=WS, role="member", membership_revision=1)


@pytest.mark.asyncio
async def test_p2_1_member_cannot_list_tools():
    from modules.tools import routes

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        mcp_runtime=None, tool_registry=MagicMock(list_tools=MagicMock(return_value=[])),
    )))
    with pytest.raises(HTTPException) as exc:
        await routes.list_tools(request, MagicMock(), MEMBER)  # type: ignore[arg-type]
    assert exc.value.status_code == 403
    assert (await routes.list_tools(request, MagicMock(), OWNER))["items"] == []  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["record_draft_check", "persist_discovery"])
async def test_p2_2_mcp_runtime_commits_hold_fence(method):
    from modules.tools import mcp_runtime
    from modules.tools.mcp_runtime import McpRuntime

    runtime = object.__new__(McpRuntime)
    runtime.settings = SimpleNamespace(multi_workspace_enabled=False)  # type: ignore[assignment]
    session = MagicMock()
    session.commit = AsyncMock()

    @asynccontextmanager
    async def factory():
        yield session

    runtime.session_factory = factory  # type: ignore[assignment]
    fence = object()
    admit = AsyncMock(return_value=fence)
    commit = AsyncMock()
    repo_name = method
    with patch.object(mcp_runtime.mcp_repository, "admit", admit), \
            patch.object(mcp_runtime.mcp_repository, repo_name, AsyncMock(return_value="r")), \
            patch.object(mcp_runtime, "commit_with_replay", commit):
        if method == "record_draft_check":
            out = await runtime.record_draft_check(OWNER, uuid4(), 1, "ok")
        else:
            out = await runtime.persist_discovery(OWNER, uuid4(), MagicMock())
    assert out == "r"
    assert admit.await_args.kwargs["lock"] is True
    assert commit.await_args.kwargs["access_fence"] is fence
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_p2_3_memory_reconciler_advances_cursor_and_wraps():
    from modules.knowledge.documents import worker

    first, second = (uuid4(), uuid4()), (uuid4(),)
    listing = AsyncMock(side_effect=[first, second, (), first])
    sessions = MagicMock()
    sessions.return_value.__aenter__ = AsyncMock(return_value=MagicMock(rollback=AsyncMock()))
    sessions.return_value.__aexit__ = AsyncMock(return_value=False)
    ctx = {"session_factory": sessions, "settings": SimpleNamespace(multi_workspace_enabled=False)}
    worker._memory_reconcile_cursor = None
    with patch.object(worker.documents, "pending_document_memory_cleanup_ids", listing), \
            patch.object(worker, "_reopen_page", AsyncMock(return_value=0)):
        await worker.reconcile_document_memory_cleanup(ctx)
        await worker.reconcile_document_memory_cleanup(ctx)
        await worker.reconcile_document_memory_cleanup(ctx)  # empty page wraps to the start
    afters = [call.kwargs.get("after") for call in listing.await_args_list]
    assert afters == [None, first[-1], second[-1], None]
    worker._memory_reconcile_cursor = None


@pytest.mark.asyncio
async def test_p2_3_memory_listing_filters_after_cursor():
    from sqlalchemy.dialects import postgresql

    from modules.knowledge.documents import public

    seen = []

    class Capture:
        async def scalars(self, statement):
            seen.append(statement)
            return SimpleNamespace(all=list)

    await public.pending_document_memory_cleanup_ids(Capture(), after=uuid4(), limit=10)  # type: ignore[arg-type]
    assert "document_cleanup_operations.id > " in str(seen[0].compile(dialect=postgresql.dialect()))


def test_p3_1_receipt_query_pins_workspace_actor_and_lock():
    from sqlalchemy.dialects import postgresql

    from modules.knowledge.documents import worker

    identity = SimpleNamespace(operation_id=uuid4(), workspace_id=WS, actor_user_id=7)
    admitted = SimpleNamespace(identity=identity)
    plain = str(worker._receipt_query(admitted).compile(dialect=postgresql.dialect()))  # type: ignore[arg-type]
    locked = str(worker._receipt_query(admitted, lock=True).compile(dialect=postgresql.dialect()))  # type: ignore[arg-type]
    for sql in (plain, locked):
        assert "document_cleanup_operations.workspace_id = " in sql
        assert "document_cleanup_operations.actor_user_id = " in sql
    assert "FOR UPDATE" not in plain
    assert "FOR UPDATE" in locked


def test_p3_2_terminal_raw_uri_failure_is_not_a_retryable_stage():
    from modules.knowledge.documents import worker

    def op(status, code):
        return SimpleNamespace(raw_status=status, error_code=code)

    assert worker._raw_stage_done(op("failed", "raw_uri_unavailable"))
    assert not worker._raw_stage_done(op("failed", "file_cleanup_failed"))
    assert worker._raw_stage_done(op("succeeded", None))


@pytest.mark.asyncio
async def test_p3_7_automation_module_gate_runs_after_admission_lock():
    from modules.automations import execution

    order: list[str] = []
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(first=MagicMock(return_value=(WS, 7))))
    session.rollback = AsyncMock()

    @asynccontextmanager
    async def factory():
        yield session

    async def admit(*_a, **_k):
        order.append("admit")

    async def module(*_a, **_k):
        order.append("module")
        return False

    ctx = {"session_factory": factory, "settings": SimpleNamespace(multi_workspace_enabled=False)}
    owner = SimpleNamespace(user_id=7, membership_revision=1)
    with patch.object(execution.workspaces, "resolve_workspace_owner_context", AsyncMock(return_value=owner)), \
            patch.object(execution, "_admit", admit), \
            patch.object(execution.settings_public, "module_is_enabled", module):
        assert await execution.process_run(ctx, str(uuid4())) == "paused"
    assert order == ["admit", "module"]


@pytest.mark.asyncio
async def test_p3_7_connector_module_gate_runs_after_access_check():
    from modules.connectors import scheduler

    order: list[str] = []
    peek = SimpleNamespace(status="queued", source_id=uuid4())
    session = MagicMock()
    session.get = AsyncMock(return_value=peek)
    session.rollback = AsyncMock()

    async def access(*_a, **_k):
        order.append("access")

    async def module(*_a, **_k):
        order.append("module")
        return False

    with patch.object(scheduler, "_request_scope", lambda request: None), \
            patch.object(scheduler.connectors, "_connector_access", access), \
            patch("modules.settings.public.module_is_enabled", module):
        assert await scheduler.admit_collection_request(session, uuid4(), multi_workspace_enabled=False) is None
    assert order == ["access", "module"]


@pytest.mark.asyncio
async def test_p3_8_normalize_event_is_module_gated():
    from modules.ingestion import worker

    session = MagicMock()
    session.rollback = AsyncMock()

    @asynccontextmanager
    async def factory():
        yield session

    work = SimpleNamespace(scope=None)
    ctx = {"session_factory": factory, "settings": SimpleNamespace(multi_workspace_enabled=False)}
    with patch.object(worker, "_lock_worker_event", AsyncMock(return_value=work)), \
            patch("modules.settings.public.module_is_enabled", AsyncMock(return_value=False)), \
            patch.object(worker, "_capture_worker_claim", MagicMock(side_effect=AssertionError("ungated"))):
        await worker.process_normalize_event(ctx, str(uuid4()))
    session.rollback.assert_awaited()


@pytest.mark.asyncio
async def test_p2_5_source_coverage_cursor_survives_per_job_ctx_copy():
    from core.worker_cursors import STATE_KEY, read_cursor
    from modules.sources import worker

    ids = [uuid4(), uuid4()]
    session = MagicMock()
    session.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: ids))
    session.rollback = AsyncMock()

    @asynccontextmanager
    async def factory():
        yield session

    base = {"session_factory": factory, "settings": SimpleNamespace(multi_workspace_enabled=False), STATE_KEY: {}}
    with patch.object(worker.sources, "resolve_source_purge_job_scope", AsyncMock(return_value=None)):
        await worker.reconcile_source_coverage(dict(base))  # ARQ hands every job a fresh ctx copy
    assert await read_cursor(dict(base), worker._COVERAGE_CURSOR_KEY, worker._CURSOR_KEYS) == ids[-1]
