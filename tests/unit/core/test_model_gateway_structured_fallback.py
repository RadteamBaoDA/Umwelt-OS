"""Structured output tries native json_schema, then falls back to app-side JSON extraction on rejection."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.model_gateway.client import CapabilityUnsupported, ModelGateway, _extract_json
from tests.unit.core.test_model_gateway import _MAPPING, _gateway, _policy

SCHEMA = {"name": "facts", "schema": {"type": "object", "properties": {"a": {"type": "integer"}}}}


def _reply(content: str) -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": content}}], "model": "m"}


class _Store:
    """In-memory evidence store: ``known`` seeds lookups, ``records`` captures writes."""

    def __init__(self, known: dict[str, str] | None = None) -> None:
        self.known = known or {}
        self.records: list[tuple[str, str]] = []

    async def lookup(self, role: str, mapping: Any, capability: str) -> str | None:
        return self.known.get(capability)

    async def record(self, role: str, mapping: Any, capability: str, result: str) -> None:
        self.records.append((capability, result))


async def _call(replies: list[Any], store: _Store | None = None) -> tuple[Any, AsyncMock]:
    """Run structured() against canned _request outcomes (an Exception entry is raised)."""
    sent = AsyncMock(side_effect=replies)
    with patch.object(ModelGateway, "_request", sent):
        result = await _gateway(MagicMock(), evidence=store).structured(
            "background", _MAPPING, _policy(), [{"role": "user", "content": "x"}], SCHEMA, before_send=AsyncMock())
    return result, sent


@pytest.mark.asyncio
async def test_native_structured_is_tried_first_without_any_evidence() -> None:
    result, sent = await _call([_reply('{"a": 1}')])
    assert sent.await_count == 1
    assert sent.await_args.args[5]["response_format"]["type"] == "json_schema"
    assert result["choices"][0]["message"]["content"] == '{"a": 1}'


@pytest.mark.asyncio
async def test_native_rejection_falls_back_to_prompted_json_and_records() -> None:
    store = _Store()
    result, sent = await _call([CapabilityUnsupported("Gateway rejected (HTTP 400: response_format)"),
                                _reply('Sure!\n```json\n{"a": 2}\n```')], store)
    payload = sent.await_args_list[1].args[5]
    assert "response_format" not in payload
    assert payload["messages"][0]["role"] == "system" and '"a"' in payload["messages"][0]["content"]
    assert sent.await_args_list[1].args[3] == "structured"  # privacy is still decided for the structured capability
    assert sent.await_args_list[1].kwargs["record_as"] == "chat"
    assert result["choices"][0]["message"]["content"] == '{"a": 2}'
    assert store.records == [("structured", "unsupported")]


@pytest.mark.asyncio
async def test_stored_unsupported_skips_native_attempt() -> None:
    _result, sent = await _call([_reply('{"a": 3}')], _Store({"structured": "unsupported"}))
    assert sent.await_count == 1 and "response_format" not in sent.await_args.args[5]


@pytest.mark.asyncio
async def test_fallback_failure_propagates_real_error() -> None:
    with pytest.raises(CapabilityUnsupported, match="HTTP 400: nope"):
        await _call([CapabilityUnsupported("native"), CapabilityUnsupported("Gateway rejected (HTTP 400: nope)")])


@pytest.mark.parametrize(("text", "expected"), [
    ('{"a":1}', '{"a":1}'),
    ('<think>plan {x}</think>{"a":1}', '{"a":1}'),
    ('```json\n[1, 2]\n```', "[1, 2]"),
    ('Result: {"a": {"b": 1}} done', '{"a": {"b": 1}}'),
    ("not json", "not json"),
])
def test_extract_json(text: str, expected: str) -> None:
    assert _extract_json(text) == expected
