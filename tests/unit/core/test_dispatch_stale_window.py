"""Stale-claim window of the outbox dispatcher is per job: heavy-slot jobs must outlast slot contention."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.dialects import postgresql

from core.events import dispatch
from core.events.registry import get_event_registry
from core.heavy_work.slots import HEAVY_JOB_MAX_TRIES, MAX_OPERATION_SECONDS


def test_default_jobs_keep_short_reclaim_window() -> None:
    """Light jobs still recover a lost enqueue within the 30 s safety window."""
    assert (
        dispatch.stale_after_for("process_ingestion_event")
        == dispatch.DISPATCH_STALE_AFTER
        == timedelta(seconds=30)
    )


def test_heavy_jobs_outlast_worst_case_contention_and_hold() -> None:
    """A heavy job waits >= ~435 s of minimum backoff then holds the slot up to 150 s; reclaiming sooner voids its claim."""
    minimum_contention = 2.5 + 5 + 7.5 * (HEAVY_JOB_MAX_TRIES - 2)
    for job in ("process_uploaded_file", "process_graph_operation"):
        assert (
            dispatch.stale_after_for(job).total_seconds()
            > minimum_contention + MAX_OPERATION_SECONDS
        )


def test_stale_clause_uses_each_routes_own_window() -> None:
    """The compiled reclaim predicate carries a distinct cutoff for heavy event types and for the rest."""
    registry = get_event_registry()
    heavy_types = {
        t for t, r in registry.routes.items() if r.job_name in dispatch.HEAVY_DISPATCH_JOBS
    }
    assert "document.file.uploaded" in heavy_types
    from datetime import UTC, datetime

    now = datetime(2026, 1, 1, tzinfo=UTC)
    sql = str(dispatch.stale_queued_clause(now).compile(dialect=postgresql.dialect()))
    assert sql.count("event_outbox.dispatched_at <") == 2
    assert "event_outbox.type IN" in sql and "event_outbox.type NOT IN" in sql
