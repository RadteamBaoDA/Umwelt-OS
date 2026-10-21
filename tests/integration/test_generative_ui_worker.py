"""Generative UI through the real chat worker: streamed tool-call fragments become message.ui.* SSE events.

Needs the disposable Compose harness (api, chat-worker, fake-model). The fake model answers the `tools`
capability probe and, for `[fake:ui=table|sandbox]`, streams a terminal UI tool call as `delta.tool_calls`.
"""

import os

import pytest
from httpx import AsyncClient

from tests.integration.test_p14_chat_worker import _events, _send, configure_fake_model

pytestmark = pytest.mark.skipif(
    os.getenv("INTEGRATION") != "1", reason="requires disposable Compose test services"
)


@pytest.fixture
async def tools_ready(ready_owner_client: AsyncClient) -> AsyncClient:
    """Point AI settings at the fake model and verify the streaming and tools capabilities."""
    client = ready_owner_client
    await configure_fake_model(client)
    for capability in ("streaming", "tools"):
        probe = await client.post("/api/v1/settings/models/chat/test", json={"capability": capability})
        assert probe.status_code == 200 and probe.json()["result"] == "supported", (capability, probe.text)
    return client


async def _ui_events(client: AsyncClient, marker: str) -> list[tuple[str, dict[str, object]]]:
    """Run one turn and return the (event, data) frames of the run, in order."""
    response_id = await _send(client, f"show it {marker}")
    return [(event, data) async for _id, event, data in _events(client, response_id, None)]


@pytest.mark.asyncio
async def test_table_tool_call_streams_ui_events_and_persists(tools_ready: AsyncClient) -> None:
    frames = await _ui_events(tools_ready, "[fake:ui=table,delay_ms=250]")
    names = [event for event, _ in frames]
    assert "message.ui.delta" in names and names[-1] == "message.done"
    deltas = [data for event, data in frames if event == "message.ui.delta"]
    assert all(d["kind"] == "table" and d["status"] == "streaming" for d in deltas)
    done = frames[-1][1]
    assert done["status"] == "completed"
    ui = done["generative_ui"]
    assert isinstance(ui, dict) and ui["kind"] == "table" and ui["status"] == "completed"
    assert ui["table"]["rows"][0][0] == "Q1"  # type: ignore[index]


@pytest.mark.asyncio
async def test_sandbox_tool_call_streams_ui_events(tools_ready: AsyncClient) -> None:
    frames = await _ui_events(tools_ready, "[fake:ui=sandbox,delay_ms=15]")
    done = frames[-1][1]
    ui = done["generative_ui"]
    assert isinstance(ui, dict) and ui["kind"] == "sandboxed_ui"
    assert "fake sandbox" in ui["sandbox"]["html"]  # type: ignore[index]
    assert any(event == "message.ui.delta" for event, _ in frames)
