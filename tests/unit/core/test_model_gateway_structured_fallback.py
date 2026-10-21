"""Structured output falls back to app-side JSON extraction when native json_schema is not verified."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from core.model_gateway.cache import capability_key
from core.model_gateway.client import ModelGateway, _extract_json, structured_evidence
from tests.unit.core.test_model_gateway import _MAPPING, _gateway, _policy

SCHEMA = {"name": "facts", "schema": {"type": "object", "properties": {"a": {"type": "integer"}}}}


def _reply(content: str) -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": content}}], "model": "m"}


async def _call(verified: set[str], content: str) -> tuple[Any, AsyncMock]:
    """Run structured() with the given verified capabilities and a canned upstream reply."""
    sent = AsyncMock(return_value=_reply(content))

    async def supported(_self: Any, _role: str, _mapping: Any, _policy: Any, capability: str) -> bool:
        return capability in verified

    with patch.object(ModelGateway, "_capability_supported", supported), patch.object(ModelGateway, "_request", sent):
        result = await _gateway(MagicMock()).structured(
            "background", _MAPPING, _policy(), [{"role": "user", "content": "x"}], SCHEMA, before_send=AsyncMock())
    return result, sent


@pytest.mark.asyncio
async def test_native_structured_keeps_json_schema() -> None:
    result, sent = await _call({"structured", "chat"}, '{"a": 1}')
    payload = sent.await_args.args[5]
    assert payload["response_format"]["type"] == "json_schema"
    assert "evidence" not in sent.await_args.kwargs
    assert result["choices"][0]["message"]["content"] == '{"a": 1}'


@pytest.mark.asyncio
async def test_falls_back_to_prompted_json_on_chat_evidence() -> None:
    result, sent = await _call({"chat"}, 'Sure!\n```json\n{"a": 2}\n```')
    payload, kwargs = sent.await_args.args[5], sent.await_args.kwargs
    assert "response_format" not in payload
    assert payload["messages"][0]["role"] == "system" and '"a"' in payload["messages"][0]["content"]
    assert sent.await_args.args[3] == "structured"  # privacy is still decided for the structured capability
    assert kwargs["evidence"] == "chat"
    assert result["choices"][0]["message"]["content"] == '{"a": 2}'


@pytest.mark.asyncio
async def test_without_any_evidence_the_gateway_gate_still_applies() -> None:
    _result, sent = await _call(set(), '{"a": 3}')
    assert "response_format" in sent.await_args.args[5]  # unchanged path: _request enforces its own gate


@pytest.mark.parametrize(("text", "expected"), [
    ('{"a":1}', '{"a":1}'),
    ('<think>plan {x}</think>{"a":1}', '{"a":1}'),
    ('```json\n[1, 2]\n```', "[1, 2]"),
    ('Result: {"a": {"b": 1}} done', '{"a": {"b": 1}}'),
    ("not json", "not json"),
])
def test_extract_json(text: str, expected: str) -> None:
    assert _extract_json(text) == expected


WS = UUID("00000000-0000-0000-0000-000000000001")
GW = "a" * 64


def _evidence(capability: str, *, model: str = "m", hours: int = 1, result: str = "supported") -> str:
    now = datetime.now(UTC)
    return json.dumps({
        "workspace_id": str(WS), "actor_user_id": 7, "membership_revision": 3,
        "role": "background", "model": model, "version": "1", "gateway_identity": GW,
        "configuration_revision": 1, "capability": capability, "result": result,
        "checked_at": now.isoformat(), "expires_at": (now + timedelta(hours=hours)).isoformat(),
    })


async def _evidence_for(stored: dict[str, str]) -> Any:
    """Resolve evidence from a fake Redis keyed exactly like the gateway cache."""
    table = {capability_key("background", "m", "1", capability, GW, workspace_id=WS, actor_user_id=7): value
             for capability, value in stored.items()}
    redis = MagicMock(get=AsyncMock(side_effect=table.get))
    return await structured_evidence(
        redis, "background", _MAPPING, gateway_identity=GW, configuration_revision=1, workspace_id=WS, actor_user_id=7)


@pytest.mark.asyncio
async def test_evidence_prefers_native_structured() -> None:
    found = await _evidence_for({"structured": _evidence("structured"), "chat": _evidence("chat")})
    assert found is not None and found.capability == "structured"


@pytest.mark.asyncio
async def test_evidence_falls_back_to_chat() -> None:
    found = await _evidence_for({"structured": _evidence("structured", result="unsupported"), "chat": _evidence("chat")})
    assert found is not None and found.capability == "chat"


@pytest.mark.asyncio
async def test_evidence_rejects_expired_or_other_model() -> None:
    assert await _evidence_for({"chat": _evidence("chat", hours=-1)}) is None
    assert await _evidence_for({"chat": _evidence("chat", model="other")}) is None
    assert await _evidence_for({}) is None
