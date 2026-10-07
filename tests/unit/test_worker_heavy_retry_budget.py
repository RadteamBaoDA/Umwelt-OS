"""Every arq job that can lose `heavy_job_slot` must outlast the longest slot hold in retries."""

import re
from pathlib import Path

from apps.worker.main import HEAVY_SLOT_JOBS, WorkerSettings
from core.heavy_work import HEAVY_JOB_MAX_TRIES, HEAVY_RETRY_DEFER_SECONDS, MAX_OPERATION_SECONDS

ROOT = Path(__file__).resolve().parents[2]


def _heavy_slot_users() -> set[str]:
    names: set[str] = set()
    for path in (ROOT / "modules").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        names.update(re.findall(r"@bounded_heavy_work\s+(?:@.*\s+)*async def (\w+)", text))
        if "heavy_job_slot(" in text and path.name == "worker.py":
            names.update(re.findall(r"async def (process_graph_operation)\b", text))
    return names


def test_every_heavy_slot_job_is_covered_by_the_retry_budget() -> None:
    # index_pending_chunks is cron-only: a dropped cron id is unique per tick, so it cannot block re-enqueue.
    crons = {"index_pending_chunks"}
    assert _heavy_slot_users() - crons == HEAVY_SLOT_JOBS


def test_retry_budget_outlasts_maximum_heavy_slot_hold() -> None:
    registered = {getattr(f, "name", None): f for f in WorkerSettings.functions}  # type: ignore[attr-defined]
    assert HEAVY_SLOT_JOBS <= registered.keys()
    for name in HEAVY_SLOT_JOBS:
        assert registered[name].max_tries * HEAVY_RETRY_DEFER_SECONDS > MAX_OPERATION_SECONDS, name
    assert HEAVY_JOB_MAX_TRIES * HEAVY_RETRY_DEFER_SECONDS > MAX_OPERATION_SECONDS
