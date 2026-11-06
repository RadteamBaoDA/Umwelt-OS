"""WireProtocol registry/capabilities and the gateway behaviours that depend on them (no network)."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.model_gateway import wire
from core.model_gateway.client import (
    CapabilityUnsupported,
    LocalCapabilityError,
    ModelGateway,
    ModelGatewayError,
)
from core.model_gateway.schemas import ModelMapping
from core.model_gateway.wire import (
    AnthropicMessagesProtocol,
    OpenAICompatProtocol,
    register_wire_protocol,
    wire_protocol_for,
)
from tests.unit.core.test_anthropic_transport import MESSAGE, SSE, USER
from tests.unit.core.test_model_gateway import _MAPPING, _POLICY, _gateway

MAX_TOKENS_400 = {
    "type": "error",
    "error": {
        "type": "invalid_request_error",
        "message": "max_tokens: 32000 > 8192, which is the maximum allowed",
    },
}
EFFORT_400 = {
    "type": "error",
    "error": {"type": "invalid_request_error", "message": "thinking is not supported here"},
}


@asynccontextmanager
async def _null() -> Any:
    """Stand-in for the Redis capacity lease."""
    yield


@pytest.fixture(autouse=True)
def _fences() -> Any:
    """Neutralise Redis-backed lease/breaker and the retry sleep."""
    with (
        patch.object(ModelGateway, "_lease", side_effect=lambda *_: _null()),
        patch.object(ModelGateway, "_breaker_check", AsyncMock()),
        patch.object(ModelGateway, "_breaker_ok", AsyncMock()),
        patch.object(ModelGateway, "_breaker_fail", AsyncMock()),
        patch("core.model_gateway.client._retry_delay", return_value=0),
    ):
        yield


def _gw(
    handler: Callable[[httpx.Request], httpx.Response],
    seen: list[httpx.Request],
    *,
    kind: str,
    evidence: Any = None,
) -> ModelGateway:
    """Gateway of ``kind`` answering through ``handler``; requests are recorded in ``seen``."""
    redis = MagicMock()
    redis.exists = AsyncMock(return_value=0)
    redis.set = AsyncMock()
    base = "https://api.anthropic.com/v1" if kind == "anthropic" else "https://x.example/v1"
    gw = _gateway(redis, base_url=base, kind=kind, destination_id="d", evidence=evidence)

    def record(request: httpx.Request) -> httpx.Response:
        """Remember then answer."""
        seen.append(request)
        return handler(request)

    patcher = patch.object(
        ModelGateway,
        "_http_client",
        side_effect=lambda _u: httpx.AsyncClient(transport=httpx.MockTransport(record)),
    )
    patcher.start()
    return gw


@pytest.fixture(autouse=True)
def _stop_patches() -> Any:
    """Undo any ``_http_client`` patch started by ``_gw``."""
    yield
    patch.stopall()


def test_registry_selects_protocol_once_from_kind() -> None:
    """anthropic -> Messages protocol; every other kind (including unknown) -> OpenAI-compatible."""
    assert isinstance(wire_protocol_for("anthropic"), AnthropicMessagesProtocol)
    for kind in ("openai", "openrouter", "groq", "custom", "something-new"):
        assert isinstance(wire_protocol_for(kind), OpenAICompatProtocol), kind
    gw = _gateway(MagicMock(), kind="anthropic")
    assert gw.protocol is wire_protocol_for("anthropic")
    assert _gateway(MagicMock(), kind="openrouter").protocol is wire_protocol_for("openrouter")


def test_capability_flags_replace_kind_checks() -> None:
    """Only the Anthropic protocol echoes reasoning_details and requires max_tokens; it lacks non-chat paths."""
    anthropic, compat = wire_protocol_for("anthropic"), wire_protocol_for("custom")
    assert anthropic.echoes_reasoning_details and anthropic.requires_max_tokens
    assert not compat.echoes_reasoning_details and not compat.requires_max_tokens
    assert anthropic.supports_path("chat/completions") and not anthropic.supports_path("embeddings")
    assert compat.supports_path("embeddings") and compat.supports_path("rerank")


def test_new_vendor_protocol_registers_without_editing_the_client() -> None:
    """A registered kind is picked up by a new gateway; the registry is restored afterwards."""

    class Fake(OpenAICompatProtocol):
        """Marker protocol with its own flags."""

        name = "fake"
        echoes_reasoning_details = True

    registry = wire._REGISTRY
    try:
        register_wire_protocol("fake-vendor", Fake())
        assert wire_protocol_for("fake-vendor").name == "fake"
        assert wire_protocol_for("fake-vendor").echoes_reasoning_details is True
    finally:
        registry.pop("fake-vendor", None)
    assert (
        isinstance(wire_protocol_for("fake-vendor"), OpenAICompatProtocol)
        and wire_protocol_for("fake-vendor").name == "openai"
    )


@pytest.mark.asyncio
async def test_anthropic_gateway_rejects_embeddings_before_io() -> None:
    """The protocol's path support drives the early CapabilityUnsupported (no request is sent)."""
    seen: list[httpx.Request] = []
    gw = _gw(lambda _r: httpx.Response(200, json={}), seen, kind="anthropic")
    with pytest.raises(CapabilityUnsupported):
        await gw.embed("embeddings", _MAPPING, _POLICY, ["x"], before_send=AsyncMock())
    assert seen == []


@pytest.mark.asyncio
async def test_max_tokens_400_is_reclassified_only_for_protocols_requiring_it() -> None:
    """Anthropic: gateway error. OpenRouter/custom: the normal CapabilityUnsupported mapping (no reclassification)."""
    for kind, expected in (
        ("anthropic", ModelGatewayError),
        ("openrouter", CapabilityUnsupported),
        ("custom", CapabilityUnsupported),
    ):
        seen: list[httpx.Request] = []
        gw = _gw(lambda _r: httpx.Response(400, json=MAX_TOKENS_400), seen, kind=kind)
        with pytest.raises(expected) as exc:
            await gw.chat("chat", _MAPPING, _POLICY, USER, max_tokens=64, before_send=AsyncMock())
        assert isinstance(exc.value, CapabilityUnsupported) is (
            expected is CapabilityUnsupported
        ), kind
        # Streaming applies the same rule.
        gw = _gw(lambda _r: httpx.Response(400, json=MAX_TOKENS_400), seen, kind=kind)
        with pytest.raises(expected) as exc:
            _ = [
                x async for x in gw.stream("chat", _MAPPING, _POLICY, USER, before_send=AsyncMock())
            ]
        assert isinstance(exc.value, CapabilityUnsupported) is (
            expected is CapabilityUnsupported
        ), kind


@pytest.mark.asyncio
async def test_stream_max_tokens_clamp_still_runs_after_reasoning_effort_retry() -> None:
    """effort 400 -> drop it; then max_tokens 400 -> clamp to 4096; third request succeeds (one of each)."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Reject thinking first, then the oversize max_tokens, then accept."""
        body = json.loads(request.content)
        if "thinking" in body:
            return httpx.Response(400, json=EFFORT_400)
        if body["max_tokens"] > 4096:
            return httpx.Response(400, json=MAX_TOKENS_400)
        return httpx.Response(200, text=SSE, headers={"content-type": "text/event-stream"})

    mapping = ModelMapping(model="m", version="1", destination="remote", reasoning_effort="high")
    gw = _gw(handler, seen, kind="anthropic")
    lines = [x async for x in gw.stream("chat", mapping, _POLICY, USER, before_send=AsyncMock())]
    bodies = [json.loads(r.content) for r in seen]
    assert [("thinking" in b, b["max_tokens"]) for b in bodies] == [
        (True, 32000),
        (False, 32000),
        (False, 4096),
    ]
    assert lines[-1] == "data: [DONE]"


@pytest.mark.asyncio
async def test_stream_effort_drop_consumes_the_transient_retry_budget() -> None:
    """effort 400 -> drop it; a following 503 is not retried again: two opens, breaker failure counted."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Reject thinking, then fail upstream."""
        if "thinking" in json.loads(request.content):
            return httpx.Response(400, json=EFFORT_400)
        return httpx.Response(
            503, json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
        )

    mapping = ModelMapping(model="m", version="1", destination="remote", reasoning_effort="high")
    gw = _gw(handler, seen, kind="anthropic")
    with pytest.raises(ModelGatewayError):
        _ = [x async for x in gw.stream("chat", mapping, _POLICY, USER, before_send=AsyncMock())]
    assert len(seen) == 2
    assert ModelGateway._breaker_fail.await_count >= 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_stream_clamp_then_503_uses_the_single_retry_budget() -> None:
    """max_tokens 400 -> clamp; a following 503 is not retried: two opens, breaker failure counted."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Reject the oversize max_tokens, then fail upstream."""
        if json.loads(request.content)["max_tokens"] > 4096:
            return httpx.Response(400, json=MAX_TOKENS_400)
        return httpx.Response(
            503, json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
        )

    gw = _gw(handler, seen, kind="anthropic")
    with pytest.raises(ModelGatewayError):
        _ = [x async for x in gw.stream("chat", _MAPPING, _POLICY, USER, before_send=AsyncMock())]
    assert len(seen) == 2
    assert ModelGateway._breaker_fail.await_count >= 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_stream_timeout_then_max_tokens_400_is_not_clamped() -> None:
    """A transient retry already used the budget: a later max_tokens 400 surfaces with no third open."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Time out once, then answer the oversize max_tokens 400."""
        if not seen[:-1]:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(400, json=MAX_TOKENS_400)

    gw = _gw(handler, seen, kind="anthropic")
    with pytest.raises(ModelGatewayError):
        _ = [x async for x in gw.stream("chat", _MAPPING, _POLICY, USER, before_send=AsyncMock())]
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_stream_retries_are_bounded_when_every_attempt_fails() -> None:
    """A persistent max_tokens 400 gets at most one clamp retry, then the gateway error surfaces."""
    seen: list[httpx.Request] = []
    gw = _gw(lambda _r: httpx.Response(400, json=MAX_TOKENS_400), seen, kind="anthropic")
    with pytest.raises(ModelGatewayError):
        _ = [x async for x in gw.stream("chat", _MAPPING, _POLICY, USER, before_send=AsyncMock())]
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_local_tool_choice_translation_error_is_not_recorded_as_evidence() -> None:
    """A forced tool_choice the gateway cannot translate is local: callers still get CapabilityUnsupported, no evidence."""
    evidence = MagicMock(lookup=AsyncMock(return_value=None), record=AsyncMock())
    seen: list[httpx.Request] = []
    gw = _gw(
        lambda _r: httpx.Response(200, json=MESSAGE), seen, kind="anthropic", evidence=evidence
    )
    with pytest.raises(LocalCapabilityError) as exc:
        await gw.tools(
            "chat", _MAPPING, _POLICY, USER, [], tool_choice="required", before_send=AsyncMock()
        )
    assert isinstance(exc.value, CapabilityUnsupported)
    assert seen == [] and evidence.record.await_count == 0
    with pytest.raises(LocalCapabilityError):
        _ = [
            x
            async for x in gw.stream(
                "chat", _MAPPING, _POLICY, USER, tool_choice="required", before_send=AsyncMock()
            )
        ]
    assert evidence.record.await_count == 0
