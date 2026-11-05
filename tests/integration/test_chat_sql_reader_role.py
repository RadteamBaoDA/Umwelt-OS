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
            app_engine=committed_engine, workspace_id=workspace_id, allowed_source_ids=source_ids, max_rows=5,
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
            app_engine=committed_engine, workspace_id=seed.workspace_id, allowed_source_ids=[seed.source_id],
            max_rows=5,
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
        "SELECT lo_create(0)",
        "CREATE TEMP TABLE t (x int)",
        "SELECT pg_advisory_lock(1)",
        "SELECT pg_notify('x', 'y')",
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


async def test_validator_bypass_is_denied_by_the_database(
    reader_engine: AsyncEngine, committed_engine: AsyncEngine,
) -> None:
    """Base tables are unreadable through the tool's own execution path."""
    with pytest.raises(DBAPIError):
        await _run_sql_as_reader(
            "SELECT count(*) AS n FROM public.chat_messages",
            app_engine=committed_engine, workspace_id=uuid4(), allowed_source_ids=[uuid4()], max_rows=5,
        )


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO chat_sql.query_fence (backend_pid, workspace_id, source_ids, expires_at) "
        "VALUES (1, gen_random_uuid(), '{}', now() + interval '1 hour')",
        "UPDATE chat_sql.query_fence SET workspace_id = gen_random_uuid()",
        "DELETE FROM chat_sql.query_fence",
        "SELECT * FROM chat_sql.query_fence",
    ],
)
async def test_reader_has_no_privilege_on_the_fence_table(reader_engine: AsyncEngine, sql: str) -> None:
    """The reader cannot read or write the fence, so it cannot retarget it."""
    async with reader_engine.connect() as conn:
        with pytest.raises(DBAPIError):
            await conn.execute(text(sql))
        await conn.rollback()


async def test_guc_and_foreign_pid_fence_rows_do_not_widen_the_views(
    reader_engine: AsyncEngine, ready_owner_client: AsyncClient, committed_engine: AsyncEngine,
) -> None:
    """``set_config`` from the reader changes nothing, and a fence row for another pid yields zero rows."""
    seed = await _seed(ready_owner_client, committed_engine)
    other_workspace = uuid4()
    async with AsyncSession(committed_engine, expire_on_commit=False) as session:
        await _observation(session, seed, revision=1, observed_at=POINT, accepted_at=POINT,
                           version_id=seed.new_version_id, current=True)
        await session.commit()
    try:
        async with reader_engine.connect() as conn:
            pid = (await conn.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            await conn.rollback()
            async with committed_engine.begin() as app:  # fence belongs to a DIFFERENT pid
                await app.execute(
                    text("INSERT INTO chat_sql.query_fence VALUES (:pid, :ws, CAST(:src AS uuid[]), "
                         "clock_timestamp() + interval '30 seconds') ON CONFLICT (backend_pid) DO NOTHING"),
                    {"pid": pid + 1_000_000, "ws": seed.workspace_id, "src": [seed.source_id]},
                )
            for name, value in (("umwelt.chat_sql_workspace", str(seed.workspace_id)),
                                ("umwelt.chat_sql_sources", str(seed.source_id))):
                await conn.execute(text("SELECT set_config(:n, :v, true)"), {"n": name, "v": value})
            for view in ("scoped_observations", "scoped_documents"):
                n = (await conn.execute(text(f"SELECT count(*) FROM {view}"))).scalar_one()  # noqa: S608
                assert n == 0
            await conn.rollback()
        # With a real fence for another workspace the seeded rows stay invisible.
        rows = await _run_sql_as_reader(
            "SELECT count(*) AS n FROM scoped_observations",
            app_engine=committed_engine, workspace_id=other_workspace, allowed_source_ids=[seed.source_id], max_rows=5,
        )
        assert dict(rows[0])["n"] == 0
    finally:
        async with AsyncSession(committed_engine) as session:
            await session.execute(text("DELETE FROM chat_sql.query_fence"))
            for table in ("observations", "source_observations", "ingestion_batches"):
                await session.execute(text(f"DELETE FROM {table} WHERE source_id = :id"), {"id": seed.source_id})  # noqa: S608
            await session.commit()


async def test_app_role_keeps_advisory_locks_and_temp_tables(committed_engine: AsyncEngine) -> None:
    """Migration 0019 revokes PUBLIC grants but the application role still locks and creates temp tables."""
    async with committed_engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(1)"))
        await conn.execute(text("CREATE TEMP TABLE t_0019 (x int) ON COMMIT DROP"))


async def test_legacy_reader_role_absent_and_app_group_holds_grants(
    committed_engine: AsyncEngine, reader_engine: AsyncEngine
) -> None:
    """0020: legacy role dropped; app user gets TEMP + advisory via umwelt_app; the reader gets neither."""
    async with committed_engine.connect() as conn:
        legacy = await conn.scalar(text("SELECT count(*) FROM pg_roles WHERE rolname = 'umwelt_chat_sql_reader'"))
        member = await conn.scalar(text("SELECT pg_has_role(current_user, 'umwelt_app', 'member')"))
        temp = await conn.scalar(text("SELECT has_database_privilege(current_user, current_database(), 'TEMPORARY')"))
        adv = await conn.scalar(text("SELECT has_function_privilege(current_user, 'pg_advisory_lock(bigint)', 'EXECUTE')"))
    assert legacy == 0 and member and temp and adv
    async with committed_engine.connect() as conn:
        attrs = (await conn.execute(text("SELECT rolcanlogin, rolsuper FROM pg_roles WHERE rolname = 'umwelt_app'"))).one()
        reader_member = await conn.scalar(text("SELECT pg_has_role('umwelt_chat_sql_login', 'umwelt_app', 'MEMBER')"))
    assert tuple(attrs) == (False, False) and reader_member is False
    async with reader_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT pg_has_role(current_user, 'umwelt_app', 'member'), "
                    "has_database_privilege(current_user, current_database(), 'TEMPORARY'), "
                    "has_function_privilege(current_user, 'pg_advisory_lock(bigint)', 'EXECUTE')"
                )
            )
        ).one()
    assert tuple(row) == (False, False, False)
