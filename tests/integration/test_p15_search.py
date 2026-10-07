"""P15 T2: conversation and event search over the live stack (privacy fences)."""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.skipif(
    os.getenv("BBD_INTEGRATION") != "1", reason="requires disposable Compose test services"
)


@pytest.mark.asyncio
async def test_conversation_search_hides_ephemeral_expired_and_blank(
    owner_client: AsyncClient, committed_engine: AsyncEngine,
) -> None:
    tag = uuid4().hex[:8]
    now = datetime.now(UTC)
    rows = [
        (uuid4(), f"visible {tag}", False, None),
        (uuid4(), f"ephemeral {tag}", True, now + timedelta(hours=1)),
        (uuid4(), f"expired {tag}", True, now - timedelta(hours=1)),
    ]
    async with committed_engine.begin() as connection:
        for row_id, title, ephemeral, expires_at in rows:
            await connection.execute(
                text("INSERT INTO chat_conversations (id, title, ephemeral, expires_at) VALUES (:i, :t, :e, :x)"),
                {"i": row_id, "t": title, "e": ephemeral, "x": expires_at},
            )
    try:
        found = await owner_client.get("/api/v1/conversations", params={"q": tag})
        assert found.status_code == 200
        assert [item["title"] for item in found.json()] == [f"visible {tag}"]
        assert (await owner_client.get("/api/v1/conversations", params={"q": "   "})).status_code == 422
        assert (await owner_client.get("/api/v1/conversations", params={"q": "x" * 201})).status_code == 422
        assert (await owner_client.get("/api/v1/events", params={"q": "x" * 201})).status_code == 422
        events = await owner_client.get("/api/v1/events", params={"q": f"no-such-{tag}"})
        assert events.status_code == 200 and events.json()["items"] == []
    finally:
        async with committed_engine.begin() as connection:
            await connection.execute(text("DELETE FROM chat_conversations WHERE title LIKE :p"), {"p": f"%{tag}"})
