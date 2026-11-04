"""The chat worker carries streamed provider thinking blocks onto the assistant tool turn of the next round."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from modules.chat import worker
from tests.unit.modules.chat.test_generative_ui_worker import END, _call, _media_config, _UIGen

DETAILS = [{"type": "redacted_thinking", "data": "x"}]


def _details(details: list[object]) -> str:
    """One SSE chunk carrying ``delta.reasoning_details``."""
    return "data: " + json.dumps({"choices": [{"delta": {"reasoning_details": details}}]})


async def _followup(monkeypatch: pytest.MonkeyPatch, first_round: list[str]) -> dict[str, object]:
    """Run a data-tool round then an empty round; return the assistant tool turn the second request carried."""
    trip_args = json.dumps({"titles": ["Hanoi"]})
    gen = _UIGen(monkeypatch, [
        [*first_round, _call(0, name="get_trip_stop_images", args=trip_args, call_id="t1")], END])
    monkeypatch.setattr(worker.settings_public, "get_ai_execution_config", AsyncMock(return_value=_media_config(True)))
    monkeypatch.setattr(worker.media, "run_trip_images_tool", AsyncMock(return_value='{"stops": []}'))
    await gen.run(csrf_signing_secret=SecretStr("sig"))
    gen.failed.assert_not_awaited()
    return gen.requests[1]["messages"][-2]


@pytest.mark.asyncio
async def test_chat_worker_round_message_carries_stream_reasoning_details(monkeypatch: pytest.MonkeyPatch) -> None:
    turn = await _followup(monkeypatch, [_details([*DETAILS, "junk"])])
    assert turn["role"] == "assistant" and turn["reasoning_details"] == DETAILS


@pytest.mark.asyncio
async def test_chat_worker_round_message_unchanged_without_reasoning_details(monkeypatch: pytest.MonkeyPatch) -> None:
    turn = await _followup(monkeypatch, [])
    assert turn["role"] == "assistant" and "reasoning_details" not in turn
