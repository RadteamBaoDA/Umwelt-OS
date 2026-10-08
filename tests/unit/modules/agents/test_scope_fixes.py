"""Fix round 1 contracts: browser epoch checks, TypeError propagation, reconcile starvation, route order."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from modules.agents import access, routes, worker
from modules.agents import public as agents
from modules.agents.harness import HarnessContext
from tests.unit.modules.agents.test_scope import CTX, FENCE, JOB, OWNER, WS, _factory

MOVED = type(FENCE)(workspace_id=WS, user_id=7, membership_revision=2, configuration_revision=6)


def _live_run(**overrides: object) -> SimpleNamespace:
    values = {
        "workspace_id": WS, "owner_id": 7, "membership_revision": 2, "configuration_revision": 5,
        "status": "running", "cancel_requested": False, "evidence_revoked": False,
        "claim_generation": 1, "auth_session_hash": "h", "profile_revision_hash": "p",
        "profile_snapshot": {"id": "x"},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _auth(**overrides: object) -> agents.BrowserRunAuthorization:
    values: dict[str, object] = {
        "run_id": uuid4(), "owner_id": 7, "auth_session_hash": "h", "claim_generation": 1,
        "profile_id": "x", "profile_revision_hash": "p",
    }
    values.update(overrides)
    return SimpleNamespace(**values)  # type: ignore[return-value]


def _harness() -> HarnessContext:
    return HarnessContext(
        uuid4(), JOB, FENCE, 1, MagicMock(), MagicMock(), SimpleNamespace(multi_workspace_enabled=False),
        MagicMock(), MagicMock(), MagicMock(), 1, 0.0, frozenset(), {},
    )


# ----------------------------------------------------------------------------- browser epoch


async def test_reserve_refuses_a_changed_epoch() -> None:
    session = MagicMock()
    session.scalar = AsyncMock(return_value=_live_run())
    with patch.object(access.workspaces, "read_access_fence", AsyncMock(return_value=MOVED)), \
            pytest.raises(PermissionError, match="authorization changed"):
        await agents.reserve_browser_run_budget_in_uow(
            session, uuid4(), 1, 1, "d" * 64, 1, **CTX,
        )
    assert session.scalar.await_count == 1


async def test_reserve_refuses_a_foreign_run() -> None:
    session = MagicMock()
    session.scalar = AsyncMock(return_value=None)
    with patch.object(access.workspaces, "read_access_fence", AsyncMock(return_value=FENCE)), \
            pytest.raises(PermissionError, match="no longer current"):
        await agents.reserve_browser_run_budget_in_uow(session, uuid4(), 1, 1, "d" * 64, 1, **CTX)
    where = str(session.scalar.await_args.args[0].compile()).split("WHERE", 1)[1]
    assert "workspace_id" in where and "owner_id" in where


async def test_revalidate_refuses_a_changed_epoch_before_the_profile_lookup() -> None:
    session = MagicMock()
    session.scalar = AsyncMock(return_value=_live_run())
    with patch.object(access.workspaces, "read_access_fence", AsyncMock(return_value=MOVED)):
        assert await agents.revalidate_browser_run_authority(session, _auth(), **CTX) is False
    assert session.scalar.await_count == 1


async def test_revalidate_owner_mismatch_is_false_before_any_select() -> None:
    session = MagicMock()
    session.scalar = AsyncMock()
    with patch.object(access.workspaces, "read_access_fence", AsyncMock(return_value=FENCE)):
        assert await agents.revalidate_browser_run_authority(session, _auth(owner_id=8), **CTX) is False
    session.scalar.assert_not_awaited()


@pytest.mark.parametrize("status", [401, 403, 404, 409])
async def test_revalidate_returns_false_when_admission_is_denied(status: int) -> None:
    with patch.object(agents, "admit", AsyncMock(side_effect=HTTPException(status_code=status))):
        assert await agents.revalidate_browser_run_authority(MagicMock(), _auth(), **CTX) is False


async def test_revalidate_reraises_unexpected_status_and_bad_flag() -> None:
    with patch.object(agents, "admit", AsyncMock(side_effect=HTTPException(status_code=500))), \
            pytest.raises(HTTPException):
        await agents.revalidate_browser_run_authority(MagicMock(), _auth(), **CTX)
    with pytest.raises(TypeError):
        await agents.revalidate_browser_run_authority(
            MagicMock(), _auth(), scope=OWNER, multi_workspace_enabled=1,  # type: ignore[arg-type]
        )


# ----------------------------------------------------------------------------- TypeError propagation


async def test_type_error_from_revalidation_propagates_in_agents_public() -> None:
    item = SimpleNamespace(tool_name="webhook.send", source_fences={})
    with patch.object(agents, "_restore_fences", MagicMock(return_value={})), \
            patch.object(agents, "ToolExecutionPrincipal", MagicMock()), \
            patch.object(agents, "revalidate_native_output_fences", AsyncMock(side_effect=TypeError("bad"))), \
            pytest.raises(TypeError):
        await agents._approval_fences_current(MagicMock(), item, **CTX)  # type: ignore[arg-type]
    row = SimpleNamespace(source_fences={"x": 1}, answer="a")
    with patch.object(agents, "_restore_fences", MagicMock(return_value={})), \
            patch.object(agents, "_read", MagicMock(return_value=SimpleNamespace(answer="a"))), \
            patch.object(agents, "_result_principal", MagicMock(return_value=object())), \
            patch.object(agents, "revalidate_native_output_fences", AsyncMock(side_effect=TypeError("bad"))), \
            pytest.raises(TypeError):
        await agents._read_current_result(row, MagicMock(), multi_workspace_enabled=False)  # type: ignore[arg-type]


async def test_malformed_fences_still_deny_without_calling_revalidation() -> None:
    item = SimpleNamespace(tool_name="webhook.send", source_fences={})
    revalidate = AsyncMock()
    with patch.object(agents, "_restore_fences", MagicMock(side_effect=ValueError("bad"))), \
            patch.object(agents, "ToolExecutionPrincipal", MagicMock()), \
            patch.object(agents, "revalidate_native_output_fences", revalidate):
        assert await agents._approval_fences_current(MagicMock(), item, **CTX) is False  # type: ignore[arg-type]
    revalidate.assert_not_awaited()


async def test_harness_authorize_propagates_type_error_but_denies_other_failures() -> None:
    context = _harness()
    context._run_snapshot = AsyncMock(return_value=SimpleNamespace(owner_id=7, auth_session_hash="h"))  # type: ignore[method-assign]
    context.decode_fences = MagicMock(return_value={})  # type: ignore[method-assign]
    context.owner_principal = MagicMock()  # type: ignore[method-assign]
    state = {"source_fences": {"x": 1}}
    with patch("modules.agents.harness.revalidate_native_output_fences", AsyncMock(side_effect=TypeError("bad"))), \
            pytest.raises(TypeError):
        await context.authorize_internal_write(state, uuid4(), MagicMock(), {})  # type: ignore[arg-type]
    with patch("modules.agents.harness.revalidate_native_output_fences", AsyncMock(side_effect=RuntimeError("x"))):
        assert await context.authorize_internal_write(state, uuid4(), MagicMock(), {}) is False  # type: ignore[arg-type]


async def test_revalidate_principal_uses_local_modules_not_the_shared_registry() -> None:
    context = _harness()
    context._run_snapshot = AsyncMock(return_value=SimpleNamespace(owner_id=7))  # type: ignore[method-assign]
    context.session_factory = _factory(MagicMock())
    context.registry.list_tools.return_value = []
    lifecycle = SimpleNamespace(modules=[SimpleNamespace(id="agents", enabled=True, explicitly_disabled=False)])
    principal = SimpleNamespace(scope=JOB, actor_id="owner:7", is_owner=True)
    with patch("modules.settings.public.read_module_availability", AsyncMock(return_value=lifecycle)):
        assert await context.revalidate_principal(principal) is True  # type: ignore[arg-type]
    context.registry.set_module_registry.assert_not_called()
    assert context.modules is not None
    assert context.registry.list_tools.call_args.kwargs["modules"] is context.modules


# ----------------------------------------------------------------------------- worker


def _worker_ctx(session: MagicMock) -> dict[str, object]:
    return {
        "settings": SimpleNamespace(multi_workspace_enabled=False), "session_factory": _factory(session),
        "db_engine": MagicMock(), "redis": MagicMock(), "agent_tool_registry": MagicMock(),
    }


def _identity_session() -> MagicMock:
    identity = SimpleNamespace(
        workspace_id=WS, owner_id=7, membership_revision=2, configuration_revision=5, status="queued",
    )
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(one_or_none=lambda: identity))
    session.rollback = AsyncMock()
    return session


async def test_worker_changed_epoch_terminates_with_workspace_access_changed() -> None:
    session = _identity_session()
    with patch.object(access.workspaces, "read_access_fence", AsyncMock(return_value=MOVED)), \
            patch.object(worker, "_terminate_unadmitted", AsyncMock()) as terminate, \
            patch.object(worker, "_claim_run", AsyncMock()) as claim:
        await worker.process_agent_run(_worker_ctx(session), str(uuid4()), 1)
    assert terminate.await_args.args[-1] == "workspace_access_changed"
    claim.assert_not_awaited()


async def test_worker_claim_conflict_terminates_instead_of_escaping() -> None:
    session = _identity_session()

    @asynccontextmanager
    async def lease(*_args: object):
        yield (MagicMock(), 1)

    availability = SimpleNamespace(modules=[])
    with patch.object(access.workspaces, "read_access_fence", AsyncMock(return_value=FENCE)), \
            patch("modules.settings.public.module_is_enabled", AsyncMock(return_value=True)), \
            patch("modules.settings.public.read_module_availability", AsyncMock(return_value=availability)), \
            patch.object(worker, "_run_lease", lease), \
            patch.object(worker, "_terminate_unadmitted", AsyncMock()) as terminate, \
            patch.object(worker, "_claim_run", AsyncMock(side_effect=HTTPException(status_code=409))):
        await worker.process_agent_run(_worker_ctx(session), str(uuid4()), 1)
    assert terminate.await_args.args[-1] == "workspace_access_changed"


# ----------------------------------------------------------------------------- reconcile

QUEUE_MARK = "agent_runs.dispatch_generation, agent_runs.workspace_id"


def _reconcile_session(pages: list[list[tuple]], seen: list[str]) -> MagicMock:
    session = MagicMock()
    queue = list(pages)

    async def execute(statement: object) -> MagicMock:
        sql = str(statement.compile())  # type: ignore[attr-defined]
        seen.append(sql)
        if QUEUE_MARK in sql:
            return MagicMock(all=lambda: queue.pop(0) if queue else [])
        return MagicMock(all=list)

    session.execute = execute
    return session


async def test_reconcile_dispatches_rows_queued_behind_a_full_page_of_disabled_workspaces() -> None:
    disabled_ws = uuid4()
    good_ws, good_run = uuid4(), uuid4()
    first = [(uuid4(), 1, disabled_ws, 9) for _ in range(worker.MAX_RECONCILE_ROWS)]
    seen: list[str] = []
    session = _reconcile_session([first, [(good_run, 1, good_ws, 7)]], seen)

    async def resolve(_session: object, workspace_id: object, **_kw: object) -> SimpleNamespace:
        return SimpleNamespace(user_id=9 if workspace_id == disabled_ws else 7, membership_revision=1)

    async def enabled(_session: object, _module: str, *, scope: object, **_kw: object) -> bool:
        return scope.workspace_id != disabled_ws  # type: ignore[attr-defined]

    enqueue = AsyncMock(return_value=True)
    with patch.object(worker.workspaces, "resolve_workspace_owner_context", resolve), \
            patch("modules.settings.public.module_is_enabled", enabled), \
            patch.object(worker, "expire_pending_approvals", AsyncMock()), \
            patch.object(worker, "_enqueue_generation", enqueue):
        assert await worker.reconcile_agent_dispatch(_worker_ctx(session)) == 1
    enqueue.assert_awaited_once()
    assert enqueue.await_args.args[1] == good_run
    queued_selects = [s for s in seen if QUEUE_MARK in s]
    assert len(queued_selects) == 2 and "NOT IN" in queued_selects[1]


async def test_reconcile_enqueues_owner_unavailable_rows_so_recipe_j_can_terminalize_them() -> None:
    run_id = uuid4()
    seen: list[str] = []
    session = _reconcile_session([[(run_id, 1, uuid4(), 9)]], seen)
    enqueue = AsyncMock(return_value=True)
    with patch.object(worker.workspaces, "resolve_workspace_owner_context", AsyncMock(return_value=None)), \
            patch.object(worker, "expire_pending_approvals", AsyncMock()), \
            patch.object(worker, "_enqueue_generation", enqueue):
        assert await worker.reconcile_agent_dispatch(_worker_ctx(session)) == 1
    assert enqueue.await_args.args[1] == run_id


# ----------------------------------------------------------------------------- route lock order


async def test_start_profile_run_locks_admission_then_creates_then_commits_with_the_fence() -> None:
    order: list[str] = []

    async def lock(*_a: object, **_k: object) -> object:
        order.append("lock")
        return FENCE

    async def create(*_a: object, **_k: object) -> SimpleNamespace:
        order.append("create")
        return SimpleNamespace(id=uuid4())

    async def commit(*_a: object, **kwargs: object) -> None:
        order.append("commit")
        assert kwargs["access_fence"] == FENCE

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        settings=SimpleNamespace(multi_workspace_enabled=False), tool_registry=MagicMock(),
        redis=SimpleNamespace(enqueue_job=AsyncMock()),
    )))
    payload = agents.ProfileRunStart(
        prompt="p", conversation_id=uuid4(), client_request_id="c1", expected_profile_revision=0,
        token_budget=None,
    )
    owner = SimpleNamespace(token_hash="a" * 64)
    with patch.object(routes.settings_public, "get_ai_execution_config", AsyncMock(return_value=None)), \
            patch.object(routes.public, "lock_write_admission", lock), \
            patch.object(routes.public, "create_profile_run_in_uow", create), \
            patch.object(routes, "commit_with_replay", commit):
        await routes.start_run("knowledge", payload, request, MagicMock(), owner, OWNER)  # type: ignore[arg-type]
    assert order == ["lock", "create", "commit"]
