"""P14-T4 acceptance: a chat SSE client that stops reading blocks neither the global privacy lock nor other chats.

Needs the disposable Compose harness (api + postgres; no model or docker CLI). The slow stream replays
~24 MB of seeded events to a raw socket with a 4 KiB receive buffer that is never read after the headers,
which is far beyond the loopback/proxy socket buffers, so uvicorn is write-paused mid-stream.

Negative control (fails on 512a3fe by construction): there the generator yields each frame inside the
transaction that holds pg_advisory_xact_lock(1297109577, 1) plus Conversation/ResponseRun FOR UPDATE.
Once the transport is write-paused, uvicorn's send() awaits flow.drain() inside that transaction, which
never completes because the client never reads. So (a) the raw lock acquisition, (b) the Memory privacy
PUT (it takes the same key first) and (c) the second chat's per-poll lock_export_privacy all wait past their
2 s deadlines, and (d) the slow stream's backend sits `idle in transaction`.
"""

import asyncio
import json
import os
import socket
import time
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.auth.dependencies import SESSION_COOKIE

pytestmark = pytest.mark.skipif(
    os.getenv("BBD_INTEGRATION") != "1", reason="requires disposable Compose test services"
)

SLOW_EVENTS = 6000
EVENT_BYTES = 4096
DEADLINE = 2.0


async def _seed_run(client: AsyncClient, engine: AsyncEngine, deltas: int, payload_bytes: int) -> UUID:
    """Create a conversation through the API, then a completed run with `deltas` deltas + message.done in SQL."""
    created = await client.post("/api/v1/conversations", json={})
    assert created.status_code in (200, 201), created.text
    conversation_id = UUID(created.json()["id"])
    message_id, response_id = uuid4(), uuid4()
    async with engine.begin() as connection:
        await connection.execute(text(
            "INSERT INTO chat_messages (id, conversation_id, role, content) "
            "VALUES (CAST(:m AS uuid), CAST(:c AS uuid), 'user', 'p14-t4')"
        ), {"m": str(message_id), "c": str(conversation_id)})
        await connection.execute(text(
            "INSERT INTO chat_response_runs (id, conversation_id, user_message_id, status, ephemeral, completed_at) "
            "VALUES (CAST(:r AS uuid), CAST(:c AS uuid), CAST(:m AS uuid), 'completed', false, now())"
        ), {"r": str(response_id), "c": str(conversation_id), "m": str(message_id)})
        await connection.execute(text(
            "INSERT INTO chat_stream_events (id, response_id, seq, event_type, event_id, data) "
            "SELECT gen_random_uuid(), CAST(:r AS uuid), n, 'message.delta', CAST(:r AS text) || ':' || n, "
            "jsonb_build_object('text', repeat('x', CAST(:size AS integer))) "
            "FROM generate_series(1, CAST(:n AS integer)) AS n"
        ), {"r": str(response_id), "n": deltas, "size": payload_bytes})
        await connection.execute(text(
            "INSERT INTO chat_stream_events (id, response_id, seq, event_type, event_id, data) "
            "VALUES (gen_random_uuid(), CAST(:r AS uuid), CAST(:seq AS integer), 'message.done', "
            "CAST(:r AS text) || ':' || CAST(:seq AS text), CAST(:done AS jsonb))"
        ), {"r": str(response_id), "seq": deltas + 1, "done": '{"status": "completed"}'})
    return response_id


async def _open_stalled_socket(client: AsyncClient, response_id: UUID) -> tuple[socket.socket, bytes]:
    """GET the SSE stream on a tiny-receive-buffer socket, read only the response headers, then stop reading."""
    url = client.base_url
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    sock.settimeout(10)
    await asyncio.to_thread(sock.connect, (url.host if url.host != "localhost" else "127.0.0.1", url.port or 80))
    cookie = client.cookies.get(SESSION_COOKIE)
    assert cookie, "owner session cookie missing"
    request = (
        f"GET /api/v1/responses/{response_id}/events HTTP/1.1\r\nHost: {url.host}:{url.port}\r\n"
        f"Cookie: {SESSION_COOKIE}={cookie}\r\nAccept: text/event-stream\r\n\r\n"
    )
    await asyncio.to_thread(sock.sendall, request.encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = await asyncio.to_thread(sock.recv, 1024)
        assert chunk, "server closed before headers"
        head += chunk
    assert head.startswith(b"HTTP/1.1 200"), head[:200]
    return sock, head


async def _drain_socket(sock: socket.socket) -> bytes:
    """Read the rest of the slow stream to its end (chunked terminator)."""
    sock.settimeout(60)
    data = bytearray()
    while not data.endswith(b"0\r\n\r\n"):
        chunk = await asyncio.to_thread(sock.recv, 1 << 20)
        if not chunk:
            break
        data += chunk
    return bytes(data)


async def _frames(client: AsyncClient, response_id: UUID) -> AsyncIterator[tuple[str, dict[str, object]]]:
    async with client.stream("GET", f"/api/v1/responses/{response_id}/events", timeout=30) as response:
        assert response.status_code == 200
        frame: dict[str, str] = {}
        async for line in response.aiter_lines():
            if line:
                key, _, value = line.partition(": ")
                frame[key] = value
                continue
            if "event" in frame:
                yield frame["event"], json.loads(frame.get("data", "{}") or "{}")
                if frame["event"] == "message.done":
                    return
            frame = {}


@pytest.mark.asyncio
async def test_slow_sse_client_blocks_neither_privacy_lock_nor_other_chats(
    ready_owner_client: AsyncClient, committed_engine: AsyncEngine,
) -> None:
    client = ready_owner_client
    slow_run = await _seed_run(client, committed_engine, SLOW_EVENTS, EVENT_BYTES)
    fast_run = await _seed_run(client, committed_engine, 5, 16)

    sock, head = await _open_stalled_socket(client, slow_run)
    try:
        await asyncio.sleep(3)  # let the server fill every buffer and become write-paused

        # (a) the global privacy key is free
        started = time.monotonic()
        async with asyncio.timeout(DEADLINE):
            async with committed_engine.begin() as connection:
                await connection.execute(text("SELECT pg_advisory_xact_lock(1297109577, 1)"))
        assert time.monotonic() - started < DEADLINE

        # (b) a privacy write that takes the same key completes
        current = (await client.get("/api/v1/settings/memory-privacy")).json()
        async with asyncio.timeout(DEADLINE):
            saved = await client.put("/api/v1/settings/memory-privacy", json=current)
        assert saved.status_code == 200, saved.text

        # (c) another chat's stream delivers every event and its terminal frame
        received: list[tuple[str, dict[str, object]]] = []
        async with asyncio.timeout(DEADLINE):
            async for frame in _frames(client, fast_run):
                received.append(frame)
        assert [event for event, _ in received] == ["message.delta"] * 5 + ["message.done"]
        assert received[-1][1]["status"] == "completed"

        # (d) the stalled stream holds no transaction open while it waits for the client
        async with committed_engine.connect() as connection:
            stuck = await connection.scalar(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND state LIKE 'idle in transaction%' AND now() - state_change > interval '1 second'"
            ))
        assert stuck == 0

        # Once read again, the stalled stream resumes and delivers every event exactly once.
        rest = head + await _drain_socket(sock)
        assert rest.count(b"event: message.delta") == SLOW_EVENTS
        assert rest.count(b"event: message.done") == 1
    finally:
        sock.close()
