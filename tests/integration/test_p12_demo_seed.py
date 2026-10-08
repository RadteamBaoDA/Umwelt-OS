"""P12 release acceptance: the explicit fictional demo seed is idempotent and never resurrects rows."""

import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from modules.goals.seed import GOAL_SEEDS, _ws_id
from modules.knowledge.documents.seed import SOURCE_ID

pytestmark = pytest.mark.skipif(
    os.getenv("BBD_INTEGRATION") != "1", reason="requires disposable Compose test services"
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
# Tables the running worker/API legitimately mutate on their own; the seed does not own them.
WORKER_OWNED = {
    "event_outbox", "ingestion_runs", "ingestion_stages", "ingestion_batches", "realtime_replay_records",
    "realtime_replay_head", "realtime_replay_events", "auth_session", "backup_activity", "p12_backup_activity", "index_generations", "search_index_items",
    "entity_extraction_work", "timeline_extraction_work", "temporal_changes", "temporal_operations",
    "temporal_dispatches", "temporal_receipts", "news_recovery_checkpoints",
    "temporal_mappings", "temporal_supports", "automation_cursors", "news_stories", "news_story_identities", "news_observations",
}
# Seed-owned tables whose rows only the owner (or the seed) may change; compared by content digest.
DIGEST_TABLES = (
    "goals", "tasks", "automations", "automation_revisions", "automation_triggers",
    "automation_schedules", "demo_seed_receipts",
)
DIGEST_SQL = (
    "SELECT count(*), coalesce(md5(string_agg(t::text, E'\n' ORDER BY t::text)), '') FROM \"{}\" t"
)
REPORT = re.compile(
    r"Demo seed: created=(\d+), existing=(\d+), p08_seeded=(True|False), "
    r"p12_seeded=(True|False), skipped=(\d+)"
)


def _run_seed() -> tuple[int, int, bool, bool, int]:
    """Run the documented CLI (`python -m modules.knowledge.documents.seed`) against the disposable DB."""
    environment = {
        **os.environ,
        "DATABASE_URL": os.environ["TEST_DATABASE_URL"],
        "REDIS_URL": "redis://127.0.0.1:1/0",  # the seed never contacts Redis
    }
    result = subprocess.run(
        [sys.executable, "-m", "modules.knowledge.documents.seed"],
        cwd=REPOSITORY_ROOT, env=environment, capture_output=True, text=True, check=False, timeout=300,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    match = REPORT.search(result.stdout)
    assert match, result.stdout
    created, existing, p08, p12, skipped = match.groups()
    return int(created), int(existing), p08 == "True", p12 == "True", int(skipped)


async def _scalar(engine: AsyncEngine, sql: str, **parameters: object) -> int:
    async with engine.connect() as connection:
        return int(await connection.scalar(text(sql), parameters) or 0)


async def _digests(engine: AsyncEngine) -> dict[str, tuple[int, str]]:
    """Return (row count, order-independent md5 of every row's text) per seed-owned table."""
    async with engine.connect() as connection:
        result = {}
        for table in DIGEST_TABLES:
            row = (await connection.execute(text(DIGEST_SQL.format(table)))).one()
            result[table] = (row[0], row[1])
        return result


async def _counts(engine: AsyncEngine) -> dict[str, int]:
    async with engine.connect() as connection:
        tables = [row[0] for row in (await connection.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        ))).all()]
        counts = {}
        for table in tables:
            if table in WORKER_OWNED or table == "alembic_version":
                continue
            counts[table] = await connection.scalar(text(f'SELECT count(*) FROM "{table}"')) or 0
        return counts


async def test_demo_seed_twice_creates_no_duplicates_and_never_resurrects_deleted_rows(
    ready_owner_client: AsyncClient, committed_engine: AsyncEngine,
) -> None:
    before = await _counts(committed_engine)
    source_present = await _scalar(
        committed_engine, "SELECT count(*) FROM sources WHERE id = :id", id=SOURCE_ID)
    created, _, p08_seeded, p12_seeded, _ = _run_seed()
    first = await _counts(committed_engine)
    if before["demo_seed_receipts"] == 0:  # fresh disposable database: the first run really seeds
        assert created > 0 and p08_seeded and p12_seeded
        grew = {name for name in first if first[name] > before[name]}
        assert {"demo_seed_receipts"} <= grew
        if not source_present:
            assert {"sources", "documents", "document_versions"} <= grew
        assert first["demo_seed_receipts"] - before["demo_seed_receipts"] == 3  # P08, P10, P12
    else:  # re-run against an already seeded database: nothing may change
        assert (created, p08_seeded, p12_seeded) == (0, False, False) and first == before

    created_again, existing_again, p08_again, p12_again, _ = _run_seed()
    second = await _counts(committed_engine)
    assert (created_again, p08_again, p12_again) == (0, False, False)
    assert existing_again > 0
    assert second == first  # same row counts in every seed-owned table

    async with committed_engine.connect() as connection:
        duplicates = (await connection.execute(text(
            "SELECT source_id, external_id, count(*) FROM documents WHERE external_id IS NOT NULL "
            "GROUP BY source_id, external_id HAVING count(*) > 1"
        ))).all()
    assert duplicates == []

    # Reset semantics: an owner-deleted demo goal stays deleted and an owner edit to another demo
    # goal survives, so re-seeding neither resurrects nor overwrites (compared by content digest).
    async with committed_engine.connect() as connection:
        workspace = SimpleNamespace(workspace_id=await connection.scalar(text("SELECT id FROM workspaces LIMIT 1")))
    # Demo goal IDs are workspace-derived since W1/W2, so the same fixture never collides across workspaces.
    deleted_goal, edited_goal = (str(_ws_id(workspace, seed["id"])) for seed in GOAL_SEEDS[:2])
    current = await ready_owner_client.get(f"/api/v1/goals/{deleted_goal}")
    if current.status_code == 200:  # already deleted by an earlier run on a reused database
        removed = await ready_owner_client.delete(
            f"/api/v1/goals/{deleted_goal}", params={"expected_revision": current.json()["revision"]})
        assert removed.status_code == 204
    other = await ready_owner_client.get(f"/api/v1/goals/{edited_goal}")
    assert other.status_code == 200, (other.status_code, other.text)
    edited = await ready_owner_client.patch(
        f"/api/v1/goals/{edited_goal}",
        json={"title": "Owner-edited demo goal", "expected_revision": other.json()["revision"]},
    )
    assert edited.status_code == 200, edited.text

    settled = await _digests(committed_engine)
    _run_seed()
    assert await _digests(committed_engine) == settled
    assert (await ready_owner_client.get(f"/api/v1/goals/{deleted_goal}")).status_code == 404
    kept = await ready_owner_client.get(f"/api/v1/goals/{edited_goal}")
    assert kept.json()["title"] == "Owner-edited demo goal"
