"""ModelGateway.stream forwards tool-calling parameters only when provided and capability-verified."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core.model_gateway.cache import capability_key
from core.model_gateway.client import CapabilityUnsupported, ModelGateway
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


def _evidence(capability: str) -> str:
    """Supported capability evidence bound to the test principal and configuration."""
    now = datetime.now(UTC)
    return json.dumps({
        "workspace_id": str(WORKSPACE_ID), "actor_user_id": 7, "membership_revision": 3, "alias": "fast",
        "model": "m", "version": "1", "gateway_identity": IDENTITY, "configuration_revision": 1,
        "capability": capability, "result": "supported", "checked_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
    })


def _gateway(capabilities: tuple[str, ...]) -> ModelGateway:
    """Gateway whose Redis cache verifies only the given capabilities."""
    table = {
        capability_key("fast", "m", "1", c, IDENTITY, workspace_id=WORKSPACE_ID, actor_user_id=7): _evidence(c)
        for c in capabilities
    }
    redis = MagicMock()
    redis.get = AsyncMock(side_effect=lambda key: table.get(key))
    return ModelGateway(
        redis=redis, base_url="https://x/v1", api_key="k", destination_id="d", scope=SCOPE,
        gateway_identity=IDENTITY, configuration_revision=1, before_send=AsyncMock(),
    )


async def _run(gw: ModelGateway, **kwargs: Any) -> dict[str, Any]:
    """Drain one stream against a fake OpenAI client and return the kwargs given to create()."""
    seen: dict[str, Any] = {}

    async def create(**kw: Any) -> Any:
        seen.update(kw)

        async def chunks() -> Any:
            return
            yield  # pragma: no cover - empty async generator

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
        _ = [x async for x in gw.stream("fast", MAPPING, POLICY, [], **kwargs)]
    return seen


@asynccontextmanager
async def _null_ctx() -> Any:
    """Stand-in for the Redis capacity lease."""
    yield


@pytest.mark.asyncio
async def test_tools_forwarded_when_given() -> None:
    """tools, tool_choice and max_tokens reach chat.completions.create."""
    seen = await _run(_gateway(("streaming", "tools")), tools=TOOLS, tool_choice="auto", max_tokens=256)
    assert seen["tools"] == TOOLS and seen["tool_choice"] == "auto" and seen["max_tokens"] == 256


@pytest.mark.asyncio
async def test_tools_absent_not_forwarded() -> None:
    """Without tools the request body keeps its previous shape."""
    seen = await _run(_gateway(("streaming",)))
    assert not {"tools", "tool_choice", "max_tokens"} & seen.keys()


@pytest.mark.asyncio
async def test_missing_tools_capability_rejected() -> None:
    """tools without a verified 'tools' capability raises CapabilityUnsupported before I/O."""
    with pytest.raises(CapabilityUnsupported):
        await _run(_gateway(("streaming",)), tools=TOOLS)
