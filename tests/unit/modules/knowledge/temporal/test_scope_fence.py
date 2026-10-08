"""Workspace scope, original-fence and cursor contracts for Temporal (no DB)."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from core.workspaces.schemas import AccessFence, InternalJobScope, WorkspaceContext
from modules.knowledge.temporal import public, routes, worker
from modules.knowledge.temporal.schemas import ReconcileRequest
from tests.unit.modules.knowledge.temporal.conftest import FENCE, JOB, MEMBER, OWNER, WORKSPACE_ID

KW = {"multi_workspace_enabled": False}


def _session() -> MagicMock:
    session = MagicMock()
    for name in ("execute", "scalar", "scalars", "get", "flush", "commit", "rollback"):
        setattr(session, name, AsyncMock(return_value=None))
    return session


def _factory(session: MagicMock) -> MagicMock:
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


def _adm() -> worker._Admission:
    return worker._Admission(JOB, False, FENCE)


# ---- a member with no share gets no content ---------------------------------------------------

@pytest.mark.parametrize("call", [
    lambda s: public.mapping_statuses(s, [uuid4()], scope=MEMBER, **KW),
    lambda s: public.find_changes(s, scope=MEMBER, **KW),
    lambda s: public.reconcile_status(s, uuid4(), scope=MEMBER, **KW),
    lambda s: public.request_reconcile(s, ReconcileRequest(source_id=uuid4()), scope=MEMBER, **KW),
    lambda s: public.select_search_partitions(s, [uuid4()], scope=MEMBER, **KW),
    lambda s: public.reconcile_slice(s, uuid4(), scope=MEMBER, **KW),
])
async def test_member_is_denied_before_any_query(call) -> None:
    session = _session()
    with patch("modules.knowledge.temporal.public.workspaces.read_access_fence", AsyncMock(return_value=FENCE)), \
            pytest.raises(HTTPException) as denied:
        await call(session)
    assert denied.value.status_code == 403
    for name in ("execute", "scalar", "scalars", "get"):
        getattr(session, name).assert_not_awaited()


async def test_find_changes_query_is_workspace_scoped(admitted_owner) -> None:
    session = _session()
    session.scalars = AsyncMock(return_value=MagicMock(all=list))
    await public.find_changes(session, scope=OWNER, **KW)
    sql = str(session.scalars.await_args.args[0].compile(dialect=postgresql.dialect()))
    assert "workspace_id" in sql


# ---- cursors bind workspace, actor and revision ------------------------------------------------

@pytest.fixture
def owner_fence():
    """Admit OWNER with a fence built from OWNER itself (independent of conftest module identity)."""
    fence = AccessFence(workspace_id=OWNER.workspace_id, user_id=OWNER.user_id,
                        membership_revision=OWNER.membership_revision, configuration_revision=1)
    with patch("modules.knowledge.temporal.public.workspaces.read_access_fence", AsyncMock(return_value=fence)):
        yield


def _cursor_for(scope: WorkspaceContext | InternalJobScope, configuration_revision: int = 1) -> str:
    actor = scope.user_id if isinstance(scope, WorkspaceContext) else scope.actor_user_id
    fingerprint = public.digest([None, None, None, None, str(scope.workspace_id), actor,
                                 scope.membership_revision, configuration_revision])
    return public._cursor(7, fingerprint)


async def test_cursor_round_trips_for_the_same_workspace_actor_and_revision(owner_fence) -> None:
    session = _session()
    session.scalars = AsyncMock(return_value=MagicMock(all=list))
    page = await public.find_changes(session, scope=OWNER, cursor=_cursor_for(OWNER), **KW)
    assert page.items == []


@pytest.mark.parametrize("other", [
    WorkspaceContext(user_id=1, workspace_id=uuid4(), role="owner", membership_revision=1),
    WorkspaceContext(user_id=9, workspace_id=WORKSPACE_ID, role="owner", membership_revision=1),
    WorkspaceContext(user_id=1, workspace_id=WORKSPACE_ID, role="owner", membership_revision=2),
])
async def test_cursor_from_another_workspace_actor_or_revision_is_rejected(other, admitted_owner) -> None:
    session = _session()
    session.scalars = AsyncMock(return_value=MagicMock(all=list))
    with pytest.raises(ValueError, match="Invalid temporal cursor"):
        await public.find_changes(session, scope=OWNER, cursor=_cursor_for(other), **KW)
    session.scalars.assert_not_awaited()


async def test_cursor_from_another_configuration_revision_is_rejected(owner_fence) -> None:
    session = _session()
    session.scalars = AsyncMock(return_value=MagicMock(all=list))
    with pytest.raises(ValueError, match="Invalid temporal cursor"):
        await public.find_changes(session, scope=OWNER, cursor=_cursor_for(OWNER, configuration_revision=2), **KW)
    session.scalars.assert_not_awaited()


# ---- the original fence is compared before every effect ----------------------------------------

async def test_changed_original_fence_is_rejected_before_the_effect() -> None:
    drifted = AccessFence(workspace_id=WORKSPACE_ID, user_id=1, membership_revision=1, configuration_revision=2)
    session = _session()
    with patch.object(worker.workspaces, "read_access_fence", AsyncMock(return_value=drifted)), \
            pytest.raises(HTTPException) as stale:
        await worker._finish(_factory(session), _adm(), uuid4(), uuid4(), "succeeded", None)
    assert stale.value.status_code == 409
    session.commit.assert_not_awaited()
    session.scalar.assert_not_awaited()


async def test_commit_carries_the_original_fence() -> None:
    session = _session()
    with patch.object(worker, "commit_with_replay", AsyncMock()) as commit:
        await worker._commit(session, _adm())
    assert commit.await_args.kwargs["access_fence"] == FENCE
    assert commit.await_args.kwargs["scope"] == JOB


async def test_foreign_workspace_root_is_absent() -> None:
    session = _session()
    await worker._get(session, worker.GraphOperation, uuid4(), _adm())
    sql = str(session.scalar.await_args.args[0].compile(dialect=postgresql.dialect()))
    assert "temporal_operations.workspace_id" in sql


# ---- fence-first lock order in the worker ------------------------------------------------------

async def test_process_admits_through_the_fence_before_slot_or_claim() -> None:
    order: list[str] = []

    async def admit(*_args, **_kwargs):
        order.append("fence")
        return _adm()

    async def claim(*_args, **_kwargs):
        order.append("claim")

    @asynccontextmanager
    async def slot(*_args, **_kwargs):
        order.append("slot")
        yield

    graph = MagicMock(close=AsyncMock())
    ctx = {"session_factory": _factory(_session()), "settings": SimpleNamespace()}
    with patch.object(worker, "_admit_job", admit), patch.object(worker, "_claim", claim), \
            patch.object(worker, "heavy_job_slot", slot), \
            patch.object(worker, "TemporalGraph", MagicMock(return_value=graph)), \
            patch.object(worker.GraphConfiguration, "from_settings", MagicMock()):
        await worker.process_graph_operation(ctx, str(uuid4()))
    assert order == ["fence", "slot", "claim"]


async def test_denied_job_takes_no_slot_claim_or_lock() -> None:
    claim, slot = AsyncMock(), MagicMock()
    ctx = {"session_factory": _factory(_session()), "settings": SimpleNamespace()}
    with patch.object(worker, "_admit_job", AsyncMock(return_value=None)), \
            patch.object(worker, "_claim", claim), patch.object(worker, "heavy_job_slot", slot):
        await worker.process_graph_operation(ctx, str(uuid4()))
    claim.assert_not_awaited()
    slot.assert_not_called()


async def test_admit_workspace_locks_the_fence_before_the_module_gate() -> None:
    order: list[str] = []
    owner = SimpleNamespace(user_id=1, membership_revision=1)

    async def resolve(*_a, **_k):
        order.append("resolve")
        return owner

    async def authorize(*_a, **_k):
        order.append("fence")
        return FENCE

    async def gate(*_a, **_k):
        order.append("gate")
        return True

    settings = SimpleNamespace(multi_workspace_enabled=False)
    with patch.object(worker.workspaces, "resolve_workspace_owner_context", resolve), \
            patch.object(worker.workspaces, "authorize_internal_job", authorize), \
            patch.object(worker.settings_public, "module_is_enabled", gate):
        adm = await worker.admit_workspace(_factory(_session()), settings, WORKSPACE_ID)  # type: ignore[arg-type]
    assert order == ["resolve", "fence", "gate"]
    assert adm is not None and adm.fence == FENCE


@pytest.mark.parametrize("status", [401, 403, 404, 409])
async def test_denied_admission_skips_the_workspace(status: int) -> None:
    owner = SimpleNamespace(user_id=1, membership_revision=1)
    settings = SimpleNamespace(multi_workspace_enabled=False)
    gate = AsyncMock(return_value=True)
    with patch.object(worker.workspaces, "resolve_workspace_owner_context", AsyncMock(return_value=owner)), \
            patch.object(worker.workspaces, "authorize_internal_job",
                         AsyncMock(side_effect=HTTPException(status_code=status))), \
            patch.object(worker.settings_public, "module_is_enabled", gate):
        assert await worker.admit_workspace(_factory(_session()), settings, WORKSPACE_ID) is None  # type: ignore[arg-type]
    gate.assert_not_awaited()


async def test_disabled_module_skips_the_workspace() -> None:
    owner = SimpleNamespace(user_id=1, membership_revision=1)
    settings = SimpleNamespace(multi_workspace_enabled=False)
    with patch.object(worker.workspaces, "resolve_workspace_owner_context", AsyncMock(return_value=owner)), \
            patch.object(worker.workspaces, "authorize_internal_job", AsyncMock(return_value=FENCE)), \
            patch.object(worker.settings_public, "module_is_enabled", AsyncMock(return_value=False)):
        assert await worker.admit_workspace(_factory(_session()), settings, WORKSPACE_ID) is None  # type: ignore[arg-type]


async def test_recovery_admits_each_workspace_separately_and_skips_denied() -> None:
    first, second = uuid4(), uuid4()
    seen: list[object] = []

    async def admit(_factory_arg, _settings, workspace_id):
        seen.append(workspace_id)
        return None if workspace_id == first else _adm()

    session = _session()
    session.scalars = AsyncMock(return_value=MagicMock(all=list))
    redis = MagicMock(enqueue_job=AsyncMock())
    ctx = {"session_factory": _factory(session), "settings": SimpleNamespace(), "redis": redis}
    with patch.object(worker, "_recovery_workspaces", AsyncMock(return_value=[first, second])), \
            patch.object(worker, "admit_workspace", admit), \
            patch.object(worker.workspaces, "read_access_fence", AsyncMock(return_value=FENCE)), \
            patch.object(worker, "commit_with_replay", AsyncMock()) as commit:
        assert await worker.recover_graph_work(ctx) == 0
    assert seen == [first, second]
    assert commit.await_count == 1  # only the admitted workspace opens a mutation transaction


# ---- cancellation coinciding with fence drift stays a cancellation -----------------------------

async def test_cancellation_with_fence_drift_reraises_the_cancellation() -> None:
    @asynccontextmanager
    async def slot(*_args, **_kwargs):
        yield

    graph = MagicMock(close=AsyncMock(), initialize=AsyncMock(side_effect=asyncio.CancelledError()))
    ctx = {"session_factory": _factory(_session()), "settings": SimpleNamespace()}
    with patch.object(worker, "_admit_job", AsyncMock(return_value=_adm())), \
            patch.object(worker, "_claim", AsyncMock(return_value=uuid4())), \
            patch.object(worker, "_admit", AsyncMock()), \
            patch.object(worker, "_finish", AsyncMock(side_effect=HTTPException(status_code=409))), \
            patch.object(worker, "heavy_job_slot", slot), \
            patch.object(worker, "TemporalGraph", MagicMock(return_value=graph)), \
            patch.object(worker.GraphConfiguration, "from_settings", MagicMock()), \
            pytest.raises(asyncio.CancelledError):
        await worker.process_graph_operation(ctx, str(uuid4()))


async def test_plain_fence_drift_is_still_a_silent_skip() -> None:
    @asynccontextmanager
    async def slot(*_args, **_kwargs):
        yield

    graph = MagicMock(close=AsyncMock())
    ctx = {"session_factory": _factory(_session()), "settings": SimpleNamespace()}
    with patch.object(worker, "_admit_job", AsyncMock(return_value=_adm())), \
            patch.object(worker, "_claim", AsyncMock(side_effect=HTTPException(status_code=409))), \
            patch.object(worker, "heavy_job_slot", slot), \
            patch.object(worker, "TemporalGraph", MagicMock(return_value=graph)), \
            patch.object(worker.GraphConfiguration, "from_settings", MagicMock()):
        assert await worker.process_graph_operation(ctx, str(uuid4())) is None


# ---- reconcile route: owner check, then access-fence lock, then fenced commit -------------------

def _request() -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        settings=SimpleNamespace(multi_workspace_enabled=False))))


async def test_reconcile_route_locks_fence_then_commits_with_it() -> None:
    order: list[str] = []
    run_id = uuid4()

    async def lock(*_a, **_k):
        order.append("lock")
        return FENCE

    async def request_reconcile(*_a, **_k):
        order.append("request")
        return run_id

    async def commit(*_a, **kwargs):
        order.append("commit")
        assert kwargs["access_fence"] == FENCE

    with patch.object(routes, "lock_access_fence", lock), \
            patch.object(routes, "authenticated_session_ref", MagicMock(return_value="ref")), \
            patch.object(routes.public, "request_reconcile", request_reconcile), \
            patch.object(routes, "commit_with_replay", commit):
        result = await routes.reconcile(
            ReconcileRequest(source_id=uuid4()), MagicMock(), None, OWNER, _request(), MagicMock(headers={}))
    assert order == ["lock", "request", "commit"]
    assert result == {"run_id": run_id}


async def test_reconcile_route_denies_member_before_taking_the_lock() -> None:
    lock = AsyncMock()
    with patch.object(routes, "lock_access_fence", lock), pytest.raises(HTTPException) as denied:
        await routes.reconcile(
            ReconcileRequest(source_id=uuid4()), MagicMock(), None, MEMBER, _request(), MagicMock(headers={}))
    assert denied.value.status_code == 403
    lock.assert_not_awaited()


# ---- recovery workspace paging: keyset and wrap-around -----------------------------------------

def _recovery_session(*pages: list) -> MagicMock:
    session = _session()
    session.scalars = AsyncMock(side_effect=[MagicMock(all=lambda page=page: page) for page in pages])
    return session


async def test_recovery_workspaces_pages_after_the_stored_cursor() -> None:
    after, ids = uuid4(), [uuid4(), uuid4()]
    session = _recovery_session(ids)
    redis = MagicMock(get=AsyncMock(return_value=str(after).encode()), set=AsyncMock(), delete=AsyncMock())
    assert await worker._recovery_workspaces(redis, _factory(session)) == ids  # type: ignore[arg-type]
    sql = str(session.scalars.await_args.args[0].compile(dialect=postgresql.dialect()))
    assert "workspace_id >" in sql
    assert redis.set.await_args.args[:2] == (worker.RECOVERY_CURSOR_KEY, str(ids[-1]))


async def test_recovery_workspaces_wraps_to_the_start_when_the_tail_is_empty() -> None:
    first = uuid4()
    session = _recovery_session([], [first])
    redis = MagicMock(get=AsyncMock(return_value=str(uuid4())), set=AsyncMock(), delete=AsyncMock())
    assert await worker._recovery_workspaces(redis, _factory(session)) == [first]  # type: ignore[arg-type]
    assert session.scalars.await_count == 2
    sql = str(session.scalars.await_args.args[0].compile(dialect=postgresql.dialect()))
    assert "workspace_id >" not in sql


async def test_recovery_workspaces_clears_the_cursor_when_nothing_is_due() -> None:
    session = _recovery_session([], [])
    redis = MagicMock(get=AsyncMock(return_value=str(uuid4())), set=AsyncMock(), delete=AsyncMock())
    assert await worker._recovery_workspaces(redis, _factory(session)) == []  # type: ignore[arg-type]
    redis.delete.assert_awaited_once_with(worker.RECOVERY_CURSOR_KEY)
