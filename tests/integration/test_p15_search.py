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
                text("INSERT INTO chat_conversations (id, workspace_id, actor_user_id, title, ephemeral, expires_at) VALUES (:i, (SELECT id FROM workspaces ORDER BY created_at LIMIT 1), (SELECT owner_user_id FROM workspaces ORDER BY created_at LIMIT 1), :t, :e, :x)"),
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


@pytest.mark.asyncio
async def test_conversation_search_fences_on_real_rows(
    owner_client: AsyncClient, committed_engine: AsyncEngine,
) -> None:
    tag = uuid4().hex[:8]
    now = datetime.now(UTC)
    # (title, context_kind, archived, ephemeral, expires_at)
    rows = [
        (f"normal {tag}", None, False, False, None),
        (f"automation {tag}", "automation", False, False, None),
        (f"archived {tag}", None, True, False, None),
        (f"live-ephemeral {tag}", None, False, True, now + timedelta(hours=1)),
        (f"100% {tag}", None, False, False, None),
        (f"100x {tag}", None, False, False, None),
    ]
    async with committed_engine.begin() as connection:
        for title, kind, archived, ephemeral, expires_at in rows:
            await connection.execute(
                text("INSERT INTO chat_conversations (id, workspace_id, actor_user_id, title, context_kind, archived, ephemeral, expires_at)"
                     " VALUES (:i, (SELECT id FROM workspaces ORDER BY created_at LIMIT 1), (SELECT owner_user_id FROM workspaces ORDER BY created_at LIMIT 1), :t, :k, :a, :e, :x)"),
                {"i": uuid4(), "t": title, "k": kind, "a": archived, "e": ephemeral, "x": expires_at},
            )
    privacy = (await owner_client.get("/api/v1/settings/memory-privacy")).json()["store_conversation_history"]
    try:
        async def titles(**params: object) -> set[str]:
            response = await owner_client.get("/api/v1/conversations", params={"q": tag, **params})
            assert response.status_code == 200
            return {item["title"] for item in response.json()}

        assert await titles() == {f"normal {tag}", f"100% {tag}", f"100x {tag}"}
        assert await titles(archived="true") == {f"archived {tag}"}
        # A literal percent sign matches only itself, not "any character".
        percent = await owner_client.get("/api/v1/conversations", params={"q": "100%"})
        assert [i["title"] for i in percent.json() if tag in i["title"]] == [f"100% {tag}"]

        created = await owner_client.post("/api/v1/conversations", json={"title": f"doomed {tag}"})
        assert created.status_code in (200, 201)
        assert f"doomed {tag}" in await titles()
        assert (await owner_client.delete(f"/api/v1/conversations/{created.json()['id']}")).status_code in (200, 204)
        assert f"doomed {tag}" not in await titles()

        off = await owner_client.put("/api/v1/settings/memory-privacy", json={"store_conversation_history": False})
        assert off.status_code == 200
        assert await titles() == set()
    finally:
        await owner_client.put("/api/v1/settings/memory-privacy", json={"store_conversation_history": privacy})
        async with committed_engine.begin() as connection:
            await connection.execute(text("DELETE FROM chat_conversations WHERE title LIKE :p"), {"p": f"%{tag}"})


@pytest.mark.asyncio
async def test_event_search_fences_on_real_rows(
    owner_client: AsyncClient, committed_engine: AsyncEngine,
) -> None:
    tag = uuid4().hex[:8]
    now = datetime.now(UTC)
    source_id, document_id, version_id, chunk_id = uuid4(), uuid4(), uuid4(), uuid4()
    derived_id, manual_id, deleted_id, pct_id, other_id = (uuid4() for _ in range(5))
    async with committed_engine.begin() as connection:
        await connection.execute(text("INSERT INTO sources (id, workspace_id, type, name) VALUES (:i, (SELECT id FROM workspaces ORDER BY created_at LIMIT 1), 'rss', :n)"),
                                 {"i": source_id, "n": f"p15 {tag}"})
        await connection.execute(
            text("INSERT INTO documents (id, workspace_id, source_id, title, current_version, content_hash)"
                 " VALUES (:d, (SELECT id FROM workspaces ORDER BY created_at LIMIT 1), :s, :t, 1, 'h')"), {"d": document_id, "s": source_id, "t": f"doc {tag}"})
        await connection.execute(
            text("INSERT INTO document_versions (id, document_id, version_number, content, content_hash)"
                 " VALUES (:v, :d, 1, 'c', 'h')"), {"v": version_id, "d": document_id})
        await connection.execute(
            text("INSERT INTO document_chunks (id, document_version_id, chunk_index, content, content_hash, token_count)"
                 " VALUES (:c, :v, 0, 'c', 'h', 1)"), {"c": chunk_id, "v": version_id})
        for event_id, title, origin, deleted_at, src in [
            (derived_id, f"derived {tag}", "derived", None, source_id),
            (manual_id, f"manual {tag}", "manual", None, None),
            (deleted_id, f"deleted {tag}", "manual", now, None),
            (pct_id, f"100% {tag}", "manual", None, None),
            (other_id, f"100x {tag}", "manual", None, None),
        ]:
            await connection.execute(
                text("INSERT INTO timeline_events (id, workspace_id, source_id, type, title, origin, date_precision, observed_at, deleted_at)"
                     " VALUES (:i, (SELECT id FROM workspaces ORDER BY created_at LIMIT 1), :s, 'note', :t, :o, 'unknown', now(), :x)"),
                {"i": event_id, "s": src, "t": title, "o": origin, "x": deleted_at},
            )
        await connection.execute(
            text("INSERT INTO timeline_event_evidence (id, workspace_id, event_id, source_id, document_id, document_version_id, chunk_id)"
                 " VALUES (:i, (SELECT id FROM workspaces ORDER BY created_at LIMIT 1), :e, :s, :d, :v, :c)"),
            {"i": uuid4(), "e": derived_id, "s": source_id, "d": document_id, "v": version_id, "c": chunk_id},
        )
    try:
        async def titles(path: str, q: str = tag) -> set[str]:
            response = await owner_client.get(path, params={"q": q, "limit": 100})
            assert response.status_code == 200
            return {item["title"] for item in response.json()["items"] if tag in item["title"]}

        paths = ("/api/v1/events", "/api/v1/timeline")
        for path in paths:
            before = await titles(path)
            assert f"derived {tag}" in before and f"deleted {tag}" not in before
        # Emulate removing the Source's support (the real closure-based API needs a held admission):
        # drop its evidence and soft-delete derived events left without any.
        async with committed_engine.begin() as connection:
            await connection.execute(text("DELETE FROM timeline_event_evidence WHERE source_id = :s"), {"s": source_id})
            await connection.execute(text(
                "UPDATE timeline_events SET deleted_at = now() WHERE origin = 'derived' AND source_id = :s"), {"s": source_id})
        for path in paths:
            after = await titles(path)
            assert not any(title.startswith(("derived", "[unsupported")) for title in after)
            assert f"manual {tag}" in after and f"deleted {tag}" not in after
            assert await titles(path, "100%") == {f"100% {tag}"}
    finally:
        async with committed_engine.begin() as connection:
            ids = [derived_id, manual_id, deleted_id, pct_id, other_id]
            await connection.execute(text("DELETE FROM timeline_events WHERE id = ANY(:ids)"), {"ids": ids})
            await connection.execute(text("DELETE FROM documents WHERE id = :d"), {"d": document_id})
            await connection.execute(text("DELETE FROM sources WHERE id = :s"), {"s": source_id})
