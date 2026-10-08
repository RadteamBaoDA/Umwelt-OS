"""Workspace-scope contracts of Agents: admission, original-epoch (Recipe J) and scoped SQL."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from core.workspaces.schemas import AccessFence, InternalJobScope, WorkspaceContext
from modules.agents import access, approvals, internal_writes, worker
from modules.agents import public as agents
from modules.agents.harness import HarnessContext, RunCancelled

WS = uuid4()
OWNER = WorkspaceContext(user_id=7, workspace_id=WS, role="owner", membership_revision=2)
MEMBER = WorkspaceContext(user_id=8, workspace_id=WS, role="member", membership_revision=3)
FENCE = AccessFence(workspace_id=WS, user_id=7, membership_revision=2, configuration_revision=5)
JOB = InternalJobScope(workspace_id=WS, actor_user_id=7, membership_revision=2)
CTX = {"scope": OWNER, "multi_workspace_enabled": False}


def _sql(statement: object) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[attr-defined]


def _where(statement_sql: str) -> str:
    """Return only the predicate part so a selected column cannot satisfy a scope assertion."""
    return statement_sql.split("WHERE", 1)[1]


def _run(**overrides: object) -> SimpleNamespace:
    values = {"workspace_id": WS, "owner_id": 7, "membership_revision": 2, "configuration_revision": 5}
    values.update(overrides)
    return SimpleNamespace(**values)


# ----------------------------------------------------------------------------- access


async def test_member_is_denied_before_any_statement() -> None:
    session = MagicMock()
    session.execute = AsyncMock()
    session.scalar = AsyncMock()
    session.scalars = AsyncMock()
    with patch.object(access.workspaces, "read_access_fence", AsyncMock()) as read:
        for call in (
            agents.list_runs(session, session_factory=MagicMock(), scope=MEMBER, multi_workspace_enabled=False),
            agents.get_run(session, uuid4(), MagicMock(), scope=MEMBER, multi_workspace_enabled=False),
            agents.purge_agent_runs(session, [uuid4()], scope=MEMBER, multi_workspace_enabled=False),
            agents.redact_expired_agent_traces(
                session, cutoff=datetime.now(UTC), scope=MEMBER, multi_workspace_enabled=False,
            ),
        ):
            with pytest.raises(HTTPException) as caught:
                await call
            assert caught.value.status_code == 403
    read.assert_not_awaited()
    session.scalar.assert_not_awaited()
    session.scalars.assert_not_awaited()
    session.execute.assert_not_awaited()


async def test_flag_must_be_an_explicit_boolean() -> None:
    with pytest.raises(TypeError):
        await access.admit(MagicMock(), scope=OWNER, multi_workspace_enabled=1)  # type: ignore[arg-type]


def test_run_epoch_rebuilds_scope_from_durable_columns_only() -> None:
    epoch = access.run_epoch(_run())
    assert epoch is not None
    scope, original = epoch
    assert scope == JOB and original == FENCE


@pytest.mark.parametrize("row", [
    _run(membership_revision=None, configuration_revision=None),
    _run(configuration_revision=None),
])
def test_legacy_null_epoch_is_quarantined_not_rebased(row: SimpleNamespace) -> None:
    assert access.run_epoch(row) is None


async def test_admit_run_requires_live_fence_to_equal_original() -> None:
    moved = AccessFence(workspace_id=WS, user_id=7, membership_revision=2, configuration_revision=6)
    with patch.object(access.workspaces, "read_access_fence", AsyncMock(return_value=moved)), \
            pytest.raises(HTTPException) as caught:
        await access.admit_run(MagicMock(), _run(), multi_workspace_enabled=False)
    assert caught.value.status_code == 409
    with patch.object(access.workspaces, "read_access_fence", AsyncMock(return_value=FENCE)):
        assert await access.admit_run(MagicMock(), _run(), multi_workspace_enabled=False) == (JOB, FENCE)
        assert await access.admit_run(
            MagicMock(), _run(membership_revision=None, configuration_revision=None),
            multi_workspace_enabled=False,
        ) is None


# ----------------------------------------------------------------------------- scoped SQL


async def test_list_runs_filters_workspace_before_limit() -> None:
    session = MagicMock()
    seen: list[str] = []

    async def scalars(statement: object) -> MagicMock:
        seen.append(_sql(statement))
        return MagicMock(all=list)

    session.scalars = scalars
    with patch.object(access.workspaces, "read_access_fence", AsyncMock(return_value=FENCE)):
        page = await agents.list_runs(session, session_factory=MagicMock(), **CTX)
    assert page.items == []
    where = _where(seen[0])
    assert "agent_runs.workspace_id = " in where
    assert where.index("agent_runs.workspace_id = ") < where.index("LIMIT")


async def test_redact_expired_traces_is_workspace_bound_before_limit() -> None:
    session = MagicMock()
    seen: list[str] = []

    async def scalars(statement: object) -> MagicMock:
        seen.append(_sql(statement))
        return MagicMock(all=list)

    session.scalars = scalars
    with patch.object(access.workspaces, "read_access_fence", AsyncMock(return_value=FENCE)):
        assert await agents.redact_expired_agent_traces(session, cutoff=datetime.now(UTC), **CTX) == 0
    where = _where(seen[0])
    assert "agent_runs.workspace_id = " in where
    assert where.index("agent_runs.workspace_id = ") < where.index("LIMIT")


async def test_trace_workspace_discovery_is_bounded_identity_only() -> None:
    session = MagicMock()
    seen: list[str] = []
    ids = [uuid4(), uuid4()]

    async def scalars(statement: object) -> MagicMock:
        seen.append(_sql(statement))
        return MagicMock(all=lambda: ids)

    session.scalars = scalars
    cursor = uuid4()
    assert await agents.list_agent_trace_workspace_ids(session, after=cursor, limit=2) == tuple(ids)
    sql = seen[0]
    assert "DISTINCT agent_runs.workspace_id" in sql and "agent_runs.workspace_id >" in sql
    assert "ORDER BY agent_runs.workspace_id" in sql and "LIMIT" in sql
    for column in ("prompt", "answer", "owner_id"):
        assert f"agent_runs.{column}" not in sql
    for bad in (0, 101, True):
        with pytest.raises(ValueError):
            await agents.list_agent_trace_workspace_ids(session, limit=bad)  # type: ignore[arg-type]


# ----------------------------------------------------------------------------- run creation


async def test_create_run_stores_workspace_actor_and_original_epoch() -> None:
    session = MagicMock()
    added: list[object] = []
    session.add = added.append
    session.refresh = AsyncMock()
    tool = SimpleNamespace(
        name="search.query", risk=agents.ToolRisk.READ_ONLY, confirmation_required=False,
        version="1", schema_fingerprint="f" * 64,
    )
    registry = MagicMock()
    registry.list_tools.return_value = [tool]
    registry.hides_tool = None
    request = agents.AgentRunStart(prompt="hi", conversation_id=None, token_budget=None)
    now = datetime.now(UTC)
    with patch.object(access.workspaces, "lock_access_fence", AsyncMock(return_value=FENCE)) as lock, \
            patch.object(agents, "commit_with_replay", AsyncMock()) as commit, \
            patch.object(agents, "_read", MagicMock(return_value="read")):
        assert await agents.create_run(session, "a" * 64, request, registry, **CTX) == "read"
    lock.assert_awaited_once()
    run = added[0]
    assert (run.workspace_id, run.owner_id) == (WS, 7)
    assert (run.membership_revision, run.configuration_revision) == (2, 5)
    assert commit.await_args.kwargs["access_fence"] == FENCE
    assert now  # silence unused-import style checks for datetime


async def test_create_profile_run_locks_fence_first_and_stores_epoch() -> None:
    session = MagicMock()
    added: list[object] = []
    session.add = added.append
    session.flush = AsyncMock()
    session.execute = AsyncMock()
    session.scalar = AsyncMock(return_value=None)
    snapshot = {
        "revision": 0, "allowed_tools": [{"name": "search.query", "version": "1", "fingerprint": "f"}],
        "id": "knowledge",
    }
    order: list[str] = []

    async def lock(*args: object, **kwargs: object) -> AccessFence:
        order.append("fence")
        return FENCE

    async def resolve(*args: object, **kwargs: object) -> tuple[dict[str, object], str]:
        order.append("profile")
        assert kwargs["scope"] == OWNER
        return snapshot, "h" * 64

    request = agents.ProfileRunStart(
        prompt="p", conversation_id=uuid4(), client_request_id="c1", expected_profile_revision=0,
        token_budget=None,
    )
    with patch.object(access.workspaces, "lock_access_fence", lock), \
            patch.object(agents, "resolve_profile_snapshot", resolve), \
            patch("modules.chat.public.link_agent_run", AsyncMock()), \
            patch.object(agents, "_read", MagicMock(return_value="read")):
        assert await agents.create_profile_run_in_uow(
            session, "a" * 64, "knowledge", request, MagicMock(), None, **CTX,
        ) == "read"
    assert order == ["fence", "profile"]
    run = added[0]
    assert (run.workspace_id, run.membership_revision, run.configuration_revision) == (WS, 2, 5)


# ----------------------------------------------------------------------------- approved writes


async def test_run_approved_write_hands_principal_scope_to_perform() -> None:
    performed: list[object] = []

    async def perform(session: object, scope: object) -> str:
        performed.append(scope)
        return "task:1"

    @asynccontextmanager
    async def factory():
        yield MagicMock()

    context = {
        "action_id": str(uuid4()), "before_internal_write": AsyncMock(return_value=True),
        "session_factory": factory, "principal": SimpleNamespace(scope=JOB),
    }
    with patch("modules.agents.approvals.mark_effect_outcome", AsyncMock()) as outcome:
        result = await internal_writes.run_approved_write({}, context, perform, ())
    assert result.success is True and performed == [JOB]
    outcome.assert_awaited()


async def test_run_approved_write_without_typed_scope_is_forbidden() -> None:
    performed = AsyncMock(return_value="x")
    context = {
        "action_id": str(uuid4()), "before_internal_write": AsyncMock(return_value=True),
        "session_factory": MagicMock(), "principal": SimpleNamespace(scope=1),
    }
    result = await internal_writes.run_approved_write({}, context, performed, ())
    assert result.success is False and result.error_code == "forbidden"
    performed.assert_not_awaited()


# ----------------------------------------------------------------------------- approvals


def _factory(session: MagicMock):
    @asynccontextmanager
    async def factory():
        yield session

    return factory


async def test_effect_reservation_is_denied_when_original_epoch_changed() -> None:
    session = MagicMock()
    session.scalar = AsyncMock()
    with patch.object(access.workspaces, "lock_access_fence", AsyncMock(
        side_effect=HTTPException(status_code=409, detail="Workspace access fence changed"),
    )):
        allowed = await approvals.reserve_effect_before_send(
            _factory(session), action_id=uuid4(), run_id=uuid4(), owner_id=7, auth_session_hash="h",
            claim_generation=1, definition=MagicMock(), arguments={}, destination_id="d",
            destination_revision="1", scope=JOB, original_fence=FENCE, multi_workspace_enabled=False,
        )
    assert allowed is False
    session.scalar.assert_not_awaited()


async def test_approved_action_verification_requires_original_epoch() -> None:
    moved = AccessFence(workspace_id=WS, user_id=7, membership_revision=3, configuration_revision=5)
    session = MagicMock()
    session.scalar = AsyncMock()
    with patch.object(access.workspaces, "read_access_fence", AsyncMock(return_value=moved)):
        allowed = await approvals.verify_approved_action(
            _factory(session), action_id=uuid4(), run_id=uuid4(), owner_id=7, auth_session_hash="h",
            claim_generation=1, definition=MagicMock(), arguments={}, destination_id="d",
            destination_revision="1", scope=JOB, original_fence=FENCE, multi_workspace_enabled=False,
        )
    assert allowed is False
    session.scalar.assert_not_awaited()


# ----------------------------------------------------------------------------- harness


def _context() -> HarnessContext:
    return HarnessContext(
        uuid4(), JOB, FENCE, 1, MagicMock(), MagicMock(), SimpleNamespace(multi_workspace_enabled=False),
        MagicMock(), MagicMock(), MagicMock(), 1, 0.0, frozenset(), {},
    )


async def test_harness_cancels_when_the_original_epoch_is_gone() -> None:
    context = _context()
    moved = AccessFence(workspace_id=WS, user_id=7, membership_revision=2, configuration_revision=6)
    with patch.object(access.workspaces, "read_access_fence", AsyncMock(return_value=moved)), \
            pytest.raises(RunCancelled):
        await context.admit_original(MagicMock())
    with patch.object(access.workspaces, "lock_access_fence", AsyncMock(
        side_effect=HTTPException(status_code=404, detail="Workspace not found"),
    )), pytest.raises(RunCancelled):
        await context.admit_original(MagicMock(), lock=True)


async def test_harness_principal_carries_the_run_scope_and_rejects_foreign_scope() -> None:
    context = _context()
    context.registry.get_tool.return_value = None
    with patch("modules.agents.harness.ToolExecutionPrincipal") as principal_cls:
        context.owner_principal({}, "dest")  # type: ignore[arg-type]
    assert principal_cls.call_args.kwargs["scope"] == JOB
    foreign = InternalJobScope(workspace_id=uuid4(), actor_user_id=7, membership_revision=2)
    assert await context.revalidate_principal(SimpleNamespace(scope=foreign)) is False  # type: ignore[arg-type]


# ----------------------------------------------------------------------------- worker


async def test_worker_quarantines_a_legacy_run_without_claiming_it() -> None:
    run_id = uuid4()
    identity = SimpleNamespace(
        workspace_id=WS, owner_id=7, membership_revision=None, configuration_revision=None, status="queued",
    )
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(one_or_none=lambda: identity))
    ctx = {
        "settings": SimpleNamespace(multi_workspace_enabled=False), "session_factory": _factory(session),
        "db_engine": MagicMock(), "redis": MagicMock(), "agent_tool_registry": MagicMock(),
    }
    with patch.object(worker, "_terminate_unadmitted", AsyncMock()) as terminate, \
            patch.object(worker, "_claim_run", AsyncMock()) as claim:
        await worker.process_agent_run(ctx, str(run_id), 1)
    terminate.assert_awaited_once()
    assert terminate.await_args.args[-1] == "workspace_epoch_unavailable"
    claim.assert_not_awaited()


async def test_worker_leaves_work_untouched_when_agents_are_disabled_for_the_workspace() -> None:
    identity = SimpleNamespace(
        workspace_id=WS, owner_id=7, membership_revision=2, configuration_revision=5, status="queued",
    )
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(one_or_none=lambda: identity))
    ctx = {
        "settings": SimpleNamespace(multi_workspace_enabled=False), "session_factory": _factory(session),
        "db_engine": MagicMock(), "redis": MagicMock(), "agent_tool_registry": MagicMock(),
    }
    with patch.object(access.workspaces, "read_access_fence", AsyncMock(return_value=FENCE)), \
            patch("modules.settings.public.module_is_enabled", AsyncMock(return_value=False)) as enabled, \
            patch.object(worker, "_terminate_unadmitted", AsyncMock()) as terminate, \
            patch.object(worker, "_claim_run", AsyncMock()) as claim:
        await worker.process_agent_run(ctx, str(uuid4()), 1)
    assert enabled.await_args.kwargs["scope"] == JOB
    terminate.assert_not_awaited()
    claim.assert_not_awaited()
