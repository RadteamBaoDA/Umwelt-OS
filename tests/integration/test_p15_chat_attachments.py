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


async def _attach(client: AsyncClient, body: bytes, name: str = "notes.txt", shared: bool = False) -> dict[str, Any]:
    response = await client.post("/api/v1/documents/chat-attachments", files={"file": (name, body, "text/plain")},
                                 data={"share_with_model": "true"} if shared else {})
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
    assert refused.status_code == 409
    body = refused.json()
    assert (body.get("error") or body.get("detail"))["code"] == "selection_local_only"
    assert await _scalar(engine, "SELECT count(*) FROM chat_response_runs WHERE conversation_id = :c",
                         c=UUID(conversation)) == 0

    # The owner opts in per upload: the file lands in the separate, not-local-only shared source.
    shared = await _attach(client, f"shared {secret}".encode(), shared=True)
    assert shared["local_only"] is False and shared["source_id"] != first["source_id"]
    shared_ready = await _wait_ready(client, shared["document_id"])
    shared_selection = {"kind": "selection", "items": [{
        "sourceId": shared_ready["source_id"], "documentId": shared_ready["document_id"],
        "documentVersionId": shared_ready["document_version_id"],
    }]}
    accepted = await client.post(f"/api/v1/conversations/{conversation}/messages",
                                 json={"content": "summarize", "context": shared_selection})
    assert accepted.status_code == 202, accepted.text
    context = await _scalar(engine, "SELECT retrieval_context::text FROM chat_response_runs WHERE id = :id",
                            id=UUID(accepted.json()["response_id"]))
    assert shared_ready["document_id"] in context and '"selected_only": true' in context

    # Purge removes the attachment content of both sources; the next attachment gets a fresh source.
    for purged in (source_id, UUID(shared["source_id"])):
        queued = await client.delete(f"/api/v1/sources/{purged}", params={"with_data": "true"})
        assert queued.status_code == 202
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            status = await _scalar(engine, "SELECT status FROM source_purge_operations WHERE source_id = :id",
                                   id=purged)
            if status == "succeeded":
                break
            await asyncio.sleep(2)
        else:
            pytest.fail("attachment source purge did not succeed in time")
    for document_id in (first["document_id"], shared["document_id"]):
        assert (await client.get(f"/api/v1/documents/{document_id}")).status_code == 404
        assert (await client.get(f"/api/v1/documents/chat-attachments/{document_id}")).status_code == 404
    assert await _scalar(engine, "SELECT count(*) FROM document_chunks WHERE content LIKE :n", n=f"%{secret}%") == 0
    fresh = await _attach(client, b"after purge")
    assert fresh["source_id"] != str(source_id)
