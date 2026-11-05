"""Regression for the P0 queue-routing bug: cron-dispatched ``process_*`` jobs must reach the default worker.

The outbox dispatcher runs inside the cron worker, whose arq pool used to default to its own ``arq:cron`` queue,
so every ``process_*`` job it enqueued sat on a queue where no worker registered the function and outbox rows
stayed ``queued`` forever (fixed in ``core.worker_queues``; unit coverage in ``tests/unit/core``).

This test drives the real path end to end on the disposable Compose stack: the owner API deletes an empty Source
with its data, which publishes ``source.purge.requested`` through ``publish_event`` (the cheapest declared event
type, no model or network I/O), the cron worker dispatches it, and the main worker must consume it. The outbox row
must therefore leave ``pending``/``queued`` for ``delivered`` and the purge operation must succeed, all within a
bounded timeout well below the dispatcher's 30 s stale-reclaim window multiplied by a few retries.
"""

import asyncio
import os
import time
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.skipif(
    os.getenv("INTEGRATION") != "1", reason="requires disposable Compose test services"
)

# Event-driven wake plus one dispatcher tick normally finishes in seconds; a stuck queue never finishes.
CONSUME_TIMEOUT_SECONDS = 120


async def _outbox_status(engine: AsyncEngine, source_id: UUID) -> str | None:
    """Return the status of the ``source.purge.requested`` outbox row for ``source_id`` (None before it exists)."""
    async with engine.connect() as connection:
        return await connection.scalar(text(
            "SELECT status FROM event_outbox WHERE type = 'source.purge.requested' "
            "AND payload->>'source_id' = :id"
        ), {"id": str(source_id)})


async def test_cron_dispatched_job_is_consumed_by_default_worker(
    ready_owner_client: AsyncClient, committed_engine: AsyncEngine,
) -> None:
    """A published outbox event is dispatched by the cron worker and delivered by the default worker."""
    client, engine = ready_owner_client, committed_engine
    created = await client.post("/api/v1/sources", json={"type": "manual", "name": f"queue route {uuid4().hex[:8]}"})
    created.raise_for_status()
    source_id = UUID(created.json()["id"])

    queued = await client.delete(f"/api/v1/sources/{source_id}", params={"with_data": "true"})
    assert queued.status_code == 202

    deadline = time.monotonic() + CONSUME_TIMEOUT_SECONDS
    status: str | None = None
    while time.monotonic() < deadline:
        status = await _outbox_status(engine, source_id)
        if status == "delivered":
            break
        await asyncio.sleep(1)
    assert status == "delivered", (
        f"outbox row stayed {status!r}: process_* job was not consumed by the default worker "
        "(cron-dispatched jobs routed to a queue nobody serves?)"
    )

    # Downstream state changed too: the consumer ran, not merely an acknowledgement. Same deadline budget.
    operation: str | None = None
    while time.monotonic() < deadline:
        async with engine.connect() as connection:
            operation = (await connection.execute(text(
                "SELECT status FROM source_purge_operations WHERE source_id = :id"
            ), {"id": str(source_id)})).scalar_one_or_none()
        if operation == "succeeded":
            break
        await asyncio.sleep(1)
    assert operation == "succeeded", f"purge operation ended {operation!r}, expected 'succeeded'"
