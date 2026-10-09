"""Privacy and bounds of conversation title search (P15 T2, BM-22)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from modules.chat.public import search_conversations

BS = chr(92)
SCOPE = SimpleNamespace(workspace_id=uuid4(), user_id=7)
KW = {"scope": SCOPE, "multi_workspace_enabled": False}


def _session() -> MagicMock:
    session = MagicMock()
    session.scalars = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    return session


def _sql(session: MagicMock) -> tuple[str, dict[str, object]]:
    compiled = session.scalars.await_args.args[0].compile(dialect=postgresql.dialect())
    return str(compiled), compiled.params


@pytest.mark.asyncio
async def test_history_off_returns_nothing_and_never_queries() -> None:
    session = _session()
    with patch("modules.chat.public._chat_export_privacy", AsyncMock(return_value=(False, True, None))):
        assert await search_conversations(session, "plan", limit=10, offset=0, archived=False, **KW) == []
    session.scalars.assert_not_awaited()


@pytest.mark.asyncio
async def test_query_excludes_ephemeral_expired_automation_and_is_bounded() -> None:
    session = _session()
    with patch("modules.chat.public._chat_export_privacy", AsyncMock(return_value=(True, True, None))):
        await search_conversations(session, "plan", limit=7, offset=3, archived=False, **KW)
    sql, _ = _sql(session)
    assert "chat_conversations.ephemeral IS false" in sql
    assert "chat_conversations.expires_at IS NULL OR chat_conversations.expires_at >" in sql
    assert "context_kind != " in sql
    assert "LIMIT" in sql and "OFFSET" in sql
    assert "ORDER BY chat_conversations.pinned DESC, chat_conversations.updated_at DESC" in sql


@pytest.mark.asyncio
async def test_like_metacharacters_are_literal() -> None:
    session = _session()
    with patch("modules.chat.public._chat_export_privacy", AsyncMock(return_value=(True, True, None))):
        await search_conversations(session, f" 100%_a{BS}b ", limit=5, offset=0, archived=True, **KW)
    sql, params = _sql(session)
    assert "ESCAPE" in sql
    assert f"%100{BS}%{BS}_a{BS}{BS}b%" in params.values()


@pytest.mark.asyncio
async def test_workspace_and_actor_bind_before_limit() -> None:
    session = _session()
    with patch("modules.chat.public._chat_export_privacy", AsyncMock(return_value=(True, True, None))):
        await search_conversations(session, "plan", limit=7, offset=3, archived=False, **KW)
    sql, params = _sql(session)
    assert sql.index("chat_conversations.workspace_id =") < sql.index("LIMIT")
    assert sql.index("chat_conversations.actor_user_id =") < sql.index("LIMIT")
    assert SCOPE.workspace_id in params.values() and 7 in params.values()


@pytest.mark.asyncio
async def test_count_source_conversations_binds_workspace_in_both_branches() -> None:
    from modules.chat.public import count_source_conversations

    session = MagicMock()
    session.scalar = AsyncMock(return_value=0)
    await count_source_conversations(session, uuid4(), scope=SCOPE)  # type: ignore[arg-type]
    sql = str(session.scalar.await_args.args[0].compile(dialect=postgresql.dialect()))
    assert sql.count("workspace_id =") == 2
    assert sql.index("workspace_id =") < sql.index("LIMIT")
