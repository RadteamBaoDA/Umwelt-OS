"""P15-T6a acceptance: preview is read-only and usage lookup is owner-scoped (needs the Compose harness)."""

import os
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.skipif(
    os.getenv("BBD_INTEGRATION") != "1", reason="requires disposable Compose test services"
)


@pytest.mark.asyncio
async def test_preview_writes_no_notifications_and_rejects_unknown_topic(
    ready_owner_client: AsyncClient, committed_engine: AsyncEngine,
) -> None:
    client = ready_owner_client
    source = await client.post("/api/v1/sources", json={"type": "manual", "name": f"rules {uuid4().hex[:8]}"})
    source.raise_for_status()
    source_id = source.json()["id"]
    rule = {"id": str(uuid4()), "keywords": ["rates"], "severity": "warning", "notify": True}
    async with committed_engine.connect() as conn:
        before = (await conn.execute(text("select count(*) from notifications"))).scalar_one()
    ok = await client.post(
        "/api/v1/gadget-definitions/highlight-preview", json={"source_ids": [source_id], "rules": [rule], "days": 7}
    )
    assert ok.status_code == 200 and ok.json()["window_days"] == 7
    bad = await client.post(
        "/api/v1/gadget-definitions/highlight-preview",
        json={"source_ids": [source_id], "rules": [{**rule, "keywords": [], "topic_ids": [str(uuid4())]}]},
    )
    assert bad.status_code == 422
    async with committed_engine.connect() as conn:
        after = (await conn.execute(text("select count(*) from notifications"))).scalar_one()
    assert after == before


@pytest.mark.asyncio
async def test_usage_unknown_definition_is_404(owner_client: AsyncClient) -> None:
    response = await owner_client.get(f"/api/v1/gadget-definitions/{uuid4()}/usage")
    assert response.status_code == 404
