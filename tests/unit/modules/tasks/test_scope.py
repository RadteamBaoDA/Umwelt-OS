"""Workspace-scope contracts for Tasks: member denial, scoped predicates, insert stamping, agent tools."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from core.workspaces.schemas import InternalJobScope, WorkspaceContext
from modules.tasks import public
from modules.tasks.models import Task
from modules.tasks.schemas import TaskCreate, TaskFilter

OWNER = WorkspaceContext(user_id=7, workspace_id=uuid4(), role="owner", membership_revision=1)
MEMBER = WorkspaceContext(user_id=8, workspace_id=OWNER.workspace_id, role="member", membership_revision=1)
FLAG = False


def _admitted() -> object:
    """Skip the workspace fence lookup while keeping the owner-role check under test elsewhere."""
    return patch("modules.tasks.public.workspaces.read_access_fence", AsyncMock(return_value=MagicMock()))


def _sql(statement: object) -> tuple[str, list[object]]:
    """Compile a statement with literal-free binds so scope predicates can be asserted."""
    compiled = statement.compile()  # type: ignore[attr-defined]
    return str(compiled), list(compiled.params.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ["get", "list"])
async def test_member_without_share_sees_nothing(call: str) -> None:
    """A workspace member is denied before any task row is read."""
    session = AsyncMock()
    with pytest.raises(HTTPException) as caught:
        if call == "get":
            await public.get_task(session, uuid4(), scope=MEMBER, multi_workspace_enabled=FLAG)
        else:
            await public.list_tasks(session, TaskFilter(), scope=MEMBER, multi_workspace_enabled=FLAG)
    assert caught.value.status_code == 403
    session.scalar.assert_not_called()
    session.scalars.assert_not_called()


@pytest.mark.asyncio
async def test_foreign_task_id_is_missing_and_query_is_workspace_scoped() -> None:
    """A task id from another workspace resolves to TaskMissing; the SELECT carries the workspace."""
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=None)
    with _admitted(), pytest.raises(public.TaskMissing):
        await public.get_task(session, uuid4(), scope=OWNER, multi_workspace_enabled=FLAG)
    text, params = _sql(session.scalar.call_args.args[0])
    assert "tasks.workspace_id" in text
    assert OWNER.workspace_id in params


@pytest.mark.asyncio
async def test_list_filters_workspace_before_limit() -> None:
    """The page query applies the workspace predicate in WHERE, ahead of ORDER BY / LIMIT."""
    session = AsyncMock()
    result = MagicMock()
    result.all.return_value = []
    session.scalars = AsyncMock(return_value=result)
    with _admitted():
        await public.list_tasks(session, TaskFilter(limit=5), scope=OWNER, multi_workspace_enabled=FLAG)
    text, params = _sql(session.scalars.call_args.args[0])
    assert text.index("tasks.workspace_id") < text.index("ORDER BY") < text.index("LIMIT")
    assert OWNER.workspace_id in params


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [OWNER, InternalJobScope(workspace_id=OWNER.workspace_id, actor_user_id=7, membership_revision=1)])
async def test_insert_carries_workspace_id_and_actor(scope: WorkspaceContext | InternalJobScope) -> None:
    """New tasks are stamped with the admitted workspace and its actor, for HTTP and job scopes."""
    session = AsyncMock()
    session.add = MagicMock()
    with patch("modules.tasks.public._to_task_read", MagicMock()):  # flush-only mock leaves timestamps unset
        await public.create_task_in_uow(session, TaskCreate(title="t"), scope=scope, multi_workspace_enabled=FLAG)
    task = session.add.call_args.args[0]
    assert isinstance(task, Task)
    assert task.workspace_id == OWNER.workspace_id
    assert task.owner_id == 7


@pytest.mark.asyncio
async def test_list_tool_uses_principal_scope_and_flag() -> None:
    """The agent read tool queries with the principal's scope, never an owner id from arguments."""
    from modules.tasks import tools

    session = MagicMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=None)
    page = MagicMock()
    page.model_dump.return_value = {"items": []}
    context = {"session_factory": factory, "principal": SimpleNamespace(scope=OWNER),
               "settings": SimpleNamespace(multi_workspace_enabled=True)}
    with patch("modules.tasks.public.list_tasks", AsyncMock(return_value=page)) as listed:
        result = await tools._list({}, context)
    assert result.success
    assert listed.call_args.kwargs == {"scope": OWNER, "multi_workspace_enabled": True}


@pytest.mark.asyncio
async def test_write_tool_passes_admitted_scope_to_service() -> None:
    """Approved writes hand the scope given by run_approved_write to the public service call."""
    from modules.tasks import tools

    created = SimpleNamespace(id=uuid4())
    with patch("modules.tasks.public.create_task", AsyncMock(return_value=created)) as create:
        reference = await tools._create(AsyncMock(), OWNER, True, {"title": "x"}, lambda value: value)
    assert reference == f"task:{created.id}"
    assert create.call_args.kwargs == {"scope": OWNER, "multi_workspace_enabled": True}
