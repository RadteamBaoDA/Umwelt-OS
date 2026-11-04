"""ModelGateway routes kind=anthropic through the native Messages API (httpx MockTransport, no network)."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.model_gateway.client import CapabilityUnsupported, ModelGateway, ModelGatewayError
from tests.unit.core.test_model_gateway import _MAPPING, _POLICY, _gateway

MESSAGE = {"id": "msg_1", "model": "claude-sonnet-5-5", "stop_reason": "end_turn",
           "content": [{"type": "text", "text": "ready"}], "usage": {"input_tokens": 1, "output_tokens": 1}}
USER = [{"role": "user", "content": "x"}]
_EVENTS = [
    {"type": "message_start", "message": {"id": "msg_1", "model": "m", "usage": {"input_tokens": 2}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}},
    {"type": "message_stop"},
]
SSE = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in _EVENTS)


@asynccontextmanager
async def _null() -> Any:
    """Stand-in for the Redis capacity lease."""
    yield


def _json(status: int, body: dict[str, Any], **headers: str) -> httpx.Response:
    """JSON reply with optional headers."""
    return httpx.Response(status, json=body, headers=headers)


def _gw(handler: Callable[[httpx.Request], httpx.Response], seen: list[httpx.Request], kind: str = "anthropic",
        base_url: str = "https://api.anthropic.com/v1") -> tuple[ModelGateway, Any]:
    """Gateway plus a transport handler that records every outgoing request before the test handler answers."""
    def record(request: httpx.Request) -> httpx.Response:
        """Remember the request, then let the test handler answer."""
        seen.append(request)
        return handler(request)

    gw = _gateway(MagicMock(), base_url=base_url, kind=kind, destination_id="d")
    return gw, record


@pytest.fixture(autouse=True)
def _fences() -> Any:
    """Neutralise Redis-backed lease/breaker and the retry jitter sleep."""
    with patch.object(ModelGateway, "_lease", side_effect=lambda *_: _null()), \
         patch.object(ModelGateway, "_breaker_check", AsyncMock()), patch.object(ModelGateway, "_breaker_ok", AsyncMock()), \
         patch.object(ModelGateway, "_breaker_fail", AsyncMock()), \
         patch("core.model_gateway.client._retry_delay", return_value=0):
        yield


def _patched(gw: ModelGateway, record: Callable[[httpx.Request], httpx.Response]) -> Any:
    """Patch the gateway's pinned http client factory with a MockTransport client."""
    return patch.object(ModelGateway, "_http_client",
                        side_effect=lambda _url: httpx.AsyncClient(transport=httpx.MockTransport(record)))


@pytest.mark.asyncio
async def test_chat_posts_native_messages_with_anthropic_headers() -> None:
    seen: list[httpx.Request] = []
    gw, record = _gw(lambda _r: _json(200, MESSAGE), seen)
    with _patched(gw, record):
        out = await gw.chat("chat", _MAPPING, _POLICY, USER, temperature=0.2, before_send=AsyncMock())
    req = seen[0]
    assert str(req.url) == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == "sk-test" and req.headers["anthropic-version"] == "2026-10-21"
    assert "authorization" not in req.headers
    assert "temperature" not in json.loads(req.content) and json.loads(req.content)["max_tokens"] == 4096
    assert out["choices"][0]["message"]["content"] == "ready"


@pytest.mark.asyncio
async def test_429_with_retry_after_is_retried_once_then_succeeds() -> None:
    seen: list[httpx.Request] = []
    replies = [_json(429, {"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}}, **{"retry-after": "0"}),
               _json(200, MESSAGE)]
    gw, record = _gw(lambda _r: replies.pop(0), seen)
    with _patched(gw, record):
        out = await gw.chat("chat", _MAPPING, _POLICY, USER, before_send=AsyncMock())
    assert len(seen) == 2 and out["choices"][0]["message"]["content"] == "ready"


@pytest.mark.asyncio
async def test_auth_error_maps_to_gateway_error_without_retry() -> None:
    seen: list[httpx.Request] = []
    gw, record = _gw(lambda _r: _json(401, {"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}), seen)
    with _patched(gw, record), pytest.raises(ModelGatewayError, match="401") as exc:
        await gw.chat("chat", _MAPPING, _POLICY, USER, before_send=AsyncMock())
    assert not isinstance(exc.value, CapabilityUnsupported) and "authentication_error" in str(exc.value)
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_400_maps_to_capability_unsupported_and_structured_falls_back_to_prompted_json() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Reject native structured output, accept the prompted variant."""
        if "output_config" in json.loads(request.content):
            return _json(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "no schema"}})
        return _json(200, {**MESSAGE, "content": [{"type": "text", "text": "{\"query\":\"q\"}"}]})

    gw, record = _gw(handler, seen)
    schema = {"name": "q", "strict": True, "schema": {"type": "object"}}
    with _patched(gw, record):
        out = await gw.structured("background", _MAPPING, _POLICY, USER, schema, before_send=AsyncMock())
    assert json.loads(out["choices"][0]["message"]["content"]) == {"query": "q"}
    assert "output_config" in json.loads(seen[0].content) and "output_config" not in json.loads(seen[1].content)


@pytest.mark.asyncio
async def test_stream_yields_openai_shaped_sse_lines_and_done() -> None:
    seen: list[httpx.Request] = []
    gw, record = _gw(lambda _r: httpx.Response(200, text=SSE, headers={"content-type": "text/event-stream"}), seen)
    with _patched(gw, record):
        lines = [line async for line in gw.stream("chat", _MAPPING, _POLICY, USER, before_send=AsyncMock())]
    assert lines[-1] == "data: [DONE]" and any('"content": "Hello"' in line for line in lines)
    assert json.loads(seen[0].content)["stream"] is True
    assert any('"total_tokens": 5' in line for line in lines)


@pytest.mark.asyncio
async def test_stream_error_status_before_first_chunk_maps_to_gateway_error() -> None:
    seen: list[httpx.Request] = []
    gw, record = _gw(lambda _r: _json(401, {"type": "error", "error": {"type": "authentication_error", "message": "x"}}), seen)
    with _patched(gw, record), pytest.raises(ModelGatewayError, match="401"):
        _ = [line async for line in gw.stream("chat", _MAPPING, _POLICY, USER, before_send=AsyncMock())]


@pytest.mark.asyncio
async def test_embeddings_and_rerank_are_unsupported_without_io() -> None:
    seen: list[httpx.Request] = []
    gw, record = _gw(lambda _r: _json(200, {}), seen)
    with _patched(gw, record):
        with pytest.raises(CapabilityUnsupported):
            await gw.embed("embedding", _MAPPING, _POLICY, ["x"], probe=True)
        with pytest.raises(CapabilityUnsupported):
            await gw.rerank("reranker", _MAPPING, _POLICY, "q", ["d"], probe=True)
    assert seen == []


@pytest.mark.asyncio
async def test_discover_lists_anthropic_models_with_native_headers() -> None:
    seen: list[httpx.Request] = []
    body = {"data": [{"id": "claude-opus-5-5", "type": "model"}, {"id": "claude-haiku-5-5"}, {"id": ""}], "has_more": False}
    gw, record = _gw(lambda _r: _json(200, body), seen)
    with _patched(gw, record):
        assert await gw.discover_models() == ["claude-opus-5-5", "claude-haiku-5-5"]
    assert seen[0].headers["x-api-key"] == "sk-test" and seen[0].url.params["limit"] == "1000"
    assert seen[0].method == "GET" and "authorization" not in seen[0].headers


@pytest.mark.asyncio
async def test_discover_failure_has_static_reason() -> None:
    seen: list[httpx.Request] = []
    gw, record = _gw(lambda _r: _json(401, {"error": {"type": "authentication_error", "message": "sk-secret"}}), seen)
    with _patched(gw, record), pytest.raises(ModelGatewayError, match="HTTP 401") as exc:
        await gw.discover_models()
    assert "sk-secret" not in str(exc.value)


@pytest.mark.asyncio
async def test_existing_openai_kind_still_uses_openai_sdk_path() -> None:
    seen: list[httpx.Request] = []
    reply = {"id": "c", "object": "chat.completion", "created": 1, "model": "m",
             "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "hi"}}]}
    gw, record = _gw(lambda _r: _json(200, reply), seen, kind="openai", base_url="https://api.openai.com/v1")
    with _patched(gw, record):
        out = await gw.chat("chat", _MAPPING, _POLICY, USER, before_send=AsyncMock())
    assert str(seen[0].url) == "https://api.openai.com/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer sk-test" and out["choices"][0]["message"]["content"] == "hi"


@pytest.mark.asyncio
async def test_non_json_2xx_maps_to_gateway_error_for_chat_and_discover() -> None:
    seen: list[httpx.Request] = []
    gw, record = _gw(lambda _r: httpx.Response(200, text="<html>proxy</html>", headers={"content-type": "text/plain"}), seen)
    with _patched(gw, record):
        with pytest.raises(ModelGatewayError, match="invalid response"):
            await gw.chat("chat", _MAPPING, _POLICY, USER, before_send=AsyncMock())
        with pytest.raises(ModelGatewayError, match="invalid response"):
            await gw.discover_models()


def test_default_max_tokens_is_larger_when_streaming() -> None:
    from core.model_gateway.anthropic import to_messages_request
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    assert to_messages_request(body, stream=False)["max_tokens"] == 4096
    assert to_messages_request(body, stream=True)["max_tokens"] == 32000
    assert to_messages_request({**body, "max_tokens": 10}, stream=True)["max_tokens"] == 10


@pytest.mark.parametrize("choice", ["required", {"type": "function", "function": {"name": "f"}}])
def test_forced_tool_choice_is_downgraded_to_auto(choice: object) -> None:
    from core.model_gateway.anthropic import to_messages_request
    out = to_messages_request({"model": "m", "messages": [], "tool_choice": choice}, stream=False)
    assert "tool_choice" not in out
