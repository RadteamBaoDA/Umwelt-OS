"""Integration proof that the chat SQL tool's dedicated login role is read-only and isolated from the app role.

Runs against the migrated disposable ``bbd_test`` database (migration 0017 plus
``python -m core.chat_sql_provision`` applying the password). The parser allowlist is bypassed on purpose
(``_run_sql_as_reader`` / raw reader connections) to prove the database layer holds on its own.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from modules.chat import db_query
from modules.chat.db_query import CHAT_SQL_LOGIN_ROLE, _run_sql_as_reader
from tests.integration.test_observation_history import POINT, _observation, _seed

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


async def test_scoped_query_runs_as_reader_and_returns_rows(
    reader_engine: AsyncEngine, ready_owner_client: AsyncClient, committed_engine: AsyncEngine,
) -> None:
    """The fence returns the seeded rows for the exact (workspace, source) pair and nothing for any other pair."""
    seed = await _seed(ready_owner_client, committed_engine)
    async with AsyncSession(committed_engine, expire_on_commit=False) as session:
        await _observation(session, seed, revision=1, observed_at=POINT, accepted_at=POINT,
                           version_id=seed.new_version_id, current=True)
        await session.commit()

    async def count(view: str, workspace_id: UUID, source_ids: list[UUID]) -> int:
        """Count ``view`` rows through the real reader path for one fence."""
        rows = await _run_sql_as_reader(
            f"SELECT count(*) AS n FROM {view}",  # noqa: S608  # test-local constant view name
            workspace_id=workspace_id, allowed_source_ids=source_ids, max_rows=5,
        )
        return int(dict(rows[0])["n"])

    try:
        for _ in range(2):  # second pass proves the cached pool is reusable
            assert await count("scoped_observations", seed.workspace_id, [seed.source_id]) == 1
            assert await count("scoped_observations", seed.workspace_id, [uuid4()]) == 0
            assert await count("scoped_observations", uuid4(), [seed.source_id]) == 0
        assert await count("scoped_documents", seed.workspace_id, [seed.source_id]) == 1
        assert await count("scoped_documents", seed.workspace_id, [uuid4()]) == 0
        assert await count("scoped_documents", uuid4(), [seed.source_id]) == 0
        docs = await _run_sql_as_reader(
            "SELECT content_excerpt, document_version_id FROM scoped_documents",
            workspace_id=seed.workspace_id, allowed_source_ids=[seed.source_id], max_rows=5,
        )
        assert [dict(row) for row in docs] == [{"content_excerpt": "v2", "document_version_id": seed.new_version_id}]
    finally:
        # Remove the committed observation rows; the API-created source/document stay (the harness resets the stack).
        async with AsyncSession(committed_engine) as session:
            for table in ("observations", "source_observations", "ingestion_batches"):
                await session.execute(text(f"DELETE FROM {table} WHERE source_id = :id"), {"id": seed.source_id})  # noqa: S608
            await session.commit()


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
