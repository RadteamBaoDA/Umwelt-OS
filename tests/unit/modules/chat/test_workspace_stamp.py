"""Chat inserts must stamp workspace identity (NOT NULL columns + composite FKs)."""

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from core.workspaces.schemas import WorkspaceContext
from modules.chat import public as chat_public
from modules.chat import routes, seed
from modules.chat.models import AgentActivityLink, Conversation, Message, ResponseRun
from modules.chat.schemas import MessageMutationRequest, SendMessageRequest

WS = uuid4()
CTX = WorkspaceContext(user_id=1, workspace_id=WS, role="owner", membership_revision=1)
OWNER = SimpleNamespace(owner_id=1, token_hash="h")


def _conv(**kw):
    return SimpleNamespace(
        id=uuid4(), workspace_id=WS, actor_user_id=1, ephemeral=False, expires_at=None,
        updated_at=None, **kw,
    )


def _session(scalar=None):
    s = MagicMock()
    s.add = MagicMock(side_effect=lambda o: setattr(o, "id", o.id or uuid4()) if hasattr(o, "id") else None)
    s.flush = AsyncMock()
    s.commit = AsyncMock()
    s.refresh = AsyncMock()
    s.scalar = scalar or AsyncMock(return_value=None)
    return s


def _added(session, cls):
    return [c.args[0] for c in session.add.call_args_list if isinstance(c.args[0], cls)]


def _patch_routes(monkeypatch, conv):
    monkeypatch.setattr(routes, "lock_export_privacy", AsyncMock())
    monkeypatch.setattr(routes, "owner_default_scope", AsyncMock(return_value=CTX))
    monkeypatch.setattr(routes, "read_export_privacy", AsyncMock(
        return_value=SimpleNamespace(store_conversation_history=True)))
    monkeypatch.setattr(routes, "_lock_conversation", AsyncMock(return_value=conv))
    monkeypatch.setattr(routes, "_reject_active_response", AsyncMock())
    monkeypatch.setattr(routes, "_privacy_fence", lambda _p: {})
    monkeypatch.setattr(routes, "_dispatch_response_run", AsyncMock())
    monkeypatch.setattr(routes.chat_public, "resolve_gadget_context", AsyncMock(return_value={}))


@pytest.mark.asyncio
async def test_create_conversation_stamps_scope(monkeypatch):
    _patch_routes(monkeypatch, _conv())
    session = _session()
    from modules.chat.schemas import ConversationCreate
    with pytest.raises(Exception):  # noqa: B017 - response building on a bare ORM object is out of scope
        await routes.create_conversation(ConversationCreate(), session, OWNER)
    (conv,) = _added(session, Conversation)
    assert conv.workspace_id == WS and conv.actor_user_id == 1


@pytest.mark.asyncio
async def test_send_message_run_stamps_workspace(monkeypatch):
    conv = _conv()
    _patch_routes(monkeypatch, conv)
    session = _session()
    await routes.send_message(
        conv.id, SendMessageRequest(content="hi"), MagicMock(), session, OWNER,
    )
    (run,) = _added(session, ResponseRun)
    assert run.workspace_id == WS and run.actor_user_id == 1


@pytest.mark.asyncio
async def test_mutate_message_run_stamps_workspace(monkeypatch):
    conv = _conv()
    _patch_routes(monkeypatch, conv)
    target = SimpleNamespace(id=uuid4(), role="user", content="old", response_id=None)
    run = SimpleNamespace(user_message_id=uuid4(), retrieval_context={})
    prompt = SimpleNamespace(content="old")
    queue = [None, None, target, run, prompt]  # receipt, collision, target, original run, prompt
    session = _session(AsyncMock(side_effect=lambda *_a, **_k: queue.pop(0)))
    payload = MessageMutationRequest(
        action="edit", base_content_hash=hashlib.sha256(b"old").hexdigest(),
        client_request_id="r1", content="new",
    )
    await routes.mutate_message(conv.id, target.id, payload, MagicMock(), session, OWNER)
    (new_run,) = _added(session, ResponseRun)
    assert new_run.workspace_id == WS and new_run.actor_user_id == 1
    assert _added(session, Message)


@pytest.mark.asyncio
async def test_link_agent_run_stamps_workspace(monkeypatch):
    conv = _conv()
    session = _session(AsyncMock(return_value=conv))
    monkeypatch.setattr("core.auth.public.revalidate_account_session", AsyncMock(return_value=True))
    monkeypatch.setattr(chat_public, "is_history_storage_enabled", AsyncMock(return_value=True))
    await chat_public.link_agent_run(session, conv.id, uuid4(), 1, "h" * 64)
    (link,) = _added(session, AgentActivityLink)
    assert link.workspace_id == WS and link.owner_id == 1


@pytest.mark.asyncio
async def test_demo_conversation_stamps_scope(monkeypatch):
    monkeypatch.setattr("modules.chat.worker.is_history_storage_enabled", AsyncMock(return_value=True))
    session = _session()
    assert await seed.ensure_demo_conversation(
        session, scope=CTX, multi_workspace_enabled=False) == (1, 0, 0)
    (conv,) = _added(session, Conversation)
    assert conv.workspace_id == WS and conv.actor_user_id == 1
