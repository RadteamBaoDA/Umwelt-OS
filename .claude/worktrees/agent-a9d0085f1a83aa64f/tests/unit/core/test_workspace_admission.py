"""Canonical workspace actor/admission helpers reproduce the observed module copies."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from core.errors import DomainError
from core.workspaces import admission, public
from core.workspaces.schemas import AdminWorkspaceReadContext, InternalJobScope, WorkspaceContext

WS = uuid4()
OWNER = WorkspaceContext(user_id=1, workspace_id=WS, role="owner", membership_revision=1)
MEMBER = WorkspaceContext(user_id=2, workspace_id=WS, role="member", membership_revision=1)
ADMIN = AdminWorkspaceReadContext(
    actor_user_id=1,
    workspace_id=WS,
    workspace_owner_user_id=7,
    membership_revision=1,
    configuration_revision=1,
)
JOB = InternalJobScope(workspace_id=WS, actor_user_id=3, membership_revision=1)


def test_actor_id_matches_both_legacy_variants() -> None:
    """Job scopes yield the actor; request and admin-view scopes yield the data-owner user_id."""
    assert admission.actor_id(OWNER) == 1
    assert admission.actor_id(JOB) == 3
    assert admission.actor_id(ADMIN) == 7  # data owner, not operator 1


@pytest.fixture
def fences(monkeypatch: pytest.MonkeyPatch) -> tuple[AsyncMock, AsyncMock]:
    """Patch the fence primitives resolved through core.workspaces.public."""
    read, lock = AsyncMock(return_value="read"), AsyncMock(return_value="lock")
    monkeypatch.setattr(public, "read_access_fence", read)
    monkeypatch.setattr(public, "lock_access_fence", lock)
    return read, lock


@pytest.mark.asyncio
async def test_owner_read_and_lock_paths(fences: tuple[AsyncMock, AsyncMock]) -> None:
    """Owners reach read or lock fence; ``expected`` is forwarded only on the lock path."""
    read, lock = fences
    assert await admission.admit_scope(None, scope=OWNER, multi_workspace_enabled=False) == "read"  # type: ignore[arg-type]
    read.assert_awaited_once_with(None, scope=OWNER, multi_workspace_enabled=False)
    assert (
        await admission.admit_scope(  # type: ignore[arg-type]
            None,
            scope=JOB,
            multi_workspace_enabled=True,
            lock=True,
            expected="fence",  # type: ignore[arg-type]
        )
        == "lock"
    )
    lock.assert_awaited_once_with(None, scope=JOB, expected="fence", multi_workspace_enabled=True)


@pytest.mark.asyncio
async def test_member_denied_before_any_fence(fences: tuple[AsyncMock, AsyncMock]) -> None:
    """Members get 403 unless owner_only is disabled (news stories variant)."""
    with pytest.raises(DomainError) as denied:
        await admission.admit_scope(None, scope=MEMBER, multi_workspace_enabled=False)  # type: ignore[arg-type]
    assert denied.value.status_code == 403 and denied.value.detail == "Workspace owner required"
    fences[0].assert_not_awaited()
    assert (
        await admission.admit_scope(  # type: ignore[arg-type]
            None,
            scope=MEMBER,
            multi_workspace_enabled=False,
            owner_only=False,
        )
        == "read"
    )


@pytest.mark.asyncio
async def test_admin_view_is_read_only_when_requested(fences: tuple[AsyncMock, AsyncMock]) -> None:
    """Admin viewers may read but cannot lock when admin_read_only_on_lock is set."""
    await admission.admit_scope(
        None, scope=ADMIN, multi_workspace_enabled=False, admin_read_only_on_lock=True
    )  # type: ignore[arg-type]
    with pytest.raises(DomainError) as denied:
        await admission.admit_scope(  # type: ignore[arg-type]
            None,
            scope=ADMIN,
            multi_workspace_enabled=False,
            lock=True,
            admin_read_only_on_lock=True,
        )
    assert denied.value.detail == "Admin workspace access is read-only"


@pytest.mark.asyncio
async def test_scope_type_and_flag_type_errors(fences: tuple[AsyncMock, AsyncMock]) -> None:
    """Untyped scopes and non-bool flags raise TypeError with the legacy wording."""
    with pytest.raises(TypeError, match="An explicit Memory workspace scope is required"):
        await admission.admit_scope(  # type: ignore[arg-type]
            None,
            scope=ADMIN,
            multi_workspace_enabled=False,
            allowed_scopes=(WorkspaceContext, InternalJobScope),
            scope_label="Memory workspace",
        )
    with pytest.raises(TypeError, match="must be a boolean"):
        await admission.admit_scope(None, scope=OWNER, multi_workspace_enabled=1)  # type: ignore[arg-type]
    # Temporal/search variants never validated the flag.
    assert (
        await admission.admit_scope(  # type: ignore[arg-type]
            None,
            scope=OWNER,
            multi_workspace_enabled=1,
            check_flag=False,  # type: ignore[arg-type]
        )
        == "read"
    )
