"""The harness keeps provider thinking blocks on assistant tool turns (Anthropic requires them back unmodified)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from modules.agents import gateway_factory, harness

THINKING = [{"type": "thinking", "thinking": "", "signature": "s"}]


async def _call_model(monkeypatch: pytest.MonkeyPatch, message: dict[str, Any]) -> dict[str, Any]:
    """Run the compiled workflow's ``call_model`` node against a stub gateway answering ``message``."""
    spec = {"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}
    config = SimpleNamespace(
        configuration_revision=1,
        endpoint_destination_id="d",
        gateway_base_url="https://x/v1",
        gateway_api_key="k",
        request_timeout_seconds=10,
        gateway_identity="a" * 64,
        model_identity="a" * 64,
        allow_public_providers=False,
        gateway_kind="anthropic",
    )
    context = MagicMock()
    context.remaining_active.return_value = 100.0
    context.elapsed.return_value = 0.0
    context.profile_snapshot = None
    context.prompt_version = "x"
    context.settings.ai_allowed_endpoint_cidrs = ()
    context.scope = MagicMock()
    context._run_snapshot = AsyncMock(return_value=SimpleNamespace(token_budget=None, tool_calls=0))
    context.authorize_remote_send = AsyncMock(
        return_value=(config, MagicMock(), "chat", MagicMock())
    )
    gateway = MagicMock()
    gateway.tools = AsyncMock(
        return_value={"choices": [{"message": message}], "usage": {"total_tokens": 5}}
    )
    monkeypatch.setattr(harness, "_reserve_step", AsyncMock())
    monkeypatch.setattr(harness, "_tool_specs", lambda _c: [spec])
    monkeypatch.setattr(gateway_factory, "ModelGateway", lambda **_kw: gateway)
    monkeypatch.setattr(gateway_factory.settings_public, "capability_evidence", lambda *_a: None)
    node = harness.build_workflow(context, None).nodes["call_model"].bound
    state = {
        "prompt": "p",
        "messages": [],
        "segment_steps": 0,
        "source_fences": [],
        "token_usage": None,
    }
    return await node.ainvoke(state) if hasattr(node, "ainvoke") else await node(state)


CALL = {"id": "toolu_1", "type": "function", "function": {"name": "t", "arguments": "{}"}}


@pytest.mark.asyncio
async def test_harness_keeps_reasoning_details_on_assistant_tool_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = await _call_model(
        monkeypatch, {"tool_calls": [CALL], "reasoning_details": [*THINKING, "junk"]}
    )
    assert state["messages"][-1]["reasoning_details"] == THINKING


@pytest.mark.asyncio
async def test_harness_assistant_turn_unchanged_without_reasoning_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = await _call_model(monkeypatch, {"tool_calls": [CALL]})
    assert "reasoning_details" not in state["messages"][-1]
