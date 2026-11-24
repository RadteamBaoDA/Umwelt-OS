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
        patch("modules.chat._worker.setup.latest_task_brief", AsyncMock(return_value=None)),
        patch("modules.chat._worker.setup.multi_workspace_enabled", return_value=False),
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


def test_pin_drops_client_retrieval_overrides() -> None:
    params = {
        "source_scope": [A],
        "selected_refs": ["x"],
        "selected_only": True,
        "selection_fences": ["f"],
        "entity_ids": [A],
        "date_context": "2026-01-01",
        "timezone": "UTC",
        "date": "2026-01-01",
        "kind": "day",
    }
    assert _pin_task_thread_scope(params, {"scheduled_task": {}, "task_source_scope": [B]})
    assert params == {"source_scope": [B], "kind": "day"}


@pytest.mark.asyncio
async def test_turn_inputs_task_thread_has_no_client_overrides() -> None:
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
        conversation_id=uuid4(),
        user_message_id=uuid4(),
        context_req={
            "kind": "day",
            "date": "2026-01-01",
            "timezone": "UTC",
            "date_context": "2026-01-01",
            "entity_ids": [A],
            "selected_only": True,
            "selected_refs": [],
        },
    )
    with (
        patch(
            "modules.chat._worker.setup._lock_live_response", AsyncMock(return_value=(False, None))
        ),
        patch("modules.chat._worker.setup.is_run_cancelled", AsyncMock(return_value=False)),
        patch(
            "modules.chat._worker.setup._history_cutoff", AsyncMock(return_value=datetime.now(UTC))
        ),
        patch("modules.chat._worker.setup.latest_task_brief", AsyncMock(return_value=None)),
        patch("modules.chat._worker.setup.multi_workspace_enabled", return_value=False),
    ):
        turn = await _load_turn_inputs(session, c, claimed)
    req = turn.answer_request
    assert req.entity_ids == [] and req.selected_refs == [] and req.selected_only is False
    assert req.date_context is None and req.timezone is None


@pytest.mark.asyncio
async def test_allowed_sources_pinned_scope_excludes_other_source() -> None:
    from modules.chat.db_query import _allowed_sources

    session = MagicMock()
    result = MagicMock()
    result.all.return_value = []
    session.execute = AsyncMock(return_value=result)
    await _allowed_sources(session, workspace_id=uuid4(), source_scope=[B], allow_local_only=True)
    sql = str(
        session.execute.await_args.args[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert B.replace("-", "") in sql.replace("-", "") and A.replace("-", "") not in sql.replace(
        "-", ""
    )


@pytest.mark.asyncio
async def test_db_query_not_granted_for_empty_task_scope() -> None:
    from modules.chat._worker.setup import _db_query_granted

    turn = SimpleNamespace(
        req_params={},
        answer_request=SimpleNamespace(
            selected_only=False, source_scope_enforced=True, source_scope=[]
        ),
    )
    assert await _db_query_granted(MagicMock(), MagicMock(), turn, False) is False


@pytest.mark.asyncio
async def test_patch_keeps_stored_reserved_keys() -> None:
    from modules.chat.schemas import ConversationPatch

    stored = {"scheduled_task": {"kind": "daily_brief"}, "task_source_scope": [B]}
    conv = SimpleNamespace(
        id=uuid4(),
        title="t",
        context_kind=None,
        context_resource_id=None,
        pinned=False,
        archived=False,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        metadata_json=dict(stored),
        ephemeral=False,
        expires_at=None,
    )
    session = MagicMock()
    session.scalar = AsyncMock(return_value=conv)
    session.commit = AsyncMock()
    session.refresh = AsyncMock(side_effect=lambda o: setattr(o, "updated_at", datetime.now(UTC)))
    with patch.object(
        chat_routes,
        "owner_default_scope",
        AsyncMock(return_value=SimpleNamespace(workspace_id=uuid4())),
    ):
        await chat_routes.patch_conversation(
            conv.id,
            ConversationPatch(metadata={"task_source_scope": [A], "task_kind": "x", "ok": 1}),
            session,
            SimpleNamespace(owner_id=1),  # type: ignore[arg-type]
        )
    assert conv.metadata_json == {**stored, "ok": 1}


@pytest.mark.asyncio
async def test_create_drops_reserved_keys() -> None:
    from modules.chat.schemas import ConversationCreate

    session = MagicMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    scope = SimpleNamespace(workspace_id=uuid4(), user_id=1)
    with (
        patch.object(chat_routes, "owner_default_scope", AsyncMock(return_value=scope)),
        patch.object(chat_routes, "lock_export_privacy", AsyncMock()),
        patch.object(
            chat_routes,
            "read_export_privacy",
            AsyncMock(return_value=SimpleNamespace(store_conversation_history=True)),
        ),
        pytest.raises(Exception),  # noqa: B017  # response building on a bare ORM object is out of scope
    ):
        await chat_routes.create_conversation(
            ConversationCreate(metadata={"scheduled_task": {"kind": "daily_brief"}, "ok": 1}),
            session,
            SimpleNamespace(owner_id=1),  # type: ignore[arg-type]
        )
    assert session.add.call_args.args[0].metadata_json == {"ok": 1}


def test_task_thread_pin_drops_client_picker_marker() -> None:
    """A task thread never inherits the picker origin, so its web opt-in is not suppressed."""
    params: dict[str, object] = {"source_scope_origin": "picker"}
    assert _pin_task_thread_scope(params, {"scheduled_task": {}, "task_source_scope": [B]})
    assert "source_scope_origin" not in params
