"""Per-attempt before_send fence: required on non-probe calls and re-run on every retry."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openai import APIConnectionError

from core.model_gateway.client import ModelGateway
from tests.unit.core.test_model_gateway import (
    _MAPPING,
    _POLICY,
    _fake_openai_with,
    _loopback_gateway,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["chat", "embed", "structured", "tools", "rerank"])
async def test_missing_before_send_raises(op: str) -> None:
    gw = _loopback_gateway("https://x")
    calls = {
        "chat": lambda: gw.chat("fast", _MAPPING, _POLICY, []),
        "embed": lambda: gw.embed("fast", _MAPPING, _POLICY, ["x"]),
        "structured": lambda: gw.structured("fast", _MAPPING, _POLICY, [], {}),
        "tools": lambda: gw.tools("fast", _MAPPING, _POLICY, [], []),
        "rerank": lambda: gw.rerank("reranker", _MAPPING, _POLICY, "q", ["d"]),
    }
    with pytest.raises(ValueError, match="before_send_required"):
        await calls[op]()


@pytest.mark.asyncio
async def test_retry_calls_before_send_twice() -> None:
    attempts = 0

    async def create(**_: object) -> object:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise APIConnectionError(request=MagicMock())
        return {"ok": True}

    before = AsyncMock()
    gw = _loopback_gateway("https://x")
    with patch("core.model_gateway.client.AsyncOpenAI", side_effect=_fake_openai_with(create)), \
         patch.object(ModelGateway, "_http_client", return_value=MagicMock()):
        assert await gw.chat("fast", _MAPPING, _POLICY, [], before_send=before) == {"ok": True}
    assert attempts == 2 and before.await_count == 2
