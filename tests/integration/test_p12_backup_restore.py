"""P12 release acceptance: database backup/restore drill into a separate fresh database.

Scope and limit. The project's host tooling (``scripts/backup.py`` / ``scripts/restore.py``,
``modules/backup/host.py``) backs up and restores a *whole Compose deployment*: it reads the owner's
``.env``, needs age keys, stops and restarts the deployment's own services, snapshots the n8n/raw
volumes, and builds an isolated restore project. Running it here would operate on a deployment other
than the disposable test stack, so it is deliberately not invoked. This drill instead executes the
PostgreSQL commands built by that tooling's own ``pg_dump_command`` / ``pg_restore_command`` (so the
flags cannot drift) inside the disposable PostgreSQL container, restoring into a second database on
the same disposable server, then runs the same post-restore ``alembic upgrade head`` step, and proves
row-level, sequence and revision equality.
"""

import os
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from modules.backup.host import pg_dump_command, pg_restore_command

pytestmark = pytest.mark.skipif(
    os.getenv("BBD_INTEGRATION") != "1", reason="requires disposable Compose test services"
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
HEAD = ScriptDirectory.from_config(Config(str(REPOSITORY_ROOT / "alembic.ini"))).get_current_head()
SEQUENCE_SQL = (
    "SELECT schemaname || '.' || sequencename, last_value, "
    "pg_sequence_last_value((schemaname || '.' || sequencename)::regclass) IS NOT NULL "
    "FROM pg_sequences WHERE schemaname = 'public' ORDER BY 1"
)
DIGEST_SQL = (
    "SELECT count(*), coalesce(md5(string_agg(t::text, E'\\n' ORDER BY t::text)), '') FROM \"{}\" t"
)


def _postgres_container() -> str:
    """Resolve the disposable stack's PostgreSQL container from the port TEST_DATABASE_URL targets."""
    port = urlsplit(os.environ["TEST_DATABASE_URL"].replace("+asyncpg", "")).port
    result = subprocess.run(
        ["docker", "ps", "--filter", f"publish={port}", "--format", "{{.ID}} {{.Names}}"],
        capture_output=True, text=True, check=False, timeout=60,
    )
    lines = [line.split() for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one container publishing {port}: {result.stdout!r}"
    container, name = lines[0]
    assert name.startswith("bbd-os-test-") and "postgres" in name, f"refusing container {name}"
    return container


def _docker_exec(
    container: str, *command: str, stdin: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["docker", "exec", *(["-i"] if stdin is not None else []), container, *command],
        input=stdin, capture_output=True, check=False, timeout=600,
    )


def _alembic_upgrade_head(database_url: str) -> None:
    upgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=REPOSITORY_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True, text=True, check=False, timeout=600,
    )
    assert upgrade.returncode == 0, upgrade.stderr[-1000:]


async def _digests(connection: AsyncConnection) -> dict[str, tuple[int, str]]:
    """Return (row count, order-independent md5 of every row's text) for every public table."""
    tables = [row[0] for row in (await connection.execute(text(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    ))).all()]
    result: dict[str, tuple[int, str]] = {}
    for table in tables:
        row = (await connection.execute(text(DIGEST_SQL.format(table)))).one()
        result[table] = (row[0], row[1])
    return result


async def _sequences(connection: AsyncConnection) -> list[tuple[object, ...]]:
    """Return each sequence's last value and whether it was ever called, so setval drift is caught."""
    return [tuple(row) for row in (await connection.execute(text(SEQUENCE_SQL))).all()]


async def _create_marker_rows(client: AsyncClient) -> str:
    """Create known user data through the real API so the drill never compares only empty tables."""
    marker = uuid4().hex
    source = await client.post("/api/v1/sources", json={"type": "manual", "name": f"drill {marker}"})
    source.raise_for_status()
    for number in range(2):
        document = await client.post("/api/v1/documents", json={
            "source_id": source.json()["id"], "title": f"drill note {number} {marker}",
            "content": f"Fictional backup drill content {number} {marker}.",
            "external_id": f"drill-{marker}-{number}",
        })
        document.raise_for_status()
    return marker


async def test_database_backup_restores_into_fresh_database_with_row_level_equality(
    ready_owner_client: AsyncClient,
    scratch_database: Callable[[], Awaitable[str]],
    tmp_path: Path,
) -> None:
    marker = await _create_marker_rows(ready_owner_client)
    container = _postgres_container()

    # Dump and digest the SAME exported snapshot, so concurrent worker writes cannot cause drift.
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    try:
        async with engine.connect() as connection:
            await connection.execute(text("BEGIN ISOLATION LEVEL REPEATABLE READ"))
            snapshot = await connection.scalar(text("SELECT pg_export_snapshot()"))
            dump = _docker_exec(
                container, *pg_dump_command("bbd_test", "bbd_test", f"--snapshot={snapshot}"),
            )
            assert dump.returncode == 0, dump.stderr.decode()[-1000:]
            expected = await _digests(connection)
            expected_sequences = await _sequences(connection)
            expected_version = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).all()
            await connection.rollback()
    finally:
        await engine.dispose()
    archive = tmp_path / "database.dump"
    archive.write_bytes(dump.stdout)
    assert archive.stat().st_size > 10_000
    assert expected["documents"][0] >= 2 and expected["owner"][0] == 1
    assert [row[0] for row in expected_version] == [HEAD]

    restored_url = await scratch_database()
    restored_name = make_url(restored_url).database
    assert restored_name and restored_name != "bbd_test"
    restore = _docker_exec(
        container, *pg_restore_command("bbd_test", restored_name), stdin=archive.read_bytes(),
    )
    assert restore.returncode == 0, restore.stderr.decode()[-1000:]
    # The same post-restore step the host tooling runs: migrate to head, which must be a no-op here.
    _alembic_upgrade_head(restored_url)

    restored_engine = create_async_engine(restored_url)
    try:
        async with restored_engine.connect() as connection:
            actual = await _digests(connection)
            actual_sequences = await _sequences(connection)
            versions = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).all()
            marker_rows = await connection.scalar(
                text("SELECT count(*) FROM documents WHERE title LIKE :pattern"),
                {"pattern": f"%{marker}"},
            )
    finally:
        await restored_engine.dispose()

    assert actual.keys() == expected.keys()
    mismatched = sorted(name for name in expected if actual[name] != expected[name])
    assert mismatched == [], f"restored rows differ in tables: {mismatched}"
    assert [row[0] for row in versions] == [HEAD]
    assert marker_rows == 2
    assert actual_sequences == expected_sequences


async def test_truncated_backup_makes_pg_restore_exit_nonzero(
    scratch_database: Callable[[], Awaitable[str]],
) -> None:
    """Only the restore command's non-zero exit is proven here; reporting lives in the host tooling."""
    container = _postgres_container()
    dump = _docker_exec(container, *pg_dump_command("bbd_test", "bbd_test"))
    assert dump.returncode == 0
    corrupt = dump.stdout[: len(dump.stdout) // 2]
    target = make_url(await scratch_database()).database
    assert target
    restore = _docker_exec(
        container, *pg_restore_command("bbd_test", target), stdin=corrupt,
    )
    assert restore.returncode != 0
