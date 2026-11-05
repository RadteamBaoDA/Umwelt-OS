"""Periodic Prometheus samplers for shared state: arq queue depth/age and event-outbox backlog/lag.

Run by the API process only (see ``apps.api.main``) so replicas do not each multiply cluster-wide
numbers. Read-only, bounded (one aggregate query, three Redis calls per queue), content-free.

The outbox read is a narrow projection of ``event_outbox`` (status and ``created_at`` aggregate
only, no payload, no workspace or user identity); it is never used for writes.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.metrics import (
    OUTBOX_LAG,
    OUTBOX_PENDING,
    OUTBOX_PURGE_BACKLOG,
    OUTBOX_ROWS_TOTAL,
    Sampler,
    sample_arq_queues,
)
from modules.ingestion.public import OUTBOX_RETENTION

# Rows still owed to consumers: not yet handed to a queue ('pending') and handed over but not finished ('queued').
_OUTBOX_BACKLOG_SQL = text(
    "SELECT count(*) AS pending, min(created_at) AS oldest FROM event_outbox WHERE status IN ('pending', 'queued')"
)

# Mirrors the retention rule of ``modules.ingestion._purge.outbox_purge_statement``: stage fan-out types expire
# after ``OUTBOX_RETENTION`` (imported from the ingestion public contract), everything else after
# ``outbox_retention_days``. One equality predicate per status lets each count use its partial index
# (``ix_event_outbox_delivered_created`` / ``ix_event_outbox_failed_created``). The LIMIT only caps the counted
# rows: when nothing matches, the stage-type branch can still scan every delivered/failed row older than the
# stage cutoff, which is why the size query runs at most every ``_SIZE_SAMPLE_INTERVAL_SECONDS``.
_PURGE_BACKLOG_CAP = 100_000
# The purge job runs hourly (limit 1000 rows per pass), so rows just past retention are normally waiting for
# the next pass. A 2h grace (>= one purge interval) keeps a healthy system at a backlog of 0 and leaves the
# ``UmweltOutboxPurgeBacklog`` alert for a purge that is genuinely falling behind.
_PURGE_GRACE = timedelta(hours=2)
# Backlog/size are slow-moving; re-running the scan every 15s tick per API process is wasted load.
_SIZE_SAMPLE_INTERVAL_SECONDS = 300.0


def _backlog_branch(status: str) -> str:
    """Return the bounded count subquery for one terminal status (status is a trusted literal)."""
    return (
        f"(SELECT count(*) FROM (SELECT 1 FROM event_outbox WHERE status = '{status}' AND ("  # noqa: S608
        "created_at < :general_cutoff OR "
        "(type IN ('ingestion.stage.requested', 'ingestion.normalize.requested') AND created_at < :stage_cutoff)"
        f") LIMIT {_PURGE_BACKLOG_CAP}) AS b_{status})"
    )


# reltuples is -1 on a never-analyzed table, hence the clamp in Python; it is an estimate by design (no count(*)).
_OUTBOX_SIZE_SQL = text(
    "SELECT (SELECT reltuples::bigint FROM pg_class WHERE oid = 'event_outbox'::regclass) AS rows_total, "  # noqa: S608 - trusted literals
    f"{_backlog_branch('delivered')} + {_backlog_branch('failed')} AS backlog"
)


async def sample_outbox(
    session_factory: async_sessionmaker[AsyncSession], *, retention_days: int = 30, include_size: bool = True,
) -> None:
    """Set the outbox pending count and lag and, when ``include_size``, the row total and purge backlog.

    Opens one short read-only session (no writes, no locks beyond the statements) and rolls it back
    on exit. Uses ``ix_event_outbox_dispatch_created`` for the aggregate and the per-status partial
    indexes for the purge backlog (counted rows capped at ``2 * _PURGE_BACKLOG_CAP``). With
    ``include_size=False`` the size/backlog gauges keep their last values and only the cheap aggregate runs.
    """
    now = datetime.now(UTC)
    async with session_factory() as session:
        row = (await session.execute(_OUTBOX_BACKLOG_SQL)).one()
        size = None
        if include_size:
            size = (await session.execute(_OUTBOX_SIZE_SQL, {
                "general_cutoff": now - timedelta(days=max(1, retention_days)) - _PURGE_GRACE,
                "stage_cutoff": now - OUTBOX_RETENTION - _PURGE_GRACE,
            })).one()
    if size is not None:
        OUTBOX_ROWS_TOTAL.set(max(0, int(size.rows_total or 0)))
        OUTBOX_PURGE_BACKLOG.set(int(size.backlog or 0))
    OUTBOX_PENDING.set(int(row.pending))
    oldest: datetime | None = row.oldest
    OUTBOX_LAG.set(max(0.0, (datetime.now(UTC) - oldest).total_seconds()) if oldest is not None else 0.0)


def build_api_samplers(
    redis: Any, session_factory: async_sessionmaker[AsyncSession], queues: Iterable[str],
    *, outbox_retention_days: int = 30,
) -> list[tuple[str, Sampler]]:
    """Return the named samplers the API metrics loop runs: ``arq_queues`` and ``outbox``."""
    queue_names = tuple(queues)
    last_size_at = float("-inf")  # monotonic time of the last size/backlog query; per-sampler-set closure state

    async def queues_sampler() -> None:
        """Sample arq queue depth and oldest-job age from Redis."""
        await sample_arq_queues(redis, queue_names)

    async def outbox_sampler() -> None:
        """Sample outbox pending/lag every tick and the size/backlog scan at most every five minutes."""
        nonlocal last_size_at
        due = time.monotonic() - last_size_at >= _SIZE_SAMPLE_INTERVAL_SECONDS
        await sample_outbox(session_factory, retention_days=outbox_retention_days, include_size=due)
        if due:  # only after success, so a failed query is retried on the next tick
            last_size_at = time.monotonic()

    return [("arq_queues", queues_sampler), ("outbox", outbox_sampler)]
