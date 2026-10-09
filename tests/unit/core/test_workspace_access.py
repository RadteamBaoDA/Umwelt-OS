from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from core.workspaces.access import assert_resource_access, can_read_resource
from core.workspaces.schemas import GrantRef, WorkspaceContext


def test_membership_without_share_is_not_read_access():
    workspace_id = uuid4()
    member = WorkspaceContext(2, workspace_id, "member", 1)
    assert not can_read_resource(member, workspace_id, False)
    assert can_read_resource(member, workspace_id, True)
    assert not can_read_resource(member, uuid4(), True)


def test_context_is_immutable():
    ctx = WorkspaceContext(2, uuid4(), "member", 1)
    with pytest.raises(FrozenInstanceError):
        ctx.role = "owner"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        ctx.workspace_id = uuid4()  # type: ignore[misc]


@pytest.mark.asyncio
async def test_assert_resource_access_owner_and_member(monkeypatch):
    wid, rid = uuid4(), uuid4()
    owner, member = WorkspaceContext(1, wid, "owner", 1), WorkspaceContext(2, wid, "member", 1)
    grant = GrantRef("document", rid, 1, 3)
    read, lock = AsyncMock(return_value=(grant,)), AsyncMock()
    monkeypatch.setattr("core.workspaces.public.read_resource_grants", read)
    monkeypatch.setattr("core.workspaces.public.lock_resource_grants", lock)

    await assert_resource_access(None, owner, "document", rid, 3)  # type: ignore[arg-type]
    read.assert_not_called()
    await assert_resource_access(None, member, "document", rid, 3)  # type: ignore[arg-type]
    lock.assert_awaited_once()
    with pytest.raises(HTTPException) as stale:
        await assert_resource_access(None, member, "document", rid, 4)  # type: ignore[arg-type]
    assert stale.value.status_code == 404
    read.return_value = ()
    with pytest.raises(HTTPException) as missing:
        await assert_resource_access(None, member, "document", rid, 3)  # type: ignore[arg-type]
    assert missing.value.status_code == 404
