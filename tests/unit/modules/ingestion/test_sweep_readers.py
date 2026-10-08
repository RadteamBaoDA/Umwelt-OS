"""Workspace-scope contracts for the automation sweep readers (compiled SQL and denial; no DB)."""

import inspect
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from core.workspaces.schemas import AccessFence, InternalJobScope, WorkspaceContext
from modules.ingestion import public

WORKSPACE_ID = uuid4()
SCOPE = InternalJobScope(workspace_id=WORKSPACE_ID, actor_user_id=7, membership_revision=3)
FENCE = AccessFence(WORKSPACE_ID, 7, 3, 1)
POSITION = (datetime(2026, 1, 1, tzinfo=UTC), uuid4())


class _Rows:
    def all(self) -> list:
        return []


class _Session:
    """Record every statement; every result is empty."""

    def __init__(self) -> None:
        self.statements: list = []

    async def scalars(self, statement):
        self.statements.append(statement)
        return _Rows()

    async def execute(self, statement):
        self.statements.append(statement)
        return _Rows()


@pytest.fixture(autouse=True)
def admission():
    mock = AsyncMock(return_value=FENCE)
    with patch.object(public.workspaces, "read_access_fence", mock):
        yield mock


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


@pytest.mark.parametrize("reader", [public.list_ready_events_after, public.list_terminal_runs_after])
def test_position_is_a_tuple_not_an_opaque_cursor(reader) -> None:
    parameters = inspect.signature(reader).parameters
    assert "tuple" in str(parameters["position"].annotation)
    assert parameters["scope"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["multi_workspace_enabled"].kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.parametrize("reader", [public.list_ready_events_after, public.list_terminal_runs_after])
async def test_workspace_predicate_precedes_order_and_limit(reader, admission) -> None:
    session = _Session()
    await reader(session, POSITION, 25, scope=SCOPE, multi_workspace_enabled=False)
    admission.assert_awaited_once_with(session, scope=SCOPE, multi_workspace_enabled=False)
    sql = _sql(session.statements[0])
    where, order, limit = sql.index("WHERE"), sql.index("ORDER BY"), sql.index("LIMIT")
    assert where < sql.index("workspace_id", where) < order < limit
    assert "(" in sql[where:order] and ") >" in sql[where:order]  # keyset tuple comparison


@pytest.mark.parametrize("reader", [public.list_ready_events_after, public.list_terminal_runs_after])
async def test_member_denied_before_any_query(reader) -> None:
    member = WorkspaceContext(user_id=2, workspace_id=WORKSPACE_ID, role="member", membership_revision=1)
    session = _Session()
    with pytest.raises(HTTPException) as denied:
        await reader(session, None, scope=member, multi_workspace_enabled=True)
    assert denied.value.status_code == 403
    assert session.statements == []


@pytest.mark.parametrize("reader", [public.list_ready_events_after, public.list_terminal_runs_after])
async def test_stale_admission_denied_before_any_query(reader, admission) -> None:
    admission.side_effect = HTTPException(status_code=409, detail="stale")
    session = _Session()
    with pytest.raises(HTTPException):
        await reader(session, POSITION, scope=SCOPE, multi_workspace_enabled=False)
    assert session.statements == []


async def test_opaque_sweep_cursor_builder_is_gone() -> None:
    assert not hasattr(public, "encode_ingestion_cursor")
