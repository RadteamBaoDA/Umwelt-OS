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
async def test_preview_matches_seeded_item_without_notifying_and_reports_dead_topic(
    ready_owner_client: AsyncClient, committed_engine: AsyncEngine,
) -> None:
    client = ready_owner_client
    source = await client.post("/api/v1/sources", json={"type": "manual", "name": f"rules {uuid4().hex[:8]}"})
    source.raise_for_status()
    source_id = source.json()["id"]
    created = await client.post("/api/v1/documents", json={
        "source_id": source_id, "title": "Rates rise", "content": f"central bank rates {uuid4().hex}",
        "external_id": f"p15-rules-{uuid4().hex}",
    })
    created.raise_for_status()
    async with committed_engine.begin() as conn:
        await conn.execute(
            text("UPDATE documents SET extraction_status = 'ready' WHERE id = :id"), {"id": created.json()["id"]},
        )
    rule = {"id": str(uuid4()), "keywords": ["rates"], "severity": "warning", "notify": True}
    async with committed_engine.connect() as conn:
        before = (await conn.execute(text("select count(*) from notifications"))).scalar_one()
    ok = await client.post(
        "/api/v1/gadget-definitions/highlight-preview", json={"source_ids": [source_id], "rules": [rule], "days": 7}
    )
    assert ok.status_code == 200 and ok.json()["window_days"] == 7
    assert ok.json()["scanned"] >= 1 and ok.json()["rules"][0]["match_count"] >= 1  # the seed is really scanned
    dead = str(uuid4())
    unresolved = await client.post(
        "/api/v1/gadget-definitions/highlight-preview",
        json={"source_ids": [source_id], "rules": [{**rule, "keywords": [], "topic_ids": [dead]}]},
    )
    assert unresolved.status_code == 200 and unresolved.json()["rules"][0]["unresolved_topic_ids"] == [dead]
    created_def = await client.post("/api/v1/gadget-definitions", json={
        "name": "bad topic", "renderer": "highlights", "source_ids": [source_id],
        "highlight_rules": [{**rule, "keywords": [], "topic_ids": [dead]}],
    })
    assert created_def.status_code == 422
    body = created_def.json()
    assert (body.get("error") or body["detail"])["code"] == "rule_topic_unknown"
    async with committed_engine.connect() as conn:
        after = (await conn.execute(text("select count(*) from notifications"))).scalar_one()
    assert after == before


@pytest.mark.asyncio
async def test_usage_unknown_definition_is_404(owner_client: AsyncClient) -> None:
    response = await owner_client.get(f"/api/v1/gadget-definitions/{uuid4()}/usage")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_rule_delivery_bounds_are_enforced_by_the_api(
    ready_owner_client: AsyncClient,
) -> None:
    client = ready_owner_client
    source = await client.post("/api/v1/sources", json={"type": "manual", "name": f"delivery {uuid4().hex[:8]}"})
    source.raise_for_status()
    rule = {"id": str(uuid4()), "keywords": ["rates"], "severity": "warning", "notify": True}
    base = {"name": "delivery", "renderer": "highlights", "source_ids": [source.json()["id"]]}
    too_long = await client.post("/api/v1/gadget-definitions", json={
        **base, "highlight_rules": [{**rule, "cooldown_minutes": 10081}],
    })
    assert too_long.status_code == 422
    ok = await client.post("/api/v1/gadget-definitions", json={
        **base, "highlight_rules": [{
            **rule, "cooldown_minutes": 60, "expires_at": "2099-01-01T00:00:00Z",
            "quiet_start": "22:00", "quiet_end": "07:00",
        }],
    })
    assert ok.status_code in (200, 201)
    stored = ok.json()["highlight_rules"][0]
    assert stored["cooldown_minutes"] == 60 and stored["quiet_start"] == "22:00"
