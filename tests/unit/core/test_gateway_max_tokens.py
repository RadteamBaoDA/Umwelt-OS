"""Per-provider ``max_tokens``: schema, request resolution order and the Anthropic ``max_tokens`` 400 retry."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from pydantic import ValidationError

from core.model_gateway.client import CapabilityUnsupported, ModelGateway, ModelGatewayError
from core.model_gateway.schemas import PROVIDER_KINDS_WITHOUT_MAX_TOKENS, ModelMapping
from tests.unit.core.test_anthropic_transport import MESSAGE, SSE, USER
from tests.unit.core.test_model_gateway import _MAPPING, _POLICY, _gateway

MAX_TOKENS_400 = {"type": "error", "error": {"type": "invalid_request_error",
                                             "message": "max_tokens: 32000 > 8192, which is the maximum allowed"}}


@asynccontextmanager
async def _null() -> Any:
    """Stand-in for the Redis capacity lease."""
    yield


@pytest.fixture(autouse=True)
def _fences() -> Any:
    """Neutralise Redis-backed lease/breaker and the retry sleep."""
    with patch.object(ModelGateway, "_lease", side_effect=lambda *_: _null()), \
         patch.object(ModelGateway, "_breaker_check", AsyncMock()), patch.object(ModelGateway, "_breaker_ok", AsyncMock()), \
         patch.object(ModelGateway, "_breaker_fail", AsyncMock()), \
         patch("core.model_gateway.client._retry_delay", return_value=0):
        yield


def _gw(handler: Callable[[httpx.Request], httpx.Response], seen: list[httpx.Request], *, kind: str,
        clamped: bool = False) -> ModelGateway:
    """Gateway of ``kind`` whose pinned http client answers through ``handler`` and records requests in ``seen``."""
    redis = MagicMock()
    redis.exists = AsyncMock(return_value=1 if clamped else 0)
    redis.set = AsyncMock()
    base = "https://api.anthropic.com/v1" if kind == "anthropic" else "https://x.example/v1"
    gw = _gateway(redis, base_url=base, kind=kind, destination_id="d")

    def record(request: httpx.Request) -> httpx.Response:
        """Remember then answer."""
        seen.append(request)
        return handler(request)

    patcher = patch.object(ModelGateway, "_http_client",
                           side_effect=lambda _u: httpx.AsyncClient(transport=httpx.MockTransport(record)))
    patcher.start()
    return gw


@pytest.fixture(autouse=True)
def _stop_patches() -> Any:
    """Undo any ``_http_client`` patch started by ``_gw``."""
    yield
    patch.stopall()


def _ok_openai(_r: httpx.Request) -> httpx.Response:
    """Minimal OpenAI chat.completion reply."""
    return httpx.Response(200, json={"id": "c", "object": "chat.completion", "created": 1, "model": "m", "choices": [
        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}]})


def test_schema_field_bounds_and_hosted_kind_constant() -> None:
    """max_output_tokens defaults to None and is bounded 1..1_000_000; hosted kinds are anthropic/openai/groq."""
    assert ModelMapping(model="m").max_output_tokens is None
    assert ModelMapping(model="m", max_output_tokens=1_000_000).max_output_tokens == 1_000_000
    for bad in (0, -1, 1_000_001):
        with pytest.raises(ValidationError):
            ModelMapping(model="m", max_output_tokens=bad)
    assert frozenset({"anthropic", "openai", "groq"}) == PROVIDER_KINDS_WITHOUT_MAX_TOKENS


@pytest.mark.asyncio
async def test_openai_compatible_sends_configured_max_tokens_only_for_other_kinds() -> None:
    """custom/openrouter send the mapping value; openai/groq ignore a stray value (server-side re-check)."""
    mapping = ModelMapping(model="m", version="1", destination="remote", max_output_tokens=777)
    for kind, expected in (("custom", 777), ("openrouter", 777), ("openai", None), ("groq", None)):
        seen: list[httpx.Request] = []
        gw = _gw(_ok_openai, seen, kind=kind)
        await gw.chat("chat", mapping, _POLICY, USER, before_send=AsyncMock())
        assert json.loads(seen[0].content).get("max_tokens") == expected, kind


@pytest.mark.asyncio
async def test_explicit_caller_value_beats_mapping_and_unset_sends_nothing() -> None:
    """Resolution order: explicit argument, then mapping, then nothing."""
    mapping = ModelMapping(model="m", version="1", destination="remote", max_output_tokens=777)
    seen: list[httpx.Request] = []
    gw = _gw(_ok_openai, seen, kind="custom")
    await gw.chat("chat", mapping, _POLICY, USER, max_tokens=64, before_send=AsyncMock())
    await gw.chat("chat", _MAPPING, _POLICY, USER, before_send=AsyncMock())
    assert json.loads(seen[0].content)["max_tokens"] == 64
    assert "max_tokens" not in json.loads(seen[1].content)


@pytest.mark.asyncio
async def test_anthropic_ignores_mapping_value_and_streams_with_internal_default() -> None:
    """Anthropic never uses max_output_tokens; the internal 32000 streaming default is sent."""
    mapping = ModelMapping(model="m", version="1", destination="remote", max_output_tokens=777)
    seen: list[httpx.Request] = []
    gw = _gw(lambda _r: httpx.Response(200, text=SSE, headers={"content-type": "text/event-stream"}), seen, kind="anthropic")
    _ = [x async for x in gw.stream("chat", mapping, _POLICY, USER, before_send=AsyncMock())]
    assert json.loads(seen[0].content)["max_tokens"] == 32000


@pytest.mark.asyncio
async def test_anthropic_stream_max_tokens_400_retries_once_with_default_and_caches_clamp() -> None:
    """A max_tokens 400 lowers 32000 to 4096 exactly once and remembers the clamp for one hour."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Reject the large default, accept the clamped one."""
        if json.loads(request.content)["max_tokens"] > 4096:
            return httpx.Response(400, json=MAX_TOKENS_400)
        return httpx.Response(200, text=SSE, headers={"content-type": "text/event-stream"})

    gw = _gw(handler, seen, kind="anthropic")
    lines = [x async for x in gw.stream("chat", _MAPPING, _POLICY, USER, before_send=AsyncMock())]
    assert [json.loads(r.content)["max_tokens"] for r in seen] == [32000, 4096] and lines[-1] == "data: [DONE]"
    assert gw.redis.set.await_args.kwargs == {"ex": 3600}  # type: ignore[attr-defined]
    assert gw.redis.set.await_args.args[0].endswith(f"{gw.model_identity}:m")  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_anthropic_cached_clamp_applies_first_and_failure_is_not_capability_unsupported() -> None:
    """A cached clamp sends 4096 up front; a max_tokens 400 that persists is a gateway error, never 'unsupported'."""
    seen: list[httpx.Request] = []
    gw = _gw(lambda _r: httpx.Response(400, json=MAX_TOKENS_400), seen, kind="anthropic", clamped=True)
    evidence = MagicMock(lookup=AsyncMock(return_value=None), record=AsyncMock())
    object.__setattr__(gw, "evidence", evidence)
    with pytest.raises(ModelGatewayError) as exc:
        _ = [x async for x in gw.stream("chat", _MAPPING, _POLICY, USER, before_send=AsyncMock())]
    assert not isinstance(exc.value, CapabilityUnsupported)
    assert [json.loads(r.content)["max_tokens"] for r in seen] == [4096]
    assert all(call.args[3] != "unsupported" for call in evidence.record.await_args_list)


@pytest.mark.asyncio
async def test_anthropic_chat_explicit_value_above_default_is_clamped_on_400() -> None:
    """Non-stream chat honours an explicit 8000 and retries once with 4096 on a max_tokens 400."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Accept only the clamped value."""
        if json.loads(request.content)["max_tokens"] > 4096:
            return httpx.Response(400, json=MAX_TOKENS_400)
        return httpx.Response(200, json=MESSAGE)

    gw = _gw(handler, seen, kind="anthropic")
    out = await gw.chat("chat", _MAPPING, _POLICY, USER, max_tokens=8000, before_send=AsyncMock())
    assert [json.loads(r.content)["max_tokens"] for r in seen] == [8000, 4096]
    assert out["choices"][0]["message"]["content"] == "ready"
