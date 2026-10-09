"""W4-jobs-c: a denied collection job is terminal (permission_lost / stale_scope); recovery skips it."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from modules.connectors import scheduler


def _request() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(), status="queued", source_id=uuid4(), workspace_id=uuid4(), actor_user_id=7,
        membership_revision=3, source_generation=1,
    )


def _session(request: SimpleNamespace) -> MagicMock:
    session = MagicMock()
    session.get = AsyncMock(return_value=request)
    session.rollback = AsyncMock()
    session.commit = AsyncMock()
    session.execute = AsyncMock()
    return session


def test_denial_code() -> None:
    assert scheduler.denial_code(HTTPException(401)) == "permission_lost"
    assert scheduler.denial_code(HTTPException(404)) == "permission_lost"
    assert scheduler.denial_code(HTTPException(409)) == "stale_scope"


@pytest.mark.asyncio
@pytest.mark.parametrize(("statuses", "code", "calls"), [
    ([401], "permission_lost", 1), ([404], "permission_lost", 1),
    ([409, 409], "stale_scope", 2),  # re-resolved exactly once
])
async def test_denied_request_becomes_terminal(statuses: list[int], code: str, calls: int) -> None:
    request = _request()
    session = _session(request)
    access = AsyncMock(side_effect=[HTTPException(s) for s in statuses])
    with patch.object(scheduler.connectors, "_connector_access", access):
        assert await scheduler.admit_collection_request(session, request.id, multi_workspace_enabled=False) is None
    assert access.await_count == calls
    values = session.execute.await_args.args[0].compile().params
    assert values["status"] == "cancelled" and values["error_code"] == code
    session.commit.assert_awaited()


@pytest.mark.asyncio
async def test_recovery_skips_terminal_rows() -> None:
    ids = [uuid4()]
    session = MagicMock()
    session.scalars = AsyncMock(return_value=iter([]))  # DB filter queued/running returns nothing for terminal rows
    assert await scheduler.recoverable_request_ids(session, ids) == set()
    sql = str(session.scalars.await_args.args[0].compile(compile_kwargs={"literal_binds": True}))
    assert "'queued'" in sql and "'running'" in sql and "cancelled" not in sql
