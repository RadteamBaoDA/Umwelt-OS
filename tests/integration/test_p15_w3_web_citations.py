"""P15 W3: web citations persist in existing JSONB, read back unchanged, and die with the conversation."""

import json
import os
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.skipif(
    os.getenv("BBD_INTEGRATION") != "1", reason="requires disposable Compose test services"
)

WEB = {
    "sourceType": "web", "url": "https://example.com/a", "title": "Example", "quote": "snippet",
    "provider": "tavily", "retrievedAt": "2026-10-07T00:00:00Z",
}


@pytest.mark.asyncio
async def test_web_citation_reads_back_and_is_deleted_with_conversation(
    owner_client: AsyncClient, committed_engine: AsyncEngine,
) -> None:
    conversation_id, message_id = uuid4(), uuid4()
    async with committed_engine.begin() as connection:
        await connection.execute(
            text("INSERT INTO chat_conversations (id, workspace_id, actor_user_id, title) VALUES (:i, (SELECT id FROM workspaces ORDER BY created_at LIMIT 1), (SELECT owner_user_id FROM workspaces ORDER BY created_at LIMIT 1), 'w3')"), {"i": conversation_id},
        )
        await connection.execute(
            text("INSERT INTO chat_messages (id, conversation_id, role, content, citations) "
                 "VALUES (:m, :c, 'assistant', 'answer [1]', CAST(:j AS jsonb))"),
            {"m": message_id, "c": conversation_id, "j": json.dumps([WEB])},
        )
    try:
        detail = await owner_client.get(f"/api/v1/conversations/{conversation_id}")
        assert detail.status_code == 200
        assert detail.json()["messages"][0]["citations"] == [WEB]
        assert detail.json()["messages"][0]["web_search"] is None
        assert (await owner_client.delete(f"/api/v1/conversations/{conversation_id}")).status_code == 204
        async with committed_engine.connect() as connection:
            left = await connection.scalar(
                text("SELECT count(*) FROM chat_messages WHERE id = :m"), {"m": message_id},
            )
        assert left == 0
    finally:
        async with committed_engine.begin() as connection:
            await connection.execute(text("DELETE FROM chat_conversations WHERE id = :i"), {"i": conversation_id})
