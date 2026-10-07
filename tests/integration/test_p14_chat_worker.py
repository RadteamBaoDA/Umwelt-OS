"""P14-T3 acceptance: chat generation runs on the dedicated arq chat worker, not in the API process.

Needs the disposable Compose harness (api, chat-worker, fake-model); the docker-driven tests also need the
docker CLI. Negative control: on 512a3fe (in-process asyncio.create_task) the restart test fails because the
run stays `streaming` after the API container restarts.
"""

import asyncio
import json
import os
import shutil
import subprocess
import time
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.fake_model import fake_text

pytestmark = pytest.mark.skipif(
    os.getenv("BBD_INTEGRATION") != "1", reason="requires disposable Compose test services"
)
requires_docker = pytest.mark.skipif(shutil.which("docker") is None, reason="requires the docker CLI")

FAKE_BASE_URL = "http://fake-model:8000/v1"
TOKENS = 300


def _docker(*args: str) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, check=True, timeout=120)
    return result.stdout.strip()


def _service_container(service: str) -> str:
    """Resolve a service container inside the same disposable project that publishes the API port."""
    port = urlsplit(os.getenv("BBD_API_URL", "http://localhost:38000")).port
    api_id = _docker("ps", "--filter", f"publish={port}", "-q").split()
    assert len(api_id) == 1, f"expected exactly one container publishing {port}"
    project = _docker("inspect", "-f", '{{index .Config.Labels "com.docker.compose.project"}}', api_id[0])
    assert project.startswith("bbd-os-test-"), f"refusing project {project}"
    found = _docker(
        "ps", "-a", "-q", "--filter", f"label=com.docker.compose.project={project}",
        "--filter", f"label=com.docker.compose.service={service}",
    ).split()
    assert len(found) == 1, f"expected one {service} container, got {found}"
    return found[0]


async def _wait_api_ready(client: AsyncClient, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if (await client.get("/api/v1/system/ready")).status_code == 200:
                return
        except Exception:  # noqa: BLE001, S110  # API is restarting
            pass
        await asyncio.sleep(1)
    raise AssertionError("API did not become ready")


@pytest.fixture
async def chat_ready(ready_owner_client: AsyncClient) -> AsyncClient:
    """Point the AI settings at the fake model, grant reasoning, and probe streaming through the real routes."""
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
    probe = await client.post("/api/v1/settings/models/reasoning-large/test", json={"capability": "streaming"})
    assert probe.status_code == 200 and probe.json()["result"] == "supported", probe.text
    return client


async def _send(client: AsyncClient, prompt: str) -> str:
    conversation = (await client.post("/api/v1/conversations", json={})).json()["id"]
    sent = await client.post(f"/api/v1/conversations/{conversation}/messages", json={"content": prompt})
    assert sent.status_code == 202, sent.text
    return str(sent.json()["response_id"])


async def _events(
    client: AsyncClient, response_id: str, last_id: str | None, stop_after: int | None = None,
) -> AsyncIterator[tuple[str, str, dict[str, object]]]:
    """Yield (event_id, event, data) frames until message.done / terminal status, or `stop_after` frames."""
    headers = {"Last-Event-ID": last_id} if last_id else {}
    seen = 0
    async with client.stream(
        "GET", f"/api/v1/responses/{response_id}/events", headers=headers, timeout=120,
    ) as response:
        assert response.status_code == 200
        frame: dict[str, str] = {}
        async for line in response.aiter_lines():
            if line:
                key, _, value = line.partition(": ")
                frame[key] = value
                continue
            if "event" in frame and "id" in frame:
                data = json.loads(frame.get("data", "{}") or "{}")
                yield frame["id"], frame["event"], data
                seen += 1
                if frame["event"] == "message.done" or (
                    frame["event"] == "status" and data.get("status") in {"failed", "cancelled"}
                ) or (stop_after is not None and seen >= stop_after):
                    return
            frame = {}


@pytest.mark.asyncio
@requires_docker
async def test_chat_survives_api_restart_via_arq_not_in_process(chat_ready: AsyncClient) -> None:
    client = chat_ready
    response_id = await _send(client, f"hello [fake:tokens={TOKENS},delay_ms=20]")
    received: list[tuple[str, str, dict[str, object]]] = []
    async for frame in _events(client, response_id, None, stop_after=3):
        received.append(frame)
    assert received, "no events before restart"

    await asyncio.to_thread(_docker, "restart", _service_container("api"))
    await _wait_api_ready(client)

    async for frame in _events(client, response_id, received[-1][0]):
        received.append(frame)

    seqs = [int(frame_id.rsplit(":", 1)[1]) for frame_id, _, _ in received]
    assert seqs == list(range(1, len(seqs) + 1)), "seq values must be contiguous with no duplicates"
    assert received[-1][1] == "message.done" and received[-1][2]["status"] == "completed"
    streamed = "".join(str(data["text"]) for _, event, data in received if event == "message.delta")
    assert streamed == fake_text(TOKENS)  # grounded answers may rewrite the final text; the stream is verbatim


@pytest.mark.asyncio
@requires_docker
async def test_stopped_chat_worker_leaves_run_pending_then_completes_when_started(
    chat_ready: AsyncClient, committed_engine: AsyncEngine,
) -> None:
    client = chat_ready
    worker_container = _service_container("chat-worker")
    await asyncio.to_thread(_docker, "stop", worker_container)
    try:
        response_id = await _send(client, "hello [fake:tokens=20,delay_ms=5]")

        async def status() -> str:
            async with committed_engine.connect() as connection:
                return str((await connection.execute(
                    text("SELECT status FROM chat_response_runs WHERE id = :id"), {"id": response_id},
                )).scalar_one())

        await asyncio.sleep(2)
        assert await status() == "pending"  # nothing generates in the API process
    finally:
        await asyncio.to_thread(_docker, "start", worker_container)

    deadline = time.monotonic() + 30
    while await status() != "completed":
        assert time.monotonic() < deadline, "run did not complete within 30 s of chat-worker start"
        await asyncio.sleep(1)


@pytest.mark.asyncio
@requires_docker
async def test_chat_worker_sigterm_mid_stream_recovers_within_30s(
    chat_ready: AsyncClient, committed_engine: AsyncEngine,
) -> None:
    """SIGTERM mid-stream: the run reaches pending/streaming-again/terminal quickly, not after the 660 s sweep."""
    client = chat_ready
    worker_container = _service_container("chat-worker")
    response_id = await _send(client, "hello [fake:tokens=2000,delay_ms=50]")

    async def status() -> str:
        async with committed_engine.connect() as connection:
            return str((await connection.execute(
                text("SELECT status FROM chat_response_runs WHERE id = :id"), {"id": response_id},
            )).scalar_one())

    deadline = time.monotonic() + 30
    while await status() != "streaming":
        assert time.monotonic() < deadline, "run never started streaming"
        await asyncio.sleep(0.5)
    await asyncio.sleep(1)
    await asyncio.to_thread(_docker, "kill", "--signal=SIGTERM", worker_container)
    try:
        # Worker is down; the release hook must have left the run non-streaming (pending or failed).
        deadline = time.monotonic() + 15
        while await status() == "streaming":
            assert time.monotonic() < deadline, "run stayed streaming after SIGTERM"
            await asyncio.sleep(0.5)
    finally:
        await asyncio.to_thread(_docker, "start", worker_container)
    deadline = time.monotonic() + 30
    while await status() in {"pending", "streaming"}:
        assert time.monotonic() < deadline, "run did not reach a terminal state within 30 s of restart"
        await asyncio.sleep(1)


@pytest.mark.asyncio
@requires_docker
async def test_chat_worker_sigterm_before_first_delta_returns_pending_then_completes(
    chat_ready: AsyncClient, committed_engine: AsyncEngine,
) -> None:
    """SIGTERM while the model has not produced a token: run goes back to pending, then completes cleanly."""
    client = chat_ready
    worker_container = _service_container("chat-worker")
    response_id = await _send(client, "hello [fake:tokens=5,delay_ms=5,first_delay_ms=20000]")

    async def status() -> str:
        async with committed_engine.connect() as connection:
            return str((await connection.execute(
                text("SELECT status FROM chat_response_runs WHERE id = :id"), {"id": response_id},
            )).scalar_one())

    deadline = time.monotonic() + 30
    while await status() != "streaming":
        assert time.monotonic() < deadline, "run never started streaming"
        await asyncio.sleep(0.5)
    await asyncio.sleep(1)
    await asyncio.to_thread(_docker, "kill", "--signal=SIGTERM", worker_container)
    try:
        deadline = time.monotonic() + 15
        while await status() == "streaming":
            assert time.monotonic() < deadline, "run stayed streaming after SIGTERM"
            await asyncio.sleep(0.5)
        assert await status() == "pending"
    finally:
        await asyncio.to_thread(_docker, "start", worker_container)
    deadline = time.monotonic() + 60
    while await status() != "completed":
        assert time.monotonic() < deadline, "run did not complete after chat-worker restart"
        await asyncio.sleep(1)
    async with committed_engine.connect() as connection:
        rows = (await connection.execute(
            text("SELECT seq, event_id FROM chat_stream_events WHERE response_id = :id ORDER BY seq"),
            {"id": response_id},
        )).all()
    seqs = [r[0] for r in rows]
    assert len(seqs) == len(set(seqs)) and len({r[1] for r in rows}) == len(rows)
    assert seqs.count(1) == 1 and rows[0][0] == 1


@pytest.mark.asyncio
async def test_reranker_alias_probe_and_chat_with_reranking(chat_ready: AsyncClient) -> None:
    """Regression: /rerank replies used to fail SDK parsing (probe 500, reranking silently unavailable)."""
    client = chat_ready
    current = (await client.get("/api/v1/settings/ai")).json()
    saved = await client.put("/api/v1/settings/ai", json={
        "omniroute_base_url": FAKE_BASE_URL,
        "omniroute_credential_action": "replaced", "omniroute_api_key": "fake-model-key",
        "chat_alias": "reasoning-large", "brief_alias": "reasoning-large",
        "aliases": {"reasoning-large": {"model": "fake-chat", "destination": "remote"},
                    "reranker": {"model": "fake-rerank", "destination": "remote"}},
        "privacy": {"allow_remote_reasoning": True, "allow_remote_embeddings": True},
        "request_timeout_seconds": 30,
        "expected_revision": current["configuration_revision"],
    })
    assert saved.status_code == 200, saved.text
    probe = await client.post("/api/v1/settings/models/reranker/test", json={"capability": "reranking"})
    assert probe.status_code == 200 and probe.json()["result"] == "supported", probe.text

    response_id = await _send(client, "hello [fake:tokens=5]")
    frames = [frame async for frame in _events(client, response_id, None)]
    assert frames[-1][1] == "message.done" and frames[-1][2]["status"] == "completed"
