"""Migration acceptance: the 0001_init baseline applies, seeds and round-trips on a fresh disposable database."""

import os
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

pytestmark = pytest.mark.skipif(
    os.getenv("INTEGRATION") != "1", reason="requires disposable Compose test services"
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


HEAD = "0001_init"


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


async def test_init_upgrade_creates_seed_rows_and_v7_function(
    scratch_database: Callable[[], Awaitable[str]],
) -> None:
    url = await scratch_database()
    _alembic(url, "upgrade", "head")
    assert await _version(url) == [HEAD]
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM connector_admission_slots")) == 2
            assert await connection.scalar(text("SELECT count(*) FROM translation_admission_slots")) == 1
            assert await connection.scalar(text("SELECT count(*) FROM github_webhook_capacity")) == 1
            assert await connection.scalar(text("SELECT substr(uuid_generate_v7()::text, 15, 1) = '7'")) is True
    finally:
        await engine.dispose()


async def test_repeated_upgrade_head_is_idempotent(
    scratch_database: Callable[[], Awaitable[str]],
) -> None:
    url = await scratch_database()
    _alembic(url, "upgrade", "head")
    first = await _schema(url)
    _alembic(url, "upgrade", "head")
    assert await _version(url) == [HEAD]
    assert await _schema(url) == first


async def test_downgrade_wipes_schema_and_reupgrade_restores_identical_schema(
    scratch_database: Callable[[], Awaitable[str]],
) -> None:
    url = await scratch_database()
    _alembic(url, "upgrade", "head")
    at_head = await _schema(url)
    _alembic(url, "downgrade", "base")
    assert "owner" not in (await _schema(url))["tables"]  # type: ignore[operator]
    _alembic(url, "upgrade", "head")
    assert await _schema(url) == at_head
