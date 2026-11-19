"""openai 3 (httpx2-based SDK) driven by the gateway's legacy ``httpx`` pinned client still maps transport faults.

The gateway keeps an ``httpx.AsyncClient`` (pinned SSRF transport) and hands it to ``AsyncOpenAI``. These tests
use the real SDK and a raising ``httpx.MockTransport`` to prove a legacy ``httpx`` timeout/connect error surfaces
as ``openai.APITimeoutError``/``APIConnectionError`` and then as the gateway's static-reason error, for both the
OpenAI-compatible SDK path and the native Anthropic path.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from openai import APIConnectionError, APITimeoutError

from core.model_gateway.client import ModelGateway, ModelGatewayError
from tests.unit.core.test_anthropic_transport import USER, _fences  # noqa: F401  # autouse fixture
from tests.unit.core.test_model_gateway import _MAPPING, _POLICY, _gateway

KINDS = [("openai", "https://api.openai.com/v1"), ("anthropic", "https://api.anthropic.com/v1")]
FAULTS = [
    (httpx.ConnectTimeout("slow"), APITimeoutError, "upstream timed out"),
    (httpx.ConnectError("refused"), APIConnectionError, r"could not connect to upstream"),
]


def _raising_gateway(kind: str, base_url: str, fault: Exception) -> tuple[ModelGateway, Any]:
    """Gateway of ``kind`` whose pinned http client is a legacy ``httpx`` client raising ``fault`` on send."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise fault

    redis = MagicMock()
    redis.exists = AsyncMock(return_value=0)
    redis.set = AsyncMock()
    gw = _gateway(redis, base_url=base_url, kind=kind, destination_id="d")
    patcher = patch.object(
        ModelGateway,
        "_http_client",
        side_effect=lambda _url: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return gw, patcher


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "base_url"), KINDS)
@pytest.mark.parametrize(("fault", "sdk_error", "reason"), FAULTS)
async def test_discover_maps_legacy_httpx_faults(
    kind: str, base_url: str, fault: Exception, sdk_error: type[Exception], reason: str
) -> None:
    gw, patcher = _raising_gateway(kind, base_url, fault)
    with patcher, pytest.raises(ModelGatewayError, match=reason) as exc:
        await gw.discover_models()
    assert isinstance(exc.value.__cause__, sdk_error)


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "base_url"), KINDS)
@pytest.mark.parametrize(("fault", "sdk_error", "_reason"), FAULTS)
async def test_chat_retries_then_raises_gateway_error_for_legacy_httpx_faults(
    kind: str, base_url: str, fault: Exception, sdk_error: type[Exception], _reason: str
) -> None:
    gw, patcher = _raising_gateway(kind, base_url, fault)
    with patcher, pytest.raises(ModelGatewayError) as exc:
        await gw.chat("chat", _MAPPING, _POLICY, USER, before_send=AsyncMock())
    assert isinstance(exc.value.__cause__, sdk_error)
