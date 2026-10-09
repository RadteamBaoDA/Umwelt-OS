"""Unit tests (mocked) for workspace share lifecycle, CAS and lock order."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from core.workspaces import access, public
from core.workspaces.models import WorkspaceShare
from core.workspaces.schemas import (
    GrantRef,
    ResourceAccessProjection,
    ShareUpsert,
    WorkspaceContext,
)

WS, OWNER, MEMBER = uuid4(), 1, 2


def _memberships(member_revision=3, with_member=True):
    owner = SimpleNamespace(user_id=OWNER, role="owner", owner_user_id=OWNER, revision=5)
    out = {OWNER: owner}
    if with_member:
        out[MEMBER] = SimpleNamespace(user_id=MEMBER, role="member", owner_user_id=None, revision=member_revision)
    return SimpleNamespace(owner_user_id=OWNER, configuration_revision=7), out


def _patch_locks(monkeypatch, calls, **kw):
    ws, ms = _memberships(**kw)

    async def lock(session, workspace_id, ids):
        calls.append("workspace+memberships")
        assert list(ids) == sorted(ids)
        return ws, ms

    monkeypatch.setattr(access, "lock_workspace_memberships", lock)
    return ws, ms


@pytest.mark.asyncio
@pytest.mark.parametrize("expected,status", [(None, 428), (2, 409)])
async def test_cas_is_target_membership_revision(monkeypatch, expected, status):
    _patch_locks(monkeypatch, [])
    with pytest.raises(HTTPException) as exc:
        await access.lock_share_management(MagicMock(), WS, OWNER, MEMBER, expected)
    assert exc.value.status_code == status


@pytest.mark.asyncio
async def test_cas_target_not_member_and_actor_not_owner(monkeypatch):
    _patch_locks(monkeypatch, [], with_member=False)
    with pytest.raises(HTTPException) as exc:
        await access.lock_share_management(MagicMock(), WS, OWNER, MEMBER, 3)
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:  # the owner is never a share target
        await access.lock_share_management(MagicMock(), WS, OWNER, OWNER, 5)
    assert exc.value.status_code == 404
    _patch_locks(monkeypatch, [])
    with pytest.raises(HTTPException) as exc:  # member acting as actor
        await access.lock_share_management(MagicMock(), WS, MEMBER, OWNER, 5)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_cas_ok_returns_locked_rows(monkeypatch):
    _patch_locks(monkeypatch, [])
    _, ms = await access.lock_share_management(MagicMock(), WS, OWNER, MEMBER, 3)
    assert set(ms) == {OWNER, MEMBER}


def test_granted_resource_ids_sql_and_owner_rejected():
    scope = WorkspaceContext(MEMBER, WS, "member", 3)
    sql = str(public.granted_resource_ids(scope=scope, kind="document").compile(dialect=postgresql.dialect()))
    for fragment in ("workspace_id", "member_user_id", "revoked_at IS NULL", "membership_revision", "resource_type"):
        assert fragment in sql
    with pytest.raises(ValueError):
        public.granted_resource_ids(scope=WorkspaceContext(OWNER, WS, "owner", 5), kind="document")


def _row(**kw):
    base = {"workspace_id": WS, "resource_type": "document", "resource_id": uuid4(), "member_user_id": MEMBER,
                "granted_by_user_id": OWNER, "revision": 1, "resource_revision": 4, "membership_revision": 3,
                "created_at": datetime.now(UTC), "updated_at": datetime.now(UTC), "revoked_at": None}
    return SimpleNamespace(**{**base, **kw})


def _setup_grant(monkeypatch, calls, *, row, projection_revision=4):
    ws, ms = _patch_locks(monkeypatch, calls)

    async def lock_row(*a):
        calls.append("share")
        return row

    async def projection(session, **kw):
        calls.append("resource")
        return ResourceAccessProjection(WS, kw["resource_id"], projection_revision, True)

    monkeypatch.setattr(public, "_lock_share_row", lock_row)
    monkeypatch.setattr(public, "_owner_visible_projection", projection)
    monkeypatch.setattr(public, "WorkspaceShare", lambda **kw: _row(**kw))
    session = MagicMock()
    session.flush = AsyncMock()
    return ws, ms, session


@pytest.mark.asyncio
async def test_grant_lock_order_and_no_revision_bump(monkeypatch):
    calls: list[str] = []
    ws, ms, session = _setup_grant(monkeypatch, calls, row=None)
    out = await public.grant_share_in_uow(
        session, WS, OWNER, "document", uuid4(), MEMBER, ShareUpsert(expected_revision=3, resource_revision=4),
        multi_workspace_enabled=True,
    )
    assert calls == ["workspace+memberships", "share", "resource"]
    assert (out.revision, out.membership_revision, out.resource_revision) == (1, 3, 4)
    assert ws.configuration_revision == 7 and ms[MEMBER].revision == 3 and ms[OWNER].revision == 5
    session.add.assert_called_once()


@pytest.mark.asyncio
async def test_regrant_bumps_share_revision_and_noop_when_in_effect(monkeypatch):
    row = _row(revoked_at=datetime.now(UTC))
    _, _, session = _setup_grant(monkeypatch, [], row=row)
    body = ShareUpsert(expected_revision=3, resource_revision=4)
    out = await public.grant_share_in_uow(session, WS, OWNER, "document", row.resource_id, MEMBER, body,
                                          multi_workspace_enabled=True)
    assert out.revision == 2 and out.revoked_at is None
    out = await public.grant_share_in_uow(session, WS, OWNER, "document", row.resource_id, MEMBER, body,
                                          multi_workspace_enabled=True)
    assert out.revision == 2  # already in effect, unchanged


@pytest.mark.asyncio
async def test_grant_resource_revision_mismatch_is_409(monkeypatch):
    _, _, session = _setup_grant(monkeypatch, [], row=None, projection_revision=9)
    with pytest.raises(HTTPException) as exc:
        await public.grant_share_in_uow(
            session, WS, OWNER, "document", uuid4(), MEMBER, ShareUpsert(expected_revision=3, resource_revision=4),
            multi_workspace_enabled=True,
        )
    assert (exc.value.status_code, exc.value.detail) == (409, "resource_revision_changed")


@pytest.mark.asyncio
async def test_revoke_bumps_share_only(monkeypatch):
    row = _row()
    ws, ms, session = _setup_grant(monkeypatch, [], row=row)
    await public.revoke_share_in_uow(session, WS, OWNER, "document", row.resource_id, MEMBER, 3)
    assert row.revoked_at is not None and row.revision == 2
    assert ws.configuration_revision == 7 and ms[MEMBER].revision == 3
    with pytest.raises(HTTPException) as exc:  # missing If-Match
        await public.revoke_share_in_uow(session, WS, OWNER, "document", row.resource_id, MEMBER, None)
    assert exc.value.status_code == 428


@pytest.mark.asyncio
async def test_brief_path_propagates_shareable_409(monkeypatch):
    import modules.dashboard.public as dash

    _patch_locks(monkeypatch, [])
    monkeypatch.setattr(public, "_lock_share_row", AsyncMock(return_value=None))

    async def not_shareable(*a, **kw):
        raise HTTPException(status_code=409, detail="brief_not_shareable")

    monkeypatch.setattr(dash, "check_brief_shareable", not_shareable, raising=False)
    monkeypatch.setattr(dash, "read_brief_access_projection", AsyncMock(), raising=False)
    with pytest.raises(HTTPException) as exc:
        await public.grant_share_in_uow(
            MagicMock(), WS, OWNER, "brief", uuid4(), MEMBER, ShareUpsert(expected_revision=3, resource_revision=1),
            multi_workspace_enabled=True,
        )
    assert (exc.value.status_code, exc.value.detail) == (409, "brief_not_shareable")


@pytest.mark.asyncio
async def test_lock_resource_grants_rejects_changed_grant():
    scope = WorkspaceContext(MEMBER, WS, "member", 3)
    row = _row()
    session = MagicMock()
    session.scalars = AsyncMock(return_value=[row])
    good = GrantRef("document", row.resource_id, 1, 4)
    await public.lock_resource_grants(session, scope=scope, grants=(good,))
    for bad in (GrantRef("document", row.resource_id, 2, 4), GrantRef("document", uuid4(), 1, 4)):
        with pytest.raises(HTTPException) as exc:
            await public.lock_resource_grants(session, scope=scope, grants=(bad,))
        assert exc.value.status_code == 404
    row.revoked_at = datetime.now(UTC)
    with pytest.raises(HTTPException):
        await public.lock_resource_grants(session, scope=scope, grants=(good,))


@pytest.mark.asyncio
async def test_revoke_resource_shares_flush_only():
    session = MagicMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(rowcount=2))
    session.commit = AsyncMock()
    assert await public.revoke_resource_shares_in_uow(
        session, workspace_id=WS, resource_type="document", resource_ids=(uuid4(),)) == 2
    assert await public.revoke_resource_shares_in_uow(
        session, workspace_id=WS, resource_type="document", resource_ids=()) == 0
    session.commit.assert_not_called()


def test_share_model_has_no_secret_columns():
    assert not {"token_hash", "token"} & set(WorkspaceShare.__table__.columns.keys())
