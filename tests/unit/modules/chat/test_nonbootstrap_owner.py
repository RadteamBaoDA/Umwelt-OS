"""Workspace owner other than the bootstrap account (id 7) must pass Chat/Agent link and export gates."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from core.workspaces.schemas import WorkspaceContext
from modules.chat import public as chat_public
from modules.chat import scope as chat_scope
from modules.chat.models import AgentActivityLink

WS = uuid4()
OWNER7 = WorkspaceContext(user_id=7, workspace_id=WS, role="owner", membership_revision=1)


def _flag(monkeypatch, value=True):
    monkeypatch.setattr(chat_scope, "multi_workspace_enabled", lambda: value)


@pytest.mark.asyncio
async def test_revalidate_chat_session_passes_flag(monkeypatch):
    _flag(monkeypatch)
    seen = AsyncMock(return_value=True)
    monkeypatch.setattr("core.auth.public.revalidate_account_session", seen)
    assert await chat_scope.revalidate_chat_session(MagicMock(), "h", 7)
    assert seen.await_args.kwargs == {"multi_workspace_enabled": True}


@pytest.mark.asyncio
async def test_link_agent_run_allows_owner_7_and_denies_other_actor(monkeypatch):
    _flag(monkeypatch)
    monkeypatch.setattr("core.auth.public.revalidate_account_session", AsyncMock(return_value=True))
    monkeypatch.setattr(chat_public, "is_history_storage_enabled", AsyncMock(return_value=True))
    conv = SimpleNamespace(id=uuid4(), workspace_id=WS, actor_user_id=7, ephemeral=False, expires_at=None)
    session = MagicMock(scalar=AsyncMock(return_value=conv), add=MagicMock())
    await chat_public.link_agent_run(session, conv.id, uuid4(), 7, "h" * 64)
    (link,) = [c.args[0] for c in session.add.call_args_list if isinstance(c.args[0], AgentActivityLink)]
    assert link.owner_id == 7 and link.workspace_id == WS
    conv.actor_user_id = 8  # a member/other actor cannot link onto someone else's conversation
    with pytest.raises(HTTPException) as denied:
        await chat_public.link_agent_run(session, conv.id, uuid4(), 7, "h" * 64)
    assert denied.value.status_code == 404


@pytest.mark.asyncio
async def test_live_agent_conversation_id_not_pinned_to_owner_1():
    cid = uuid4()
    session = MagicMock(scalar=AsyncMock(return_value=cid))
    assert await chat_public.live_agent_conversation_id(session, uuid4(), 7, "h") == cid


@pytest.mark.asyncio
async def test_filter_live_run_ids_denies_when_session_invalid(monkeypatch):
    _flag(monkeypatch)
    monkeypatch.setattr("core.auth.public.revalidate_account_session", AsyncMock(return_value=False))
    assert await chat_public.filter_live_agent_run_ids(MagicMock(), [uuid4()], 7, "h") == frozenset()


@pytest.mark.asyncio
async def test_export_owner_gate(monkeypatch):
    session = MagicMock(scalar=AsyncMock(return_value=7))
    await chat_public._require_chat_export_owner(session, 7, scope=OWNER7, multi_workspace_enabled=True)
    with pytest.raises(PermissionError):  # rollout off: only the bootstrap account
        await chat_public._require_chat_export_owner(session, 7, scope=OWNER7, multi_workspace_enabled=False)
    member = WorkspaceContext(user_id=8, workspace_id=WS, role="member", membership_revision=1)
    with pytest.raises(PermissionError):  # scope belongs to someone else
        await chat_public._require_chat_export_owner(session, 7, scope=member, multi_workspace_enabled=True)


@pytest.mark.asyncio
async def test_export_owner_gate_denies_member_role_of_same_user():
    session = MagicMock(scalar=AsyncMock(return_value=7))
    member = WorkspaceContext(user_id=7, workspace_id=WS, role="member", membership_revision=1)
    with pytest.raises(PermissionError):
        await chat_public._require_chat_export_owner(session, 7, scope=member, multi_workspace_enabled=True)
