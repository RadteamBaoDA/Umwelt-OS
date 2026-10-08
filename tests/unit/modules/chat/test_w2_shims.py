"""Chat call-shape shims pass the W2 scope/gate kwargs to Memory and Agents."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from core.workspaces.schemas import WorkspaceContext
from modules.chat import scope as chat_scope


def _ctx() -> WorkspaceContext:
    return WorkspaceContext(user_id=1, workspace_id=uuid4(), role="owner", membership_revision=3)


@pytest.mark.asyncio
async def test_read_export_privacy_gets_scope_and_gate(monkeypatch):
    ctx = _ctx()
    monkeypatch.setattr(chat_scope, "owner_default_scope", AsyncMock(return_value=ctx))
    monkeypatch.setattr(chat_scope, "multi_workspace_enabled", lambda: True)
    callee = AsyncMock(return_value="privacy")
    monkeypatch.setattr("modules.memory.public.read_export_privacy", callee)
    session = MagicMock()
    assert await chat_scope.read_owner_export_privacy(session) == "privacy"
    callee.assert_awaited_once_with(session, scope=ctx, multi_workspace_enabled=True)


@pytest.mark.asyncio
async def test_purge_conversation_actions_gets_scope_and_gate(monkeypatch):
    from modules.chat import public as chat_public

    ctx = _ctx()
    monkeypatch.setattr(chat_scope, "owner_default_scope", AsyncMock(return_value=ctx))
    monkeypatch.setattr(chat_scope, "multi_workspace_enabled", lambda: False)
    callee = AsyncMock(return_value=0)
    monkeypatch.setattr("modules.agents.public.purge_conversation_actions", callee)
    monkeypatch.setattr("modules.memory.public.lock_export_privacy", AsyncMock())
    conv = SimpleNamespace(pinned=False)
    session = MagicMock()
    session.scalar = AsyncMock(side_effect=[conv, 1])
    session.delete = AsyncMock()
    cid = uuid4()
    assert await chat_public.delete_conversation(session, cid, owner_id=1) is True
    callee.assert_awaited_once_with(session, cid, scope=ctx, multi_workspace_enabled=False)


def test_export_functions_accept_scope_kwargs():
    import inspect

    from modules.chat import public as chat_public

    for fn in (chat_public.export_page, chat_public.validate_export_fences):
        params = inspect.signature(fn).parameters
        assert params["scope"].kind is inspect.Parameter.KEYWORD_ONLY or "scope" in params
        assert "multi_workspace_enabled" in params


@pytest.mark.asyncio
async def test_chat_export_privacy_uses_given_scope(monkeypatch):
    from modules.chat import public as chat_public

    ctx = _ctx()
    privacy = SimpleNamespace(store_conversation_history=True, persisted=False, updated_at=None)
    callee = AsyncMock(return_value=privacy)
    monkeypatch.setattr("modules.memory.public.read_export_privacy", callee)
    session = MagicMock()
    out = await chat_public._chat_export_privacy(session, scope=ctx, multi_workspace_enabled=True)
    assert out == (True, False, None)
    callee.assert_awaited_once_with(session, scope=ctx, multi_workspace_enabled=True)
