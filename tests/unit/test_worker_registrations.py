"""W4-jobs-c: the hourly collection-receipt purge is registered on the main worker."""
from __future__ import annotations

from apps.worker.main import WorkerSettings


def test_receipt_purge_cron_registered_hourly() -> None:
    jobs = [job for job in WorkerSettings.cron_jobs if "purge_collection_receipts" in repr(job)]
    assert len(jobs) == 1
    assert jobs[0].minute == 7 and jobs[0].hour is None  # type: ignore[attr-defined]
