"""Integration proof that the chat SQL tool's dedicated login role is read-only and isolated from the app role.

Runs against the migrated disposable ``bbd_test`` database (migration 0017 plus
``python -m core.chat_sql_provision`` applying the password). The parser allowlist is bypassed on purpose
(``_run_sql_as_reader`` / raw reader connections) to prove the database layer holds on its own.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from modules.chat import db_query
from modules.chat.db_query import CHAT_SQL_LOGIN_ROLE, _run_sql_as_reader

pytestmark = pytest.mark.integration

_APP_ROLE = "bbd_test"


@pytest.fixture
async def reader_engine(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncEngine]:
    """Engine logging in as the reader; also exported as ``CHAT_SQL_READER_DATABASE_URL`` for the tool."""
    url = os.getenv("CHAT_SQL_READER_DATABASE_URL")
    if not url:
        pytest.skip("CHAT_SQL_READER_DATABASE_URL is available only in the disposable test runner")
    monkeypatch.setenv("CHAT_SQL_READER_DATABASE_URL", url)
    db_query._reader_engines.clear()
    engine = create_async_engine(url, pool_size=1, max_overflow=0)
    yield engine
    await engine.dispose()
    for cached in db_query._reader_engines.values():
        await cached.dispose()
    db_query._reader_engines.clear()


async def test_scoped_query_runs_as_reader_and_returns_rows(reader_engine: AsyncEngine) -> None:
    """A scoped query works repeatedly and an unknown workspace yields zero rows."""
    for _ in range(2):
        rows = await _run_sql_as_reader(
            "SELECT count(*) AS n FROM scoped_observations",
            workspace_id=uuid4(), allowed_source_ids=[uuid4()], max_rows=5,
        )
        assert [dict(row) for row in rows] == [{"n": 0}]


async def test_session_user_is_the_login_role_and_cannot_switch_to_app_role(reader_engine: AsyncEngine) -> None:
    """``session_user`` is the dedicated login and ``SET ROLE``/``RESET ROLE`` cannot reach the app role."""
    async with reader_engine.connect() as conn:
        assert (await conn.execute(text("SELECT session_user"))).scalar_one() == CHAT_SQL_LOGIN_ROLE
        for stmt in (f"SET ROLE {_APP_ROLE}", "SET ROLE umwelt_chat_sql_reader"):
            with pytest.raises(DBAPIError):
                await conn.execute(text(stmt))
            await conn.rollback()
        await conn.execute(text("RESET ROLE"))
        assert (await conn.execute(text("SELECT current_user"))).scalar_one() == CHAT_SQL_LOGIN_ROLE


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE chat_sql_probe (x int)",
        "INSERT INTO chat_sql.scoped_observations (id) VALUES (gen_random_uuid())",
        "DELETE FROM public.observations",
        "SELECT count(*) FROM public.chat_messages",
        "SELECT count(*) FROM public.observations",
        "SELECT lo_from_bytea(0, 'x'::bytea)",
    ],
)
async def test_writes_and_base_table_reads_fail_even_after_leaving_read_only(
    reader_engine: AsyncEngine, sql: str,
) -> None:
    """Default read-only plus missing privileges deny writes and base-table reads, even with READ WRITE requested."""
    async with reader_engine.connect() as conn:
        with pytest.raises(DBAPIError):
            await conn.execute(text("SET TRANSACTION READ WRITE"))
            await conn.execute(text(sql))
        await conn.rollback()
        assert (await conn.execute(text("SHOW default_transaction_read_only"))).scalar_one() == "on"


async def test_validator_bypass_is_denied_by_the_database(reader_engine: AsyncEngine) -> None:
    """Base tables are unreadable through the tool's own execution path."""
    with pytest.raises(DBAPIError):
        await _run_sql_as_reader(
            "SELECT count(*) AS n FROM public.chat_messages",
            workspace_id=uuid4(), allowed_source_ids=[uuid4()], max_rows=5,
        )
