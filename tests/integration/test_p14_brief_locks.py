"""P14 T2 acceptance: a slow brief call holds no lock or connection, and a mid-call purge publishes nothing.

Needs the disposable Compose harness (api, worker, postgres, fake-model) and the docker CLI (the fake model
is reached with `docker exec`, it publishes no port). A task title carries the fake-model marker
`[fake:first_delay_ms=8000,cite=1]`: the model reads the request body, then waits 8 s before the response
headers, and cites [1] so the brief validates. A news story observed today, supported by a Document of the
target Source, makes that Source a brief dependency.

Negative control (to record when first run): before the three-phase brief, the Source row stays
FOR UPDATE-locked and the API connection idles in transaction for the whole 8 s, so the NOWAIT lock,
the purge admission and the pg_stat_activity check below all fail.
"""

import asyncio
import hashlib
import json
import os
import shutil
import time
from urllib.parse import quote
from datetime import UTC, datetime
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.test_p14_chat_worker import FAKE_BASE_URL, _docker, _service_container

pytestmark = pytest.mark.skipif(
    os.getenv("BBD_INTEGRATION") != "1", reason="requires disposable Compose test services"
)
requires_docker = pytest.mark.skipif(shutil.which("docker") is None, reason="requires the docker CLI")

TZ = "Asia/Ho_Chi_Minh"
DELAY_MS = 8000
DEADLINE = 2.0


@pytest.fixture
async def brief_ready(ready_owner_client: AsyncClient) -> AsyncClient:
    """Point the brief alias at the fake model with remote reasoning allowed, and probe chat."""
    client = ready_owner_client
    current = (await client.get("/api/v1/settings/ai")).json()
    saved = await client.put("/api/v1/settings/ai", json={
        "omniroute_base_url": FAKE_BASE_URL,
        "omniroute_credential_action": "replaced", "omniroute_api_key": "fake-model-key",
        "chat_alias": "reasoning-large", "brief_alias": "reasoning-large",
        "aliases": {"reasoning-large": {"model": "fake-chat", "destination": "remote"}},
        "privacy": {"allow_remote_reasoning": True, "allow_remote_embeddings": False},
        "request_timeout_seconds": 30,
        "expected_revision": current["configuration_revision"],
    })
    assert saved.status_code == 200, saved.text
    probe = await client.post("/api/v1/settings/models/reasoning-large/test", json={"capability": "chat"})
    assert probe.status_code == 200 and probe.json()["result"] == "supported", probe.text
    return client


def _received(nonce: str) -> int:
    """Count chat bodies the fake model has fully read whose prompt carries ``nonce``."""
    script = (
        "import json,urllib.request;print(json.load(urllib.request.urlopen("
        f"'http://127.0.0.1:8000/_fake/received?contains={quote(nonce)}'))['count'])"
    )
    return int(_docker("exec", _service_container("fake-model"), "python", "-c", script, timeout=30))


async def _wait_received(nonce: str, generate: asyncio.Task[Response], timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if generate.done():
            early = generate.result()
            raise AssertionError(f"generate finished before the model got the request: {early.status_code} {early.text}")
        if await asyncio.to_thread(_received, nonce):
            return
        await asyncio.sleep(0.2)
    raise AssertionError("the fake model never received the brief request body")


async def _seed_supported_story(client: AsyncClient, engine: AsyncEngine, nonce: str) -> tuple[UUID, UUID]:
    """Create a Source + Document through the API and a news story observed now that cites its chunk.

    Returns ``(source_id, story_id)``; ``_assert_story_in_context`` proves the dashboard reads the seed.
    """
    created = await client.post("/api/v1/sources", json={"type": "manual", "name": f"p14 brief {nonce}"})
    created.raise_for_status()
    source_id = UUID(created.json()["id"])
    # Manual sources are created local_only (sources/public.py create_source); the brief never sends
    # local-only facts, so clear it or the story is not a prompt dependency at all.
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE sources SET local_only = false WHERE id = :id"), {"id": source_id})
    document = await client.post("/api/v1/documents", json={
        "source_id": str(source_id), "title": f"Brief story {nonce}",
        "content": f"Fictional story body {nonce}.", "external_id": f"p14-brief-{nonce}",
    })
    document.raise_for_status()
    document_id = UUID(document.json()["id"])
    query = text(
        "SELECT v.id AS version_id, v.version_number, c.id AS chunk_id, s.generation "
        "FROM document_versions v JOIN document_chunks c ON c.document_version_id = v.id "
        "JOIN sources s ON s.id = :source WHERE v.document_id = :document "
        "ORDER BY v.version_number DESC, c.id LIMIT 1"
    )
    row = None
    for _ in range(60):  # ingest may chunk asynchronously: bounded poll, never .one() on an empty set
        async with engine.connect() as connection:
            row = (await connection.execute(query, {"source": source_id, "document": document_id})).mappings().first()
        if row:
            break
        await asyncio.sleep(0.5)
    assert row, "the document was never chunked"
    async with engine.begin() as connection:
        story_id = uuid4()
        await connection.execute(text(
            "INSERT INTO news_stories (id, identity_key, identity_kind) VALUES (:id, :key, 'hash')"
        ), {"id": story_id, "key": f"p14-brief-{nonce}"})
        await connection.execute(text(
            "INSERT INTO news_observations (id, story_id, document_id, document_version_id, chunk_id, "
            "source_id, source_generation, version_number, content_hash, title, excerpt, observed_at, "
            "local_only, match_method) VALUES (:id, :story, :document, :version, :chunk, :source, "
            ":generation, :number, :hash, :title, :excerpt, now(), false, 'hash')"
        ), {
            "id": uuid4(), "story": story_id, "document": document_id, "version": row["version_id"],
            "chunk": row["chunk_id"], "source": source_id, "generation": row["generation"],
            "number": row["version_number"], "hash": hashlib.sha256(nonce.encode()).hexdigest(),
            "title": f"Brief story {nonce}", "excerpt": f"Fictional story body {nonce}.",
        })
    return source_id, story_id


async def _assert_story_in_context(client: AsyncClient, day: object, story_id: UUID) -> None:
    """The seed is real only if the dashboard's stories widget (the brief's fact source) lists it."""
    context = await client.get("/api/v1/context/daily", params={"date": str(day), "timezone": TZ})
    assert context.status_code == 200, context.text
    stories = next(w for w in context.json()["widgets"] if w["id"] == "stories")
    assert str(story_id) in {item["id"] for item in stories["items"]}, "seeded story missing from the brief facts"


async def _clear_earlier_tasks(client: AsyncClient) -> None:
    """Delete earlier ``Plan [fake:`` tasks so the shared DB cannot push the story out of MAX_FACTS."""
    page = await client.get("/api/v1/tasks", params={"q": "Plan [fake:", "limit": 100})
    page.raise_for_status()
    for item in page.json()["items"]:
        if item["title"].startswith("Plan [fake:"):
            gone = await client.delete(f"/api/v1/tasks/{item['id']}", params={"expected_revision": item["revision"]})
            assert gone.status_code == 204, gone.text


async def _revisions(engine: AsyncEngine, day: object) -> int:
    async with engine.connect() as connection:
        return int(await connection.scalar(text(
            "SELECT count(*) FROM daily_briefs WHERE owner_id = 1 AND brief_date = :day AND timezone = :tz"
        ), {"day": day, "tz": TZ}) or 0)


async def _start_slow_brief(client: AsyncClient, nonce: str) -> tuple[object, asyncio.Task[Response]]:
    day = datetime.now(UTC).astimezone(ZoneInfo(TZ)).date()
    task = await client.post("/api/v1/tasks", json={
        "title": f"Plan [fake:first_delay_ms={DELAY_MS},cite=1] {nonce}", "due_date": day.isoformat(),
    })
    assert task.status_code == 201, task.text
    generate = asyncio.create_task(client.post(
        "/api/v1/briefs/generate",
        json={"brief_date": day.isoformat(), "timezone": TZ, "force": True}, timeout=90,
    ))
    await _wait_received(f"Brief story {nonce}", generate)
    return day, generate


@requires_docker
async def test_slow_brief_holds_no_lock_and_a_mid_call_purge_publishes_no_revision(
    brief_ready: AsyncClient, committed_engine: AsyncEngine,
) -> None:
    client, nonce = brief_ready, uuid4().hex[:12]
    await _clear_earlier_tasks(client)
    source_id, story_id = await _seed_supported_story(client, committed_engine, nonce)
    day = datetime.now(UTC).astimezone(ZoneInfo(TZ)).date()
    await _assert_story_in_context(client, day, story_id)
    before = await _revisions(committed_engine, day)
    day, generate = await _start_slow_brief(client, nonce)

    started = time.monotonic()
    # (a) The cited Source row is not locked while the model is thinking.
    async with committed_engine.connect() as connection:
        await connection.execute(text("SELECT id FROM sources WHERE id = :id FOR UPDATE NOWAIT"), {"id": source_id})
        await connection.rollback()
    # (b) The privacy write (purge admission) on that Source completes.
    queued = await asyncio.wait_for(
        client.delete(f"/api/v1/sources/{source_id}", params={"with_data": "true"}), DEADLINE,
    )
    assert queued.status_code == 202, queued.text
    # (c) No API backend that touched brief tables sits idle in transaction (workers on other tables excluded).
    async with committed_engine.connect() as connection:
        idle = await connection.scalar(text(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND state = 'idle in transaction' AND query ~* '(sources|documents|timeline_events|daily_briefs|news_)' "
            "AND now() - state_change > interval '1 second' "
            "AND pid <> pg_backend_pid()"
        ))
    assert idle == 0
    assert time.monotonic() - started < DEADLINE
    assert not generate.done(), "the model response arrived before the checks; raise DELAY_MS"

    response = await generate
    assert response.status_code == 503, response.text
    assert json.loads(response.text)["detail"]["code"] == "model_unavailable"
    assert await _revisions(committed_engine, day) == before


@requires_docker
async def test_slow_brief_without_interference_publishes_one_revision(
    brief_ready: AsyncClient, committed_engine: AsyncEngine,
) -> None:
    """Control: the same slow call publishes, so the 503 above is the discard, not an outage."""
    client, nonce = brief_ready, uuid4().hex[:12]
    await _clear_earlier_tasks(client)
    _, story_id = await _seed_supported_story(client, committed_engine, nonce)
    day = datetime.now(UTC).astimezone(ZoneInfo(TZ)).date()
    await _assert_story_in_context(client, day, story_id)
    before = await _revisions(committed_engine, day)
    day, generate = await _start_slow_brief(client, nonce)
    response = await generate
    assert response.status_code == 201, response.text
    assert await _revisions(committed_engine, day) == before + 1
