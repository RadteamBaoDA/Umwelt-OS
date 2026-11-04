"""ModelGateway.stream forwards tool-calling parameters only when provided and without a capability gate."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from openai import APIStatusError

from core.model_gateway.client import CapabilityUnsupported, ModelGateway, ModelGatewayError
from core.model_gateway.schemas import ModelMapping, RequestPolicy
from core.workspaces.schemas import WorkspaceContext

WORKSPACE_ID = uuid4()
IDENTITY = "a" * 64
SCOPE = WorkspaceContext(user_id=7, workspace_id=WORKSPACE_ID, role="owner", membership_revision=3)
POLICY = RequestPolicy(
    workspace_id=WORKSPACE_ID, actor_user_id=7, membership_revision=3, gateway_identity=IDENTITY,
    configuration_revision=1, reasoning_allowed=True, permitted_destinations=frozenset({"d"}),
)
MAPPING = ModelMapping(model="m", version="1", destination="remote")
TOOLS = [{"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}]


class _Evidence:
    """In-memory CapabilityEvidenceStore: ``known`` seeds lookups; ``records`` captures what the gateway wrote."""

    def __init__(self, known: dict[str, str] | None = None, *, fail: bool = False) -> None:
        self.known = known or {}
        self.fail = fail
        self.records: list[tuple[str, str]] = []

    async def lookup(self, role: str, mapping: ModelMapping, capability: str) -> str | None:
        if self.fail:
            raise RuntimeError("db down")
        return self.known.get(capability)

    async def record(self, role: str, mapping: ModelMapping, capability: str, result: str) -> None:
        if self.fail:
            raise RuntimeError("db down")
        self.records.append((capability, result))


def _gateway(evidence: _Evidence | None = None) -> ModelGateway:
    """Gateway with an optional evidence store; Redis holds no capability evidence any more."""
    redis = MagicMock()
    redis.get = AsyncMock(return_value=None)
    return ModelGateway(
        redis=redis, base_url="https://x/v1", api_key="k", destination_id="d", scope=SCOPE,
        gateway_identity=IDENTITY, configuration_revision=1, before_send=AsyncMock(), evidence=evidence,
    )


async def _run(gw: ModelGateway, *, error: Exception | None = None, **kwargs: Any) -> dict[str, Any]:
    """Drain one stream against a fake OpenAI client and return the kwargs given to create()."""
    seen: dict[str, Any] = {}

    async def create(**kw: Any) -> Any:
        seen.update(kw)
        if error is not None:
            raise error

        async def chunks() -> Any:
            yield MagicMock(model_dump=lambda **_: {"choices": [{"index": 0, "delta": {"content": "hi"}}]})

        return chunks()

    client = MagicMock()
    client.chat.completions.create = create

    @asynccontextmanager
    async def fake_openai(*_a: object, **_k: object) -> Any:
        yield client

    with patch("core.model_gateway.client.AsyncOpenAI", side_effect=fake_openai), \
         patch.object(ModelGateway, "_http_client", return_value=MagicMock()), \
         patch.object(ModelGateway, "_breaker_check", AsyncMock()), patch.object(ModelGateway, "_breaker_ok", AsyncMock()), \
         patch.object(ModelGateway, "_lease", side_effect=lambda *_: _null_ctx()):
        _ = [x async for x in gw.stream("chat", MAPPING, POLICY, [], **kwargs)]
    return seen


@asynccontextmanager
async def _null_ctx() -> Any:
    """Stand-in for the Redis capacity lease."""
    yield


def _status_error(status: int, message: str) -> APIStatusError:
    """Provider HTTP error as the OpenAI SDK raises it."""
    response = httpx.Response(status, request=httpx.Request("POST", "https://x/v1/chat/completions"))
    return APIStatusError(message, response=response, body=None)


@pytest.mark.asyncio
async def test_tools_forwarded_when_given() -> None:
    """tools, tool_choice and max_tokens reach chat.completions.create."""
    seen = await _run(_gateway(), tools=TOOLS, tool_choice="auto", max_tokens=256)
    assert seen["tools"] == TOOLS and seen["tool_choice"] == "auto" and seen["max_tokens"] == 256


@pytest.mark.asyncio
async def test_tools_absent_not_forwarded() -> None:
    """Without tools the request body keeps its previous shape."""
    seen = await _run(_gateway())
    assert not {"tools", "tool_choice", "max_tokens"} & seen.keys()


@pytest.mark.asyncio
async def test_missing_evidence_does_not_block_stream_and_records_supported() -> None:
    """No evidence at all: the stream proceeds to the provider and the first chunk records streaming+tools supported."""
    evidence = _Evidence()
    seen = await _run(_gateway(evidence), tools=TOOLS)
    assert seen["tools"] == TOOLS
    assert ("streaming", "supported") in evidence.records and ("tools", "supported") in evidence.records


@pytest.mark.asyncio
async def test_unsupported_tools_evidence_raises_without_network() -> None:
    """Stored tools=unsupported raises CapabilityUnsupported before the provider is contacted."""
    seen: dict[str, Any] = {}
    with pytest.raises(CapabilityUnsupported):
        seen = await _run(_gateway(_Evidence({"tools": "unsupported"})), tools=TOOLS)
    assert seen == {}


@pytest.mark.asyncio
async def test_tools_rejection_is_recorded_unsupported() -> None:
    """A 400 naming tools is a clear capability rejection: raised for the caller's fallback and recorded."""
    evidence = _Evidence()
    with pytest.raises(CapabilityUnsupported):
        await _run(_gateway(evidence), tools=TOOLS, error=_status_error(400, "tools are not supported by this model"))
    assert ("tools", "unsupported") in evidence.records


@pytest.mark.asyncio
async def test_transient_error_is_never_recorded_unsupported() -> None:
    """A 500 carries the real status in the error and is recorded as failed, never unsupported."""
    evidence = _Evidence()
    with patch.object(ModelGateway, "_on_retryable_status", AsyncMock(return_value=False)),          pytest.raises(ModelGatewayError, match="HTTP 500"):
        await _run(_gateway(evidence), tools=TOOLS, error=_status_error(500, "upstream exploded"))
    assert ("streaming", "failed") in evidence.records
    assert all(result != "unsupported" for _cap, result in evidence.records)


@pytest.mark.asyncio
async def test_recorder_failure_does_not_fail_the_stream() -> None:
    """Evidence lookup/record errors are swallowed: the user's request still streams."""
    seen = await _run(_gateway(_Evidence(fail=True)), tools=TOOLS)
    assert seen["tools"] == TOOLS


@pytest.mark.asyncio
async def test_embed_cohere_uses_native_body_and_normalizes_reply() -> None:
    """Cohere ids get texts/input_type and the embeddings.float reply becomes OpenAI-shaped data rows."""
    gw = _gateway()
    seen: dict[str, Any] = {}

    async def fake_request(role: str, mapping: Any, policy: Any, capability: str, path: str, payload: dict[str, Any], *a: Any, **k: Any) -> Any:
        seen.update(path=path, payload=payload)
        return {"embeddings": {"float": [[0.1, 0.2]]}}

    cohere = ModelMapping(model="cohere/embed-multilingual-light-v3.0", version="1", destination="remote")
    with patch.object(ModelGateway, "_request", side_effect=fake_request):
        out = await gw.embed("embedding", cohere, POLICY, ["hi"], probe=True)
    assert seen["path"] == "embeddings-native"
    assert seen["payload"] == {"texts": ["hi"], "input_type": "search_document", "embedding_types": ["float"]}
    assert out["data"] == [{"index": 0, "embedding": [0.1, 0.2]}]
