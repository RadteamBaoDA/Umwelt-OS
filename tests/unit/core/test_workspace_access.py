from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from core.auth.schemas import AccountRead, AccountSessionRef
from core.workspaces.access import assert_resource_access, can_read_resource
from core.workspaces.dependencies import require_workspace_observer_read
from core.workspaces.schemas import AdminWorkspaceReadContext, GrantRef, WorkspaceContext


def test_membership_without_share_is_not_read_access():
    workspace_id = uuid4()
    member = WorkspaceContext(2, workspace_id, "member", 1)
    assert not can_read_resource(member, workspace_id, False)
    assert can_read_resource(member, workspace_id, True)
    assert not can_read_resource(member, uuid4(), True)


def test_instance_admin_view_reads_only_its_selected_workspace():
    workspace_id = uuid4()
    admin_view = AdminWorkspaceReadContext(1, workspace_id, 7, 3, 4)
    assert can_read_resource(admin_view, workspace_id, False)
    assert not can_read_resource(admin_view, uuid4(), True)


def test_context_is_immutable():
    ctx = WorkspaceContext(2, uuid4(), "member", 1)
    with pytest.raises(FrozenInstanceError):
        ctx.role = "owner"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        ctx.workspace_id = uuid4()  # type: ignore[misc]


@pytest.mark.asyncio
async def test_observer_dependency_resolves_admin_view_and_rechecks_fence(monkeypatch):
    workspace_id = uuid4()
    account = AccountRead(id=1, email=None, default_workspace_id=uuid4(), email_verified_at=None,
                          email_verification_source=None)
    viewer = AdminWorkspaceReadContext(1, workspace_id, 7, 2, 3)
    request = SimpleNamespace(
        method="GET", state=SimpleNamespace(account=account),
        app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(multi_workspace_enabled=False))),
    )
    resolve = AsyncMock(return_value=viewer)
    fence = AsyncMock()
    publication_gate = Mock()
    session = object()
    monkeypatch.setattr("core.workspaces.dependencies.resolve_admin_workspace_context", resolve)
    monkeypatch.setattr("core.workspaces.dependencies.read_access_fence", fence)
    monkeypatch.setattr("core.workspaces.dependencies.authenticated_session_ref", lambda _request: AccountSessionRef(1, "a" * 64))
    monkeypatch.setattr("core.workspaces.dependencies.require_publication_gate", publication_gate)

    result = await require_workspace_observer_read(request, object(), session, str(workspace_id))

    assert result is viewer
    resolve.assert_awaited_once()
    fence.assert_awaited_once_with(session, scope=viewer, multi_workspace_enabled=False)
    publication_gate.assert_called_once()


@pytest.mark.asyncio
async def test_observer_dependency_rejects_post_writes():
    request = SimpleNamespace(method="POST", url=SimpleNamespace(path="/api/v1/tasks"))
    with pytest.raises(HTTPException) as denied:
        await require_workspace_observer_read(request, object(), object(), str(uuid4()))
    assert denied.value.status_code == 403


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
