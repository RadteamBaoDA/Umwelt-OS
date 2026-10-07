import os

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.skipif(
    os.getenv("BBD_INTEGRATION") != "1", reason="requires disposable Compose test services"
)


@pytest.mark.asyncio
async def test_kind_counts_follow_create_and_forget(owner_client: AsyncClient) -> None:
    before = (await owner_client.get("/api/v1/memories?limit=1")).json()
    created = []
    for kind in ("fact", "fact", "preference"):
        response = await owner_client.post("/api/v1/memories", json={"content": f"count {kind}", "type": kind})
        assert response.status_code == 201
        created.append(response.json()["id"])
    after = (await owner_client.get("/api/v1/memories?limit=1")).json()
    assert after["kind_counts"]["fact"] == before["kind_counts"].get("fact", 0) + 2
    assert after["total_count"] == (before["total_count"] or 0) + 3

    forget = await owner_client.post(f"/api/v1/memories/{created[0]}/forget", json={"reason": "test"})
    assert forget.status_code == 200
    final = (await owner_client.get("/api/v1/memories?limit=1")).json()
    assert final["kind_counts"]["fact"] == after["kind_counts"]["fact"] - 1
