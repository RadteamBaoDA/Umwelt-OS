"""P15-T8 (BM-20) acceptance: upload -> ingest -> chat context -> purge removes the attachment.

Runs against the disposable stack (real API, worker and PostgreSQL). Attachments use the ordinary
upload pipeline into a server-chosen local-only "Chat attachments" source, so the existing Source
purge path must erase them with no special casing.
"""

import asyncio
import os
import time
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.skipif(
    os.getenv("BBD_INTEGRATION") != "1", reason="requires disposable Compose test services"
)

TIMEOUT_SECONDS = 360


async def _scalar(engine: AsyncEngine, sql: str, **parameters: Any) -> Any:
    async with engine.connect() as connection:
        return await connection.scalar(text(sql), parameters)


async def _attach(client: AsyncClient, body: bytes, name: str = "notes.txt") -> dict[str, Any]:
    response = await client.post("/api/v1/documents/chat-attachments", files={"file": (name, body, "text/plain")})
    assert response.status_code == 202, response.text
    return dict(response.json())


async def _wait_ready(client: AsyncClient, document_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        read = (await client.get(f"/api/v1/documents/chat-attachments/{document_id}")).json()
        if read["status"] != "pending":
            return dict(read)
        await asyncio.sleep(1)
    pytest.fail("attachment never finished ingesting")


async def test_chat_attachment_lifecycle(ready_owner_client: AsyncClient, committed_engine: AsyncEngine) -> None:
    client, engine = ready_owner_client, committed_engine
    secret = f"ATTACH-SECRET-{uuid4().hex}"

    # Boundary validation reuses the shared allowlist.
    bad = await client.post("/api/v1/documents/chat-attachments", files={"file": ("x.exe", b"MZ", "application/octet-stream")})
    assert bad.status_code == 415

    # Concurrent first uploads still converge on one local-only source.
    first, second = await asyncio.gather(
        _attach(client, f"first {secret}".encode()), _attach(client, f"second {secret}".encode()),
    )
    assert first["source_id"] == second["source_id"] and first["local_only"] is True
    source_id = UUID(first["source_id"])
    assert await _scalar(
        engine, "SELECT count(*) FROM sources WHERE configuration->>'chat_attachments' = 'true' "
        "AND status <> 'archived'") == 1

    ready = await _wait_ready(client, first["document_id"])
    assert ready["status"] == "ready" and ready["document_version_id"]
    await _wait_ready(client, second["document_id"])
    # It is an ordinary Document: the generic Documents read sees it.
    assert (await client.get(f"/api/v1/documents/{first['document_id']}")).status_code == 200

    conversation = (await client.post("/api/v1/conversations", json={"title": "attach"})).json()["id"]
    selection = {"kind": "selection", "items": [{
        "sourceId": ready["source_id"], "documentId": ready["document_id"],
        "documentVersionId": ready["document_version_id"],
    }]}
    # Local-only content is refused visibly before any run is created or model egress occurs.
    refused = await client.post(f"/api/v1/conversations/{conversation}/messages",
                                json={"content": "summarize", "context": selection})
    assert refused.status_code == 409 and "local-only" in refused.json()["detail"]
    assert await _scalar(engine, "SELECT count(*) FROM chat_response_runs WHERE conversation_id = :c",
                         c=UUID(conversation)) == 0

    # With local_only lifted (an owner decision outside this flow), the same item is accepted as selection context.
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE sources SET local_only = false WHERE id = :id"), {"id": source_id})
    accepted = await client.post(f"/api/v1/conversations/{conversation}/messages",
                                 json={"content": "summarize", "context": selection})
    assert accepted.status_code == 202, accepted.text
    context = await _scalar(engine, "SELECT retrieval_context::text FROM chat_response_runs WHERE id = :id",
                            id=UUID(accepted.json()["response_id"]))
    assert ready["document_id"] in context and '"selected_only": true' in context

    # Purge removes the attachment content everywhere; the next attachment gets a fresh source.
    queued = await client.delete(f"/api/v1/sources/{source_id}", params={"with_data": "true"})
    assert queued.status_code == 202
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        status = await _scalar(engine, "SELECT status FROM source_purge_operations WHERE source_id = :id", id=source_id)
        if status == "succeeded":
            break
        await asyncio.sleep(2)
    else:
        pytest.fail("attachment source purge did not succeed in time")
    assert (await client.get(f"/api/v1/documents/{first['document_id']}")).status_code == 404
    assert (await client.get(f"/api/v1/documents/chat-attachments/{first['document_id']}")).status_code == 404
    assert await _scalar(engine, "SELECT count(*) FROM document_chunks WHERE content LIKE :n", n=f"%{secret}%") == 0
    fresh = await _attach(client, b"after purge")
    assert fresh["source_id"] != str(source_id)
