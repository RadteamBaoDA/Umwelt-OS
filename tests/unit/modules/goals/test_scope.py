"""Workspace-scope contracts for Goals: member denial, scoped predicates, insert stamping, agent tools."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from core.workspaces.schemas import WorkspaceContext
from modules.goals import public
from modules.goals.models import Goal
from modules.goals.schemas import GoalCreate, GoalFilter

OWNER = WorkspaceContext(user_id=7, workspace_id=uuid4(), role="owner", membership_revision=1)
MEMBER = WorkspaceContext(user_id=8, workspace_id=OWNER.workspace_id, role="member", membership_revision=1)
FLAG = False


def _admitted() -> object:
    """Skip the workspace fence lookup while the owner-role check is tested separately."""
    return patch("modules.goals.public.workspaces.read_access_fence", AsyncMock(return_value=MagicMock()))


def _sql(statement: object) -> tuple[str, list[object]]:
    """Compile a statement so scope predicates and bound values can be asserted."""
    compiled = statement.compile()  # type: ignore[attr-defined]
    return str(compiled), list(compiled.params.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ["get", "list"])
async def test_member_without_share_sees_nothing(call: str) -> None:
    """A workspace member is denied before any goal row is read."""
    session = AsyncMock()
    with pytest.raises(HTTPException) as caught:
        if call == "get":
            await public.get_goal(session, uuid4(), scope=MEMBER, multi_workspace_enabled=FLAG)
        else:
            await public.list_goals(session, GoalFilter(), scope=MEMBER, multi_workspace_enabled=FLAG)
    assert caught.value.status_code == 403
    session.scalar.assert_not_called()
    session.scalars.assert_not_called()


@pytest.mark.asyncio
async def test_foreign_goal_id_is_missing_and_query_is_workspace_scoped() -> None:
    """A goal id from another workspace resolves to GoalMissing; the SELECT carries the workspace."""
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=None)
    with _admitted(), pytest.raises(public.GoalMissing):
        await public.get_goal(session, uuid4(), scope=OWNER, multi_workspace_enabled=FLAG)
    text, params = _sql(session.scalar.call_args.args[0])
    assert "goals.workspace_id" in text
    assert OWNER.workspace_id in params


@pytest.mark.asyncio
async def test_list_filters_workspace_before_limit() -> None:
    """The page query applies the workspace predicate in WHERE, ahead of ORDER BY / LIMIT."""
    session = AsyncMock()
    result = MagicMock()
    result.all.return_value = []
    session.scalars = AsyncMock(return_value=result)
    with _admitted():
        await public.list_goals(session, GoalFilter(limit=5), scope=OWNER, multi_workspace_enabled=FLAG)
    text, params = _sql(session.scalars.call_args.args[0])
    assert text.index("goals.workspace_id") < text.index("ORDER BY") < text.index("LIMIT")
    assert OWNER.workspace_id in params


@pytest.mark.asyncio
async def test_insert_carries_workspace_id_and_actor() -> None:
    """A new goal is stamped with the admitted workspace and its actor."""
    session = AsyncMock()
    session.add = MagicMock()
    with (
        _admitted(),
        patch("modules.goals.public.workspaces.lock_access_fence", AsyncMock(return_value=MagicMock())),
        patch("modules.goals.public.commit_with_replay", AsyncMock()),
        patch("modules.goals.public._to_goal_read", MagicMock()),
        patch("modules.goals.public._current_entity_projection", AsyncMock()),
    ):
        await public.create_goal(session, GoalCreate(title="g"), scope=OWNER, multi_workspace_enabled=FLAG)
    goal = session.add.call_args.args[0]
    assert isinstance(goal, Goal)
    assert goal.workspace_id == OWNER.workspace_id
    assert goal.owner_id == 7


@pytest.mark.asyncio
async def test_get_tool_uses_principal_scope_and_flag() -> None:
    """The agent read tool queries with the principal's scope and the configured flag."""
    from modules.goals import tools

    session = MagicMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=None)
    goal = MagicMock()
    goal.model_dump.return_value = {}
    context = {"session_factory": factory, "principal": SimpleNamespace(scope=OWNER),
               "settings": SimpleNamespace(multi_workspace_enabled=True)}
    goal_id = uuid4()
    with patch("modules.goals.public.get_goal", AsyncMock(return_value=goal)) as fetched:
        result = await tools._get({"goal_id": str(goal_id)}, context)
    assert result.success
    assert fetched.call_args.kwargs == {"scope": OWNER, "multi_workspace_enabled": True}


@pytest.mark.asyncio
async def test_write_tool_passes_admitted_scope_to_service() -> None:
    """Approved writes hand the scope given by run_approved_write to the public service call."""
    from modules.goals import tools

    created = SimpleNamespace(id=uuid4())
    with patch("modules.goals.public.create_goal", AsyncMock(return_value=created)) as create:
        reference = await tools._create(AsyncMock(), OWNER, True, {"title": "x"}, lambda value: value)
    assert reference == f"goal:{created.id}"
    assert create.call_args.kwargs == {"scope": OWNER, "multi_workspace_enabled": True}
