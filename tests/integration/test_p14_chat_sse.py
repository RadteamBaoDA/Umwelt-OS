"""P14-T4 acceptance: chat SSE waits for slow clients with no lock held, yet writes under the locks.

Needs the disposable Compose harness (api + postgres). Each stalled stream replays ~100 MB of seeded events
to a raw socket with a 4 KiB receive buffer that is not read after the headers. Before any check, the test
waits until the server has STOPPED reading `chat_stream_events` (pg_stat_user_tables counters quiet for 3 s)
while rows remain unread, i.e. the server is blocked on this client rather than merely slow or absorbed by a
large host/port-forwarder buffer (Docker Desktop's). Events are large so even the pre-T4
one-event-per-transaction loop fills that buffer quickly.

Negative control (recorded in the P14-T4 fix report): on the pre-T4 routes (a29d08d) the generator yields each
frame inside the transaction that holds pg_advisory_xact_lock(1297109577, 1) plus Conversation/ResponseRun
FOR UPDATE, so once write-paused, uvicorn's send() awaits flow.drain() inside that transaction and (a)/(b)/(c)
of the slow-client test time out.
"""

import asyncio
import json
import os
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.auth.dependencies import SESSION_COOKIE

pytestmark = pytest.mark.skipif(
    os.getenv("BBD_INTEGRATION") != "1", reason="requires disposable Compose test services"
)

SLOW_EVENTS = 400
EVENT_BYTES = 256 * 1024
STALL_QUIET = 3.0  # > pgstat's 1 s flush interval for a backend that keeps ending transactions
STALL_TIMEOUT = 120.0
DEADLINE = 2.0
MARKER = "p14-t4-unread-when-checked"
OWNER_PASSWORD = "test-owner-password-42"
STREAM_CAP = 64  # modules.chat.routes.MAX_CHAT_STREAMS_PER_API_PROCESS
API_PROCESSES = 2  # WEB_CONCURRENCY in api.Dockerfile; pinned by test_api_runs_two_uvicorn_workers


async def _event_reads(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:  # a new transaction = a fresh pg_stat snapshot
        return int(await connection.scalar(text(
            "SELECT coalesce(idx_tup_fetch, 0) + coalesce(seq_tup_read, 0) FROM pg_stat_user_tables "
            "WHERE relname = 'chat_stream_events'"
        )) or 0)


async def _wait_until_stalled(engine: AsyncEngine) -> None:
    """Return once nothing has read chat_stream_events for STALL_QUIET s: the stream is write-paused."""
    deadline = time.monotonic() + STALL_TIMEOUT
    last, since = await _event_reads(engine), time.monotonic()
    while time.monotonic() - since < STALL_QUIET:
        assert time.monotonic() < deadline, "server never became write-paused on the stalled client"
        await asyncio.sleep(0.5)
        current = await _event_reads(engine)
        if current != last:
            last, since = current, time.monotonic()


async def _privacy_fence(client: AsyncClient, engine: AsyncEngine) -> dict[str, object]:
    """Persist the privacy row (PUT current values) and return the fence a run admitted now is stamped with."""
    current = (await client.get("/api/v1/settings/memory-privacy")).json()
    assert (await client.put("/api/v1/settings/memory-privacy", json=current)).status_code == 200
    async with engine.connect() as connection:
        enabled, updated_at = (await connection.execute(text(
            "SELECT store_conversation_history, updated_at FROM memory_privacy_settings WHERE owner_id = 1"
        ))).one()
    return {"store_conversation_history": enabled, "persisted": True, "updated_at": updated_at.isoformat()}


async def _seed_run(
    client: AsyncClient, engine: AsyncEngine, deltas: int, payload_bytes: int,
    *, status: str = "completed", fence: dict[str, object] | None = None,
) -> UUID:
    """Create a conversation through the API, then a run with `deltas` deltas in SQL.

    A completed run also gets `message.done`; a streaming run is stamped with `fence` and has no terminal event.
    """
    created = await client.post("/api/v1/conversations", json={})
    assert created.status_code in (200, 201), created.text
    conversation_id = UUID(created.json()["id"])
    message_id, response_id = uuid4(), uuid4()
    context = json.dumps({"_chat_privacy_fence": fence} if fence is not None else {})
    async with engine.begin() as connection:
        await connection.execute(text(
            "INSERT INTO chat_messages (id, conversation_id, role, content) "
            "VALUES (CAST(:m AS uuid), CAST(:c AS uuid), 'user', 'p14-t4')"
        ), {"m": str(message_id), "c": str(conversation_id)})
        await connection.execute(text(
            "INSERT INTO chat_response_runs (id, workspace_id, actor_user_id, conversation_id, user_message_id, "
            "status, ephemeral, retrieval_context, completed_at) "
            "SELECT CAST(:r AS uuid), workspace_id, actor_user_id, id, CAST(:m AS uuid), "
            "CAST(:status AS varchar), false, CAST(:ctx AS jsonb), "
            "CASE WHEN CAST(:status AS varchar) = 'completed' THEN now() END "
            "FROM chat_conversations WHERE id = CAST(:c AS uuid)"
        ), {"r": str(response_id), "c": str(conversation_id), "m": str(message_id), "status": status,
            "ctx": context})
        await connection.execute(text(
            "INSERT INTO chat_stream_events (id, response_id, seq, event_type, event_id, data) "
            "SELECT gen_random_uuid(), CAST(:r AS uuid), n, 'message.delta', CAST(:r AS text) || ':' || n, "
            "jsonb_build_object('text', repeat('x', CAST(:size AS integer))) "
            "FROM generate_series(1, CAST(:n AS integer)) AS n"
        ), {"r": str(response_id), "n": deltas, "size": payload_bytes})
        if status == "completed":
            await connection.execute(text(
                "INSERT INTO chat_stream_events (id, response_id, seq, event_type, event_id, data) "
                "VALUES (gen_random_uuid(), CAST(:r AS uuid), CAST(:seq AS integer), 'message.done', "
                "CAST(:r AS text) || ':' || CAST(:seq AS text), CAST(:done AS jsonb))"
            ), {"r": str(response_id), "seq": deltas + 1, "done": '{"status": "completed"}'})
    return response_id


async def _open_socket(
    client: AsyncClient, response_id: UUID, cookie: str | None = None,
) -> tuple[socket.socket, bytes]:
    """GET the SSE stream on a tiny-receive-buffer socket and read only the response headers."""
    url = client.base_url
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    sock.settimeout(10)
    await asyncio.to_thread(sock.connect, (url.host if url.host != "localhost" else "127.0.0.1", url.port or 80))
    cookie = cookie or client.cookies.get(SESSION_COOKIE)
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
    return sock, head


async def _open_stalled_socket(
    client: AsyncClient, response_id: UUID, cookie: str | None = None,
) -> tuple[socket.socket, bytes]:
    sock, head = await _open_socket(client, response_id, cookie)
    assert head.startswith(b"HTTP/1.1 200"), head[:200]
    return sock, head


async def _drain_socket(sock: socket.socket) -> bytes:
    """Read the rest of a stream to its end (chunked terminator or close)."""
    sock.settimeout(60)
    data = bytearray()
    while not data.endswith(b"0\r\n\r\n"):
        chunk = await asyncio.to_thread(sock.recv, 1 << 20)
        if not chunk:
            break
        data += chunk
    return bytes(data)


def _sse_frames(raw: bytes) -> list[tuple[str, Any]]:
    """De-chunk an HTTP/1.1 response and parse its SSE frames as (event, data)."""
    _, _, rest = raw.partition(b"\r\n\r\n")
    body, i = bytearray(), 0
    while True:
        j = rest.index(b"\r\n", i)
        size = int(rest[i:j], 16)
        if size == 0:
            break
        body += rest[j + 2:j + 2 + size]
        i = j + 2 + size + 2
    frames = []
    for block in body.decode().split("\n\n"):
        fields = dict(line.partition(": ")[::2] for line in block.splitlines() if not line.startswith(":"))
        if "event" in fields:
            frames.append((fields["event"], json.loads(fields.get("data") or "null")))
    return frames


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
        await _wait_until_stalled(committed_engine)  # the server is blocked on this client

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

        # (d) no backend holding the privacy key sits idle in transaction (the slow stream would be the one)
        async with committed_engine.connect() as connection:
            stuck = await connection.scalar(text(
                "SELECT count(*) FROM pg_stat_activity a JOIN pg_locks l ON l.pid = a.pid "
                "WHERE a.datname = current_database() AND l.locktype = 'advisory' AND l.granted "
                "AND l.classid::text::bigint = 1297109577 AND l.objid::text::bigint = 1 "
                "AND a.state LIKE 'idle in transaction%' AND now() - a.state_change > interval '1 second'"
            ))
        assert stuck == 0

        # Stall proof: rewrite the LAST delta now. Only a server that had not yet read it while (a)-(d) ran
        # (i.e. was write-paused on this client, not merely buffered by a proxy) can deliver the new text.
        async with committed_engine.begin() as connection:
            await connection.execute(text(
                "UPDATE chat_stream_events SET data = jsonb_build_object('text', CAST(:marker AS text)) "
                "WHERE response_id = CAST(:r AS uuid) AND seq = :seq"
            ), {"marker": MARKER, "r": str(slow_run), "seq": SLOW_EVENTS})

        # Once read again, the stalled stream resumes and delivers every event exactly once.
        frames = _sse_frames(head + await _drain_socket(sock))
        assert [event for event, _ in frames].count("message.delta") == SLOW_EVENTS
        assert frames[-1] == ("message.done", {"status": "completed"})
        assert frames[SLOW_EVENTS - 1][1] == {"text": MARKER}, "server was not stalled on the slow client"
    finally:
        sock.close()


@pytest.mark.asyncio
async def test_consent_change_mid_stream_delivers_no_event_read_after_it_commits(
    ready_owner_client: AsyncClient, committed_engine: AsyncEngine,
) -> None:
    """Redaction ordering on the real stack: a consent change that commits while the client is stalled.

    The redaction (`_privacy_cancel_locked`) blanks every delta to "" in the same commit that cancels the run,
    so any delta the server read after the change committed would arrive as "". Expected: only original
    deltas (written before the change), fewer than seeded (the server was stalled), then `status: cancelled`.
    """
    client = ready_owner_client
    original = (await client.get("/api/v1/settings/memory-privacy")).json()
    run = await _seed_run(client, committed_engine, SLOW_EVENTS, EVENT_BYTES, status="streaming",
                          fence=await _privacy_fence(client, committed_engine))
    sock, head = await _open_stalled_socket(client, run)
    try:
        await _wait_until_stalled(committed_engine)
        flip = {**original, "store_conversation_history": not original["store_conversation_history"]}
        async with asyncio.timeout(DEADLINE):  # the consent write is not blocked by the stalled stream
            flipped = await client.put("/api/v1/settings/memory-privacy", json=flip)
        assert flipped.status_code == 200, flipped.text

        frames = _sse_frames(head + await _drain_socket(sock))
        deltas = [data for event, data in frames if event == "message.delta"]
        assert frames[-1] == ("status", {"status": "cancelled"})
        assert [event for event, _ in frames].count("status") == 1
        assert 0 < len(deltas) < SLOW_EVENTS, "server was not stalled on the slow client"
        assert all(d == {"text": "x" * EVENT_BYTES} for d in deltas), "a delta read after the change was sent"
        async with committed_engine.connect() as connection:
            status, kept = (await connection.execute(text(
                "SELECT r.status, (SELECT count(*) FROM chat_stream_events e WHERE e.response_id = r.id "
                "AND e.event_type = 'message.delta' AND e.data->>'text' <> '') "
                "FROM chat_response_runs r WHERE r.id = CAST(:r AS uuid)"
            ), {"r": str(run)})).one()
        assert (status, kept) == ("cancelled", 0)  # the redaction really committed before the next read
    finally:
        sock.close()
        await client.put("/api/v1/settings/memory-privacy", json=original)


@pytest.mark.asyncio
async def test_revoked_session_gets_auth_expired_and_no_further_text(
    ready_owner_client: AsyncClient, committed_engine: AsyncEngine,
    post_throttled: Callable[..., Awaitable[Response]],
) -> None:
    client = ready_owner_client
    run = await _seed_run(client, committed_engine, SLOW_EVENTS, EVENT_BYTES, status="streaming",
                          fence=await _privacy_fence(client, committed_engine))
    origin = client.headers["Origin"]
    async with AsyncClient(base_url=client.base_url, timeout=30) as second:
        csrf = (await second.get("/api/v1/auth/csrf")).json()["csrfToken"]
        login = await post_throttled(
            second, "/api/v1/auth/login", headers={"Origin": origin, "X-CSRF-Token": csrf},
            json={"password": OWNER_PASSWORD},
        )
        login.raise_for_status()
        second.headers.update({"Origin": origin, "X-CSRF-Token": login.json()["csrfToken"]})
        sock, head = await _open_stalled_socket(client, run, cookie=second.cookies.get(SESSION_COOKIE))
        try:
            await _wait_until_stalled(committed_engine)
            async with asyncio.timeout(DEADLINE):
                logout = await second.post("/api/v1/auth/logout")
            assert logout.status_code == 204, logout.text

            frames = _sse_frames(head + await _drain_socket(sock))
            events = [event for event, _ in frames]
            assert frames[-1] == ("status", {"status": "auth_expired"})
            assert "message.delta" not in events[events.index("status"):]
            assert 0 < events.count("message.delta") < SLOW_EVENTS, "server was not stalled on the slow client"
        finally:
            sock.close()


@pytest.mark.asyncio
async def test_65th_concurrent_stream_gets_503_and_closed_streams_free_their_permits(
    ready_owner_client: AsyncClient, committed_engine: AsyncEngine,
) -> None:
    client = ready_owner_client
    run = await _seed_run(client, committed_engine, 0, 0, status="streaming",
                          fence=await _privacy_fence(client, committed_engine))
    # The cap is per API process (MAX_CHAT_STREAMS_PER_API_PROCESS) and the API runs API_PROCESSES uvicorn
    # workers; the kernel picks which one accepts. So open until the first refusal: no process refuses
    # before it holds the cap, and none can hold more, so the refusal comes in [cap, cap * processes].
    socks: list[socket.socket] = []
    try:
        while True:
            sock, head = await _open_socket(client, run)
            if not head.startswith(b"HTTP/1.1 200"):
                break
            socks.append(sock)
            assert len(socks) <= STREAM_CAP * API_PROCESSES, "no stream was refused past the total cap"
        while b"Chat stream limit reached" not in head and (chunk := await asyncio.to_thread(sock.recv, 1024)):
            head += chunk
        sock.close()
        assert head.startswith(b"HTTP/1.1 503") and b"Chat stream limit reached" in head, head[:300]
        assert len(socks) >= STREAM_CAP, f"refused with only {len(socks)} open streams"
    finally:
        for sock in socks:
            sock.close()
    opened = len(socks)

    # Closed clients release their permits promptly (not at garbage collection): as many new streams fit again.
    reopened: list[socket.socket] = []
    try:
        deadline = time.monotonic() + 30
        while len(reopened) < opened:
            sock, head = await _open_socket(client, run)
            if head.startswith(b"HTTP/1.1 200"):
                reopened.append(sock)
                continue
            sock.close()
            assert head.startswith(b"HTTP/1.1 503") and time.monotonic() < deadline, head[:200]
            await asyncio.sleep(0.2)
    finally:
        for sock in reopened:
            sock.close()
