"""P12 release acceptance: every P12 migration round-trips on a fresh disposable database."""

import os
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

pytestmark = pytest.mark.skipif(
    os.getenv("BBD_INTEGRATION") != "1", reason="requires disposable Compose test services"
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _p12_revisions() -> tuple[str, list[str]]:
    """Return (head, P12 revision ids) and the pre-P12 parent, derived from the migration scripts."""
    script = ScriptDirectory.from_config(Config(str(REPOSITORY_ROOT / "alembic.ini")))
    head = script.get_current_head()
    assert head is not None
    chain: list[str] = []
    revision = script.get_revision(head)
    # Skip later (non-P12) revisions above the P12 chain so new heads do not break collection.
    while revision is not None and not revision.revision.startswith("p12_"):
        parent = revision.down_revision
        assert isinstance(parent, str), "revisions above P12 form a linear chain"
        revision = script.get_revision(parent)
    while revision is not None and revision.revision.startswith("p12_"):
        chain.append(revision.revision)
        parent = revision.down_revision
        assert isinstance(parent, str), "P12 revisions form a linear chain"
        revision = script.get_revision(parent)
    assert revision is not None
    return revision.revision, chain


PRE_P12, P12_CHAIN = _p12_revisions()
HEAD = str(ScriptDirectory.from_config(Config(str(REPOSITORY_ROOT / "alembic.ini"))).get_current_head())


def _alembic(database_url: str, *arguments: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=REPOSITORY_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True, text=True, check=False, timeout=600,
    )
    assert result.returncode == 0, f"alembic {' '.join(arguments)} failed:\n{result.stderr}"
    return result.stdout + result.stderr


async def _schema(database_url: str) -> dict[str, object]:
    """Capture an exact structural schema fingerprint from the PostgreSQL catalogs."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return await _schema_of(connection)
    finally:
        await engine.dispose()


async def _schema_of(connection: AsyncConnection) -> dict[str, object]:
    def tables(sync_connection: object) -> list[str]:
        return sorted(inspect(sync_connection).get_table_names())  # type: ignore[arg-type]

    async def rows(sql: str) -> list[tuple[object, ...]]:
        return [tuple(row) for row in (await connection.execute(text(sql))).all()]

    return {
        "tables": await connection.run_sync(tables),
        "columns": await rows(
            "SELECT table_name, column_name, data_type, udt_name, is_nullable, column_default, "
            "character_maximum_length FROM information_schema.columns WHERE table_schema = 'public' "
            "ORDER BY table_name, column_name"
        ),
        "indexes": await rows(
            "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' "
            "ORDER BY tablename, indexname"
        ),
        "enums": await rows(
            "SELECT t.typname, array_agg(e.enumlabel ORDER BY e.enumsortorder) FROM pg_type t "
            "JOIN pg_enum e ON e.enumtypid = t.oid JOIN pg_namespace n ON n.oid = t.typnamespace "
            "WHERE n.nspname = 'public' GROUP BY t.typname ORDER BY 1"
        ),
        "functions": await rows(
            "SELECT p.proname, pg_get_functiondef(p.oid) FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' "
            "AND p.prokind = 'f' ORDER BY 1, 2"
        ),
        "triggers": await rows(
            "SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid) FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND NOT t.tgisinternal ORDER BY 1, 2"
        ),
        "sequences": await rows(
            "SELECT sequencename, data_type::text, start_value, increment_by, max_value, "
            "min_value, cycle FROM pg_sequences WHERE schemaname = 'public' ORDER BY 1"
        ),
        "views": await rows(
            "SELECT viewname, definition FROM pg_views WHERE schemaname = 'public' ORDER BY 1"
        ),
        "constraints": await rows(
            "SELECT c.conrelid::regclass::text, c.conname, pg_get_constraintdef(c.oid) "
            "FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace "
            "WHERE n.nspname = 'public' ORDER BY 1, 2"
        ),
    }


async def _version(database_url: str) -> list[str]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text("SELECT version_num FROM alembic_version"))
            return sorted(row[0] for row in result.all())
    finally:
        await engine.dispose()


def _run_demo_seed(database_url: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "modules.knowledge.documents.seed"], cwd=REPOSITORY_ROOT,
        env={**os.environ, "DATABASE_URL": database_url, "REDIS_URL": "redis://127.0.0.1:1/0"},
        capture_output=True, text=True, check=False, timeout=300,
    )
    assert result.returncode == 0, result.stderr[-2000:]


async def _seed_rows(url: str) -> dict[str, int]:
    """Create owner-scoped demo data plus rows in P12-added tables; return pre-P12 table row counts."""
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(
                "INSERT INTO owner (id, password_hash) VALUES (1, 'not-a-real-hash')"
            ))
        _run_demo_seed(url)
        async with engine.begin() as connection:
            await connection.execute(text(
                "INSERT INTO document_cleanup_operations (id, source_id, document_id) "
                "VALUES (gen_random_uuid(), gen_random_uuid(), gen_random_uuid())"
            ))
            await connection.execute(text(
                "INSERT INTO onboarding_state (owner_id) SELECT id FROM owner LIMIT 1"
            ))
            return {
                table: int(await connection.scalar(text(f"SELECT count(*) FROM {table}")) or 0)
                for table in ("documents", "document_versions", "goals", "sources")
            }
    finally:
        await engine.dispose()


async def test_all_p12_revisions_downgrade_and_reupgrade_to_identical_schema(
    scratch_database: Callable[[], Awaitable[str]],
) -> None:
    assert len(P12_CHAIN) == 10, P12_CHAIN  # every P12 revision is in scope, none silently skipped
    url = await scratch_database()
    _alembic(url, "upgrade", "head")
    assert await _version(url) == [HEAD]
    at_head = await _schema(url)

    _alembic(url, "downgrade", PRE_P12)
    assert await _version(url) == [PRE_P12]
    downgraded = await _schema(url)
    # The downgrade must really remove schema, otherwise the round trip proves nothing.
    assert downgraded != at_head
    assert "document_cleanup_operations" in at_head["tables"]  # type: ignore[operator]
    assert "document_cleanup_operations" not in downgraded["tables"]  # type: ignore[operator]

    _alembic(url, "upgrade", "head")
    assert await _version(url) == [HEAD]
    assert await _schema(url) == at_head


async def test_all_p12_revisions_round_trip_preserves_existing_data(
    scratch_database: Callable[[], Awaitable[str]],
) -> None:
    url = await scratch_database()
    _alembic(url, "upgrade", "head")
    before = await _seed_rows(url)
    assert before["documents"] > 0 and before["goals"] > 0, before
    at_head = await _schema(url)

    _alembic(url, "downgrade", PRE_P12)
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            retained = {
                table: int(await connection.scalar(text(f"SELECT count(*) FROM {table}")) or 0)
                for table in before
            }
    finally:
        await engine.dispose()
    assert retained == before  # data of pre-P12 tables survives the P12 downgrade

    _alembic(url, "upgrade", "head")
    assert await _version(url) == [HEAD]
    assert await _schema(url) == at_head


async def test_repeated_upgrade_head_is_idempotent(
    scratch_database: Callable[[], Awaitable[str]],
) -> None:
    url = await scratch_database()
    _alembic(url, "upgrade", "head")
    first = await _schema(url)
    _alembic(url, "upgrade", "head")
    assert await _version(url) == [HEAD]
    assert await _schema(url) == first
