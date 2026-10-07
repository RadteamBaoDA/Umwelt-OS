"""Regression: the graph worker must evaluate its source dependency without crashing.

``_dependency_fingerprint`` read ``local_only`` from the connector-source projection, which has no
such field, so every graph operation for an ingested document raised AttributeError and was retried
forever instead of being parked with its real reason (graph disabled in the test stack).
"""

import asyncio
import os
import time
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.skipif(
    os.getenv("BBD_INTEGRATION") != "1", reason="requires disposable Compose test services"
)


async def test_graph_operation_for_new_document_is_parked_with_graph_disabled(
    ready_owner_client: AsyncClient, committed_engine: AsyncEngine,
) -> None:
    source = await ready_owner_client.post(
        "/api/v1/sources", json={"type": "manual", "name": f"graph {uuid4().hex[:8]}"})
    source.raise_for_status()
    source_id = source.json()["id"]
    document = await ready_owner_client.post("/api/v1/documents", json={
        "source_id": source_id, "title": "Graph note", "content": "Fictional graph content.",
        "external_id": f"graph-{uuid4().hex}",
    })
    document.raise_for_status()

    deadline = time.monotonic() + 120
    states: list[tuple[str, str | None]] = []
    while time.monotonic() < deadline:
        async with committed_engine.connect() as connection:
            states = [tuple(row) for row in (await connection.execute(text(
                "SELECT o.status, o.error_code FROM temporal_operations o "
                "JOIN temporal_partitions p ON p.id = o.partition_id WHERE p.source_id = :id"
            ), {"id": source_id})).all()]
        if states and all(row == ("blocked", "graph_disabled") for row in states):
            return
        await asyncio.sleep(3)
    pytest.fail(f"graph operations were not parked as graph_disabled: {states}")
