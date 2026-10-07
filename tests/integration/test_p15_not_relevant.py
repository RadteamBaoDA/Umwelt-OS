"""P15-T9 acceptance: hide/unhide through the interaction API and the dashboard projection (Compose harness)."""

import os
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.skipif(
    os.getenv("BBD_INTEGRATION") != "1", reason="requires disposable Compose test services"
)

PROJECTIONS = "/api/v1/documents/dashboard-projections"


@pytest.mark.asyncio
async def test_hide_excludes_from_feed_and_unhide_restores(
    ready_owner_client: AsyncClient, committed_engine: AsyncEngine,
) -> None:
    client = ready_owner_client
    source = await client.post("/api/v1/sources", json={"type": "manual", "name": f"hide {uuid4().hex[:8]}"})
    source.raise_for_status()
    source_id = source.json()["id"]
    created = await client.post("/api/v1/documents", json={
        "source_id": source_id, "title": "hide me", "content": f"content {uuid4().hex}",
        "external_id": f"p15-{uuid4().hex}",
    })
    created.raise_for_status()
    document_id = created.json()["id"]
    interaction = f"/api/v1/documents/{document_id}/versions/1/interaction"

    async def feed_ids(extra: str = "") -> list[str]:
        response = await client.get(f"{PROJECTIONS}?source_ids={source_id}{extra}")
        response.raise_for_status()
        return [item["document_id"] for item in response.json()["items"]]

    assert document_id in await feed_ids()
    hidden = await client.put(interaction, json={"dismissed": True})
    assert hidden.status_code == 200 and hidden.json()["dismissed_at"] is not None
    assert (await client.put(interaction, json={"dismissed": True})).status_code == 200  # idempotent
    assert document_id not in await feed_ids()
    assert document_id in await feed_ids("&include_dismissed=true")

    assert (await client.put(interaction, json={"dismissed": False})).json()["dismissed_at"] is None
    assert document_id in await feed_ids()
    async with committed_engine.connect() as connection:
        count = (await connection.execute(
            text("SELECT count(*) FROM document_interactions WHERE document_version_id IN "
                 "(SELECT id FROM document_versions WHERE document_id = :id)"), {"id": document_id},
        )).scalar_one()
    assert count == 0
