import os
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from modules.memory.models import Memory

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
    for memory_id in created[1:]:  # leave the shared database as found
        assert (await owner_client.post(f"/api/v1/memories/{memory_id}/forget", json={"reason": "cleanup"})).status_code == 200


async def _visible(client: AsyncClient) -> list[dict]:
    items: list[dict] = []
    cursor = None
    while True:
        url = "/api/v1/memories?limit=100" + (f"&cursor={cursor}" if cursor else "")
        page = (await client.get(url)).json()
        items += page["items"]
        cursor = page["next_cursor"]
        if not cursor:
            return items


@pytest.mark.asyncio
async def test_counts_exclude_chat_memories_hidden_by_history_off(
    owner_client: AsyncClient, committed_engine: AsyncEngine,
) -> None:
    """A retained chat-derived copy is hidden while history is off; counts must equal the visible list."""
    hidden_id = uuid4()
    prior = (await owner_client.get("/api/v1/settings/memory-privacy")).json()
    async with AsyncSession(committed_engine, expire_on_commit=False) as session:
        ws_id, actor_id = (await session.execute(text("SELECT id, owner_user_id FROM workspaces ORDER BY created_at LIMIT 1"))).one()
        session.add(Memory(
            workspace_id=ws_id, actor_user_id=actor_id, id=hidden_id, content="hidden chat copy", memory_type="fact", is_manual=False,
            provenance={"conversation_id": str(uuid4()), "message_id": str(uuid4())},
        ))
        await session.commit()
    try:
        off = await owner_client.put(
            "/api/v1/settings/memory-privacy", json={**prior, "store_conversation_history": False},
        )
        assert off.status_code == 200
        page = (await owner_client.get("/api/v1/memories?limit=1")).json()
        visible = await _visible(owner_client)
        assert page["counts_capped"] is False
        assert page["total_count"] == len(visible)
        for kind in ("fact", "preference", "instruction"):
            assert page["kind_counts"].get(kind, 0) == len([i for i in visible if i["type"] == kind])
    finally:
        await owner_client.put("/api/v1/settings/memory-privacy", json=prior)
        async with AsyncSession(committed_engine) as session:
            await session.execute(delete(Memory).where(Memory.id == hidden_id))
            await session.commit()
