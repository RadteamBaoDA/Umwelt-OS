"""Scheduled-task threads: visible in the Chat list, retrieval scope pinned server-side."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from modules.chat import routes as chat_routes
from modules.chat._worker.base import _pin_task_thread_scope
from modules.chat._worker.setup import _load_turn_inputs
from modules.chat.db_query import turn_source_scope

A, B = str(uuid4()), str(uuid4())


def test_pin_ignores_client_scope() -> None:
    params = {"source_scope": [A]}
    assert _pin_task_thread_scope(
        params, {"scheduled_task": {"kind": "daily_brief"}, "task_source_scope": [B]}
    )
    assert params["source_scope"] == [B]
    assert turn_source_scope(params) == [B]


def test_pin_without_briefs_is_empty_enforced_scope() -> None:
    params = {"source_scope": [A]}
    assert _pin_task_thread_scope(params, {"scheduled_task": {"kind": "daily_brief"}})
    assert params["source_scope"] == []
    assert turn_source_scope(params) == []


def test_pin_leaves_plain_conversations_alone() -> None:
    params = {"source_scope": [A]}
    assert not _pin_task_thread_scope(params, {"task_source_scope": [B]})
    assert not _pin_task_thread_scope(params, None)
    assert params["source_scope"] == [A]


def test_pin_drops_malformed_entries() -> None:
    params: dict = {}
    _pin_task_thread_scope(params, {"scheduled_task": {}, "task_source_scope": [B, "nope", 3]})
    assert params["source_scope"] == [B]


@pytest.mark.asyncio
async def test_turn_inputs_pin_task_scope() -> None:
    """The worker request carries the pinned scope and enforcement, not the client's."""
    session = MagicMock()
    meta = {"scheduled_task": {"kind": "daily_brief"}, "task_source_scope": [B]}
    session.scalar = AsyncMock(
        side_effect=[SimpleNamespace(id=uuid4(), content="hi", revision_of_message_id=None), meta]
    )
    session.commit = AsyncMock()
    c = SimpleNamespace(
        response_id=uuid4(),
        privacy_fence=None,
        job_scope=None,
        access_fence=None,
        scope=None,
        redis=None,
    )
    claimed = SimpleNamespace(
        conversation_id=uuid4(), user_message_id=uuid4(), context_req={"source_scope": [A]}
    )
    with (
        patch(
            "modules.chat._worker.setup._lock_live_response", AsyncMock(return_value=(False, None))
        ),
        patch("modules.chat._worker.setup.is_run_cancelled", AsyncMock(return_value=False)),
        patch(
            "modules.chat._worker.setup._history_cutoff", AsyncMock(return_value=datetime.now(UTC))
        ),
    ):
        turn = await _load_turn_inputs(session, c, claimed)
    assert [str(x) for x in turn.answer_request.source_scope] == [B]
    assert turn.answer_request.source_scope_enforced is True
    assert turn.req_params["source_scope"] == [B]


@pytest.mark.asyncio
async def test_list_includes_task_threads_only() -> None:
    session = MagicMock()
    session.scalars = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    with patch.object(
        chat_routes,
        "owner_default_scope",
        AsyncMock(return_value=SimpleNamespace(workspace_id=uuid4())),
    ):
        await chat_routes.list_conversations(
            session,
            SimpleNamespace(owner_id=1),
            MagicMock(headers={}),
            limit=5,
            offset=0,
            archived=False,
            q=None,
        )
    sql = str(session.scalars.await_args.args[0].compile(dialect=postgresql.dialect()))
    assert "context_kind != " in sql
    assert "chat_conversations.metadata ? " in sql
