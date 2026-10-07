"""P15-T4 acceptance: language/since params on dashboard projections (needs the Compose harness)."""

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
async def test_invalid_language_and_since_rejected(owner_client: AsyncClient) -> None:
    source = uuid4()
    for extra in ("language=xx", "language=EN", "since=2001-01-01T00:00:00Z", "since=2026-01-01T00:00:00"):
        response = await owner_client.get(f"{PROJECTIONS}?source_ids={source}&{extra}")
        assert response.status_code == 422, extra


@pytest.mark.asyncio
async def test_valid_filters_return_empty_page_for_unknown_source(owner_client: AsyncClient) -> None:
    response = await owner_client.get(f"{PROJECTIONS}?source_ids={uuid4()}&language=vi")
    assert response.status_code == 200
    assert response.json()["items"] == []


@pytest.mark.asyncio
async def test_language_filter_matches_only_stored_language(
    ready_owner_client: AsyncClient, committed_engine: AsyncEngine,
) -> None:
    """Seed en/vi/NULL documents; `language=vi` returns only vi, no filter returns all three."""
    client = ready_owner_client
    source = await client.post("/api/v1/sources", json={"type": "manual", "name": f"feed {uuid4().hex[:8]}"})
    source.raise_for_status()
    source_id = source.json()["id"]
    ids: dict[str, str] = {}
    for name, language in (("en", "en"), ("vi", "vi"), ("none", None)):
        created = await client.post("/api/v1/documents", json={
            "source_id": source_id, "title": f"doc {name}", "content": f"content {name} {uuid4().hex}",
            "external_id": f"p15-{uuid4().hex}",
        })
        created.raise_for_status()
        ids[name] = created.json()["id"]
        async with committed_engine.begin() as connection:
            await connection.execute(
                text("UPDATE documents SET language = :language, extraction_status = 'ready' WHERE id = :id"),
                {"language": language, "id": ids[name]},
            )
    base = f"{PROJECTIONS}?source_ids={source_id}"
    vi = await client.get(f"{base}&language=vi")
    assert vi.status_code == 200
    assert {item["document_id"] for item in vi.json()["items"]} == {ids["vi"]}
    anything = await client.get(base)
    assert {item["document_id"] for item in anything.json()["items"]} >= set(ids.values())
