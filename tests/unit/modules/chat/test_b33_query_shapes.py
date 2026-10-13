"""B3.3: passive_deletes on chat cascades, stable list ordering, run lookup bound to the conversation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from modules.chat import routes as chat_routes
from modules.chat.models import Conversation, ResponseRun


def test_cascading_relationships_defer_to_database_cascade() -> None:
    """Every ORM cascade whose FK is ON DELETE CASCADE must not load children on delete."""
    for rel in (Conversation.messages, Conversation.response_runs, ResponseRun.stream_events):
        assert rel.property.passive_deletes is True
        assert rel.property.cascade.delete_orphan
    # The DB cascade the ORM now relies on.
    assert {fk.ondelete for fk in Conversation.messages.property.mapper.local_table.c.conversation_id.foreign_keys} == {"CASCADE"}
    assert {fk.ondelete for fk in ResponseRun.stream_events.property.mapper.local_table.c.response_id.foreign_keys} == {"CASCADE"}
    assert any(c.ondelete == "CASCADE" for c in ResponseRun.__table__.foreign_key_constraints)


async def test_list_conversations_orders_by_id_tiebreaker(monkeypatch) -> None:
    session = MagicMock()
    session.scalars = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    monkeypatch.setattr(chat_routes, "owner_default_scope", AsyncMock(return_value=SimpleNamespace(workspace_id=uuid4())))
    await chat_routes.list_conversations(
        session, SimpleNamespace(owner_id=1), MagicMock(headers={}), limit=5, offset=5, archived=False, q=None,
    )
    sql = str(session.scalars.await_args.args[0].compile(dialect=postgresql.dialect()))
    assert "ORDER BY chat_conversations.pinned DESC, chat_conversations.updated_at DESC, chat_conversations.id DESC" in sql


async def test_get_conversation_run_lookup_filters_by_conversation(monkeypatch) -> None:
    cid, mid = uuid4(), uuid4()
    conv = SimpleNamespace(id=cid, ephemeral=False, expires_at=None)
    msg = SimpleNamespace(id=mid, role="user")
    statements: list[str] = []
    session = MagicMock()
    session.scalar = AsyncMock(return_value=conv)

    async def scalars(stmt):
        return MagicMock(all=MagicMock(return_value=[msg]))

    async def execute(stmt):
        statements.append(str(stmt.compile(dialect=postgresql.dialect())))
        raise _Stop

    session.scalars = scalars
    session.execute = execute
    monkeypatch.setattr(chat_routes, "owner_default_scope", AsyncMock(return_value=SimpleNamespace(workspace_id=uuid4())))
    monkeypatch.setattr(chat_routes, "_reject_expired_conversation", lambda c: None)
    try:
        await chat_routes.get_conversation(cid, session, SimpleNamespace(owner_id=1), MagicMock(headers={}))
    except _Stop:
        pass
    assert statements and "chat_response_runs.conversation_id =" in statements[0]


class _Stop(Exception):
    pass
