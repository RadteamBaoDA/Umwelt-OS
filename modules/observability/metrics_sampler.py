"""Periodic Prometheus samplers for shared state: arq queue depth/age and event-outbox backlog/lag.

Run by the API process only (see ``apps.api.main``) so replicas do not each multiply cluster-wide
numbers. Read-only, bounded (one aggregate query, three Redis calls per queue), content-free.

The outbox read is a narrow projection of ``event_outbox`` (status and ``created_at`` aggregate
only, no payload, no workspace or user identity); it is never used for writes.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.metrics import OUTBOX_LAG, OUTBOX_PENDING, Sampler, sample_arq_queues

# Rows still owed to consumers: not yet handed to a queue ('pending') and handed over but not finished ('queued').
_OUTBOX_BACKLOG_SQL = text(
    "SELECT count(*) AS pending, min(created_at) AS oldest FROM event_outbox WHERE status IN ('pending', 'queued')"
)


async def sample_outbox(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Set the outbox backlog count and the age of the oldest undelivered row.

    Opens one short read-only session (no writes, no locks beyond the statement) and rolls it back
    on exit. Uses the partial index ``ix_event_outbox_dispatch_created`` for the aggregate.
    """
    async with session_factory() as session:
        row = (await session.execute(_OUTBOX_BACKLOG_SQL)).one()
    OUTBOX_PENDING.set(int(row.pending))
    oldest: datetime | None = row.oldest
    OUTBOX_LAG.set(max(0.0, (datetime.now(UTC) - oldest).total_seconds()) if oldest is not None else 0.0)


def build_api_samplers(
    redis: Any, session_factory: async_sessionmaker[AsyncSession], queues: Iterable[str],
) -> list[tuple[str, Sampler]]:
    """Return the named samplers the API metrics loop runs: ``arq_queues`` and ``outbox``."""
    queue_names = tuple(queues)

    async def queues_sampler() -> None:
        """Sample arq queue depth and oldest-job age from Redis."""
        await sample_arq_queues(redis, queue_names)

    async def outbox_sampler() -> None:
        """Sample outbox backlog from the database."""
        await sample_outbox(session_factory)

    return [("arq_queues", queues_sampler), ("outbox", outbox_sampler)]
