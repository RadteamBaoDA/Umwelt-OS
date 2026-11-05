"""Periodic Prometheus samplers for shared state: arq queue depth/age and event-outbox backlog/lag.

Run by the API process only (see ``apps.api.main``) so replicas do not each multiply cluster-wide
numbers. Read-only, bounded (one aggregate query, three Redis calls per queue), content-free.

The outbox read is a narrow projection of ``event_outbox`` (status and ``created_at`` aggregate
only, no payload, no workspace or user identity); it is never used for writes.
"""

from __future__ import annotations

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

# Rows still owed to consumers: not yet handed to a queue ('pending') and handed over but not finished ('queued').
_OUTBOX_BACKLOG_SQL = text(
    "SELECT count(*) AS pending, min(created_at) AS oldest FROM event_outbox WHERE status IN ('pending', 'queued')"
)

# Mirrors the retention rule of ``modules.ingestion._purge.outbox_purge_statement`` (kept as literals: this
# module must not import ingestion internals). Stage fan-out types expire after 7 days, everything else after
# ``outbox_retention_days``. One equality predicate per status lets each count use its partial index
# (``ix_event_outbox_delivered_created`` / ``ix_event_outbox_failed_created``); the LIMIT bounds the scan.
_PURGE_STAGE_RETENTION = timedelta(days=7)
_PURGE_BACKLOG_CAP = 100_000


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


async def sample_outbox(session_factory: async_sessionmaker[AsyncSession], *, retention_days: int = 30) -> None:
    """Set the outbox backlog count, oldest undelivered age, estimated row total and purge backlog.

    Opens one short read-only session (no writes, no locks beyond the statements) and rolls it back
    on exit. Uses ``ix_event_outbox_dispatch_created`` for the aggregate and the per-status partial
    indexes for the purge backlog, which is capped at ``2 * _PURGE_BACKLOG_CAP`` rows.
    """
    now = datetime.now(UTC)
    async with session_factory() as session:
        row = (await session.execute(_OUTBOX_BACKLOG_SQL)).one()
        size = (await session.execute(_OUTBOX_SIZE_SQL, {
            "general_cutoff": now - timedelta(days=max(1, retention_days)),
            "stage_cutoff": now - _PURGE_STAGE_RETENTION,
        })).one()
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

    async def queues_sampler() -> None:
        """Sample arq queue depth and oldest-job age from Redis."""
        await sample_arq_queues(redis, queue_names)

    async def outbox_sampler() -> None:
        """Sample outbox backlog from the database."""
        await sample_outbox(session_factory, retention_days=outbox_retention_days)

    return [("arq_queues", queues_sampler), ("outbox", outbox_sampler)]
