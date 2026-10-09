"""Realtime for members: virtual empty head, no replay reads, owner-only append unchanged."""

from __future__ import annotations

import asyncio
from dataclasses import fields
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from core import realtime, realtime_routes
from core.realtime import ReplayCursor
from core.workspaces.schemas import AccessFence, WorkspaceContext


def _member_fence():
    ws = uuid4()
    member = WorkspaceContext(user_id=2, workspace_id=ws, role="member", membership_revision=1)
    values = {f.name: 1 for f in fields(AccessFence)}
    values.update(workspace_id=ws, user_id=2)
    return member, AccessFence(**values)


def test_member_head_is_virtual_and_never_reads_replay(monkeypatch) -> None:
    member, fence = _member_fence()

    async def read_fence(session, **kw):
        return fence

    monkeypatch.setattr(realtime.workspaces, "read_access_fence", read_fence)
    head = asyncio.run(realtime.current_head(
        SimpleNamespace(), scope=member, multi_workspace_enabled=False, access_fence=fence))
    assert (head.sequence, head.floor_sequence) == (0, 1)  # any session query would raise AttributeError


def test_commit_with_replay_still_refuses_member() -> None:
    member, fence = _member_fence()

    class S:
        async def rollback(self) -> None:
            return None

    with pytest.raises(HTTPException) as exc:
        asyncio.run(realtime.commit_with_replay(
            S(), (), scope=member, multi_workspace_enabled=False, access_fence=fence))  # type: ignore[arg-type]
    assert exc.value.status_code == 403


def test_member_replay_page_rechecks_membership_and_skips_query(monkeypatch) -> None:
    member, _ = _member_fence()
    locked: list[bool] = []
    epoch = uuid4()

    async def lock(session, **kw):
        locked.append(True)

    async def head(session, **kw):
        return SimpleNamespace(epoch=epoch, sequence=0, floor_sequence=1)

    async def noop(_s):
        return None

    class Sess:
        async def scalars(self, *a):
            raise AssertionError("member must not query replay rows")

    monkeypatch.setattr(realtime_routes.workspaces, "lock_access_fence", lock)
    monkeypatch.setattr(realtime_routes, "current_head", head)
    monkeypatch.setattr(realtime_routes, "_cleanup_session", noop)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        session_factory=Sess, settings=SimpleNamespace(multi_workspace_enabled=False))))

    def page(position):
        return asyncio.run(realtime_routes._read_replay_page(
            request, workspace=member, fence=object(), auth_session=object(),  # type: ignore[arg-type]
            position=position))

    ok = page(ReplayCursor(epoch=epoch, sequence=0))
    assert locked and ok.messages == () and ok.reason is None
    assert page(ReplayCursor(epoch=uuid4(), sequence=0)).reason == "epoch_changed"
