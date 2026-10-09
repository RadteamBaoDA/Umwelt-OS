"""Mock-level lifecycle contracts: purge scope, running rows survive expiry, revoke hooks purge briefs only."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from core.workspaces import public as workspaces
from modules.translations import lifecycle

WS, RID = uuid4(), uuid4()


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


async def test_purge_is_workspace_scoped_with_no_actor_filter():
    result = MagicMock(rowcount=3)
    session = MagicMock(execute=AsyncMock(return_value=result), flush=AsyncMock())
    assert await lifecycle.purge_resource_translations(session, WS, "daily_brief", RID) == 3
    sql = _sql(session.execute.await_args.args[0])
    assert sql.startswith("DELETE FROM content_translations")
    assert str(WS) in sql and "'daily_brief'" in sql and str(RID) in sql and "actor_user_id" not in sql


async def test_expiry_never_evicts_a_live_leased_running_row():
    result = MagicMock(rowcount=2)
    session = MagicMock(execute=AsyncMock(return_value=result))
    assert await lifecycle.expire_page(session, now=datetime(2030, 1, 1, tzinfo=UTC)) == 4  # rows + batches
    first = _sql(session.execute.await_args_list[0].args[0])
    assert "expires_at <=" in first and "lease_token IS NOT NULL" in first and "lease_expires_at >" in first
    assert "LIMIT 100" in first and "SKIP LOCKED" in first
    assert "DELETE FROM translation_batches" in _sql(session.execute.await_args_list[1].args[0])


async def test_brief_share_revoke_purges_but_document_does_not(monkeypatch):
    purge = AsyncMock(return_value=1)
    monkeypatch.setattr(lifecycle, "purge_resource_translations", purge)
    session = MagicMock()
    await workspaces._purge_translations(session, WS, "document", (RID,))
    purge.assert_not_awaited()
    other = uuid4()
    await workspaces._purge_translations(session, WS, "brief", (RID, other))
    assert [c.args for c in purge.await_args_list] == [
        (session, WS, "daily_brief", RID), (session, WS, "daily_brief", other)]
