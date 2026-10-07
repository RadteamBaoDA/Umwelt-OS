"""Privacy and bounds of conversation title search (P15 T2, BM-22)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.dialects import postgresql

from modules.chat.public import search_conversations

BS = chr(92)


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
        assert await search_conversations(session, "plan", limit=10, offset=0, archived=False) == []
    session.scalars.assert_not_awaited()


@pytest.mark.asyncio
async def test_query_excludes_ephemeral_expired_automation_and_is_bounded() -> None:
    session = _session()
    with patch("modules.chat.public._chat_export_privacy", AsyncMock(return_value=(True, True, None))):
        await search_conversations(session, "plan", limit=7, offset=3, archived=False)
    sql, _ = _sql(session)
    assert "chat_conversations.ephemeral IS false" in sql
    assert "chat_conversations.expires_at IS NULL OR chat_conversations.expires_at >" in sql
    assert "context_kind != " in sql
    assert "LIMIT" in sql and "OFFSET" in sql
    assert "ORDER BY chat_conversations.updated_at DESC" in sql


@pytest.mark.asyncio
async def test_like_metacharacters_are_literal() -> None:
    session = _session()
    with patch("modules.chat.public._chat_export_privacy", AsyncMock(return_value=(True, True, None))):
        await search_conversations(session, f" 100%_a{BS}b ", limit=5, offset=0, archived=True)
    sql, params = _sql(session)
    assert "ESCAPE" in sql
    assert f"%100{BS}%{BS}_a{BS}{BS}b%" in params.values()
