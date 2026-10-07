"""Unit tests for core.model_gateway modules.

Covers ModelGateway client, RequestPolicy evaluation, capability caching,
transport networking policies, capacity leasing via Redis, retry logic,
and error mapping.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openai import APIStatusError
from redis.exceptions import RedisError

from core.model_gateway.cache import (
    capability_alias_pattern,
    capability_key,
    capability_model_pattern,
)
from core.model_gateway.client import (
    CapabilityUnsupported,
    ModelGateway,
    ModelGatewayError,
    PrivacyPolicyDenied,
)
from core.model_gateway.policy import may_send
from core.model_gateway.schemas import (
    ModelMapping,
    RequestPolicy,
)
from core.model_gateway.transport import (
    EndpointNetworkPolicyError,
    _address,
    _networks,
)


class TestPolicyMaySend:
    """Unit tests for privacy and capability dispatch policy evaluation."""

    def test_may_send_rejects_missing_or_empty_model(self) -> None:
        """Verify may_send returns False when mapping is None or model string is empty."""
        policy = RequestPolicy(reasoning_allowed=True, permitted_destinations=frozenset({"dest1"}))
        assert may_send(policy, "fast", None, "dest1", True, "chat") is False
        empty_mapping = ModelMapping(model="", destination="remote")
        assert may_send(policy, "fast", empty_mapping, "dest1", True, "chat") is False

    def test_may_send_rejects_local_private_alias_or_local_only(self) -> None:
        """Verify may_send denies remote dispatch when alias is local-private or policy is local_only."""
        policy = RequestPolicy(reasoning_allowed=True, permitted_destinations=frozenset({"dest1"}), local_only=True)
        mapping = ModelMapping(model="gpt-4o", destination="remote")
        assert may_send(policy, "fast", mapping, "dest1", True, "chat") is False

        policy.local_only = False
        assert may_send(policy, "local-private", mapping, "dest1", True, "chat") is False

    def test_may_send_rejects_unconfigured_credentials(self) -> None:
        """Verify may_send returns False when credentials are not configured."""
        policy = RequestPolicy(reasoning_allowed=True, permitted_destinations=frozenset({"dest1"}))
        mapping = ModelMapping(model="gpt-4o", destination="remote")
        assert may_send(policy, "fast", mapping, "dest1", False, "chat") is False

    def test_may_send_rejects_unpermitted_destination(self) -> None:
        """Verify may_send returns False when destination_id is not permitted."""
        policy = RequestPolicy(reasoning_allowed=True, permitted_destinations=frozenset({"dest1"}))
        mapping = ModelMapping(model="gpt-4o", destination="remote")
        assert may_send(policy, "fast", mapping, "dest2", True, "chat") is False

    def test_may_send_reasoning_and_embeddings_capabilities(self) -> None:
        """Verify may_send checks reasoning_allowed vs embeddings_allowed flags."""
        policy = RequestPolicy(
            reasoning_allowed=True,
            embeddings_allowed=False,
            permitted_destinations=frozenset({"dest1"}),
        )
        mapping = ModelMapping(model="gpt-4o", destination="remote")
        assert may_send(policy, "fast", mapping, "dest1", True, "chat") is True
        assert may_send(policy, "fast", mapping, "dest1", True, "embeddings") is False


class TestCapabilityCacheKeys:
    """Unit tests for cache key generators in core.model_gateway.cache."""

    def test_capability_key_format(self) -> None:
        """Verify capability_key builds expected Redis key."""
        key = capability_key(
            alias="fast",
            model="gpt-4o-mini",
            version="2024-07-18",
            capability="chat",
            gateway_identity="gw-1",
        )
        assert key.startswith("bbd:model-gateway:capability:")
        assert "gw-1" in key

    def test_capability_patterns(self) -> None:
        """Verify pattern builders for alias and model cache invalidation."""
        alias_pat = capability_alias_pattern("fast")
        assert alias_pat.endswith(":*")
        model_pat = capability_model_pattern("fast", "gpt-4o", "v1")
        assert model_pat.endswith(":*")


class TestTransportNetworkPolicy:
    """Unit tests for endpoint network policy, IP address normalization, and CIDR checks."""

    def test_networks_parses_cidrs(self) -> None:
        """Verify _networks parses valid CIDRs."""
        nets = _networks(["10.0.0.0/8", "192.168.1.0/24"])
        assert len(nets) == 2

    def test_networks_fails_closed_on_empty(self) -> None:
        """Verify _networks raises EndpointNetworkPolicyError on empty sequence."""
        with pytest.raises(EndpointNetworkPolicyError, match="unavailable"):
            _networks([])

    def test_networks_fails_on_malformed(self) -> None:
        """Verify _networks raises EndpointNetworkPolicyError on malformed CIDR."""
        with pytest.raises(EndpointNetworkPolicyError, match="invalid"):
            _networks(["invalid-cidr"])

    def test_address_parses_and_normalizes_ipv4(self) -> None:
        """Verify _address parses IPv4 address."""
        addr = _address("192.168.1.5")
        assert str(addr) == "192.168.1.5"

    def test_address_normalizes_ipv4_mapped_ipv6(self) -> None:
        """Verify _address unwraps IPv4-mapped IPv6 address."""
        addr = _address("::ffff:192.168.1.1")
        assert str(addr) == "192.168.1.1"


class TestModelGatewaySlotLeasing:
    """Unit tests for concurrency slot lease acquisition and release in Redis."""

    @pytest.mark.asyncio
    async def test_slot_acquisition_success_and_release(self) -> None:
        """Verify _slot acquires candidate key and releases it on exit."""
        redis = AsyncMock()
        redis.set.return_value = True
        redis.eval = AsyncMock()

        gateway = ModelGateway(
            redis=redis,
            base_url="https://api.openai.com/v1",
            api_key="sk-test",
            destination_id="openai",
        )

        async with gateway._slot():
            pass

        redis.set.assert_awaited()
        redis.eval.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_slot_acquisition_failure_raises_model_gateway_error(self) -> None:
        """Verify _slot raises ModelGatewayError when Redis fails."""
        redis = AsyncMock()
        redis.set.side_effect = RedisError("Redis unavailable")

        gateway = ModelGateway(
            redis=redis,
            base_url="https://api.openai.com/v1",
            api_key="sk-test",
            destination_id="openai",
            timeout_seconds=0.1,
        )

        with pytest.raises(ModelGatewayError, match="Model capacity is unavailable"):
            async with gateway._slot():
                pass


class TestModelGatewayRequestExecution:
    """Unit tests for ModelGateway request execution, error mapping, and retries."""

    @pytest.fixture
    def mock_gateway(self) -> ModelGateway:
        """Return ModelGateway configured with mock redis."""
        redis = AsyncMock()
        redis.set.return_value = True
        redis.eval = AsyncMock()
        redis.get = AsyncMock(return_value=json.dumps({
            "result": "supported",
            "gateway_identity": "test-gw",
            "model": "gpt-4o",
            "version": "1",
        }))

        return ModelGateway(
            redis=redis,
            base_url="https://api.openai.com/v1",
            api_key="sk-test",
            destination_id="dest1",
            gateway_identity="test-gw",
            timeout_seconds=5.0,
        )

    @pytest.mark.asyncio
    async def test_chat_parameter_validation(self, mock_gateway) -> None:
        """Verify chat rejects invalid max_tokens or temperature."""
        policy = RequestPolicy(reasoning_allowed=True, permitted_destinations=frozenset({"dest1"}))
        mapping = ModelMapping(model="gpt-4o", version="1", destination="remote")

        with pytest.raises(ValueError, match="max_tokens"):
            await mock_gateway.chat("fast", mapping, policy, [], max_tokens=0)

        with pytest.raises(ValueError, match="temperature"):
            await mock_gateway.chat("fast", mapping, policy, [], temperature=2.5)

    @pytest.mark.asyncio
    async def test_request_denied_by_policy(self, mock_gateway) -> None:
        """Verify _request raises PrivacyPolicyDenied when policy forbids capability."""
        policy = RequestPolicy(reasoning_allowed=False, permitted_destinations=frozenset({"dest1"}))
        mapping = ModelMapping(model="gpt-4o", version="1", destination="remote")

        with pytest.raises(PrivacyPolicyDenied, match="denied by privacy policy"):
            await mock_gateway.chat("fast", mapping, policy, [{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_request_unverified_capability_raises(self, mock_gateway) -> None:
        """Verify _request raises ModelGatewayError when capability is not in cache."""
        mock_gateway.redis.get.return_value = None  # Not verified
        policy = RequestPolicy(reasoning_allowed=True, permitted_destinations=frozenset({"dest1"}))
        mapping = ModelMapping(model="gpt-4o", version="1", destination="remote")

        with pytest.raises(ModelGatewayError, match="capability is not supported"):
            await mock_gateway.chat("fast", mapping, policy, [{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_request_maps_unsupported_status_error(self, mock_gateway) -> None:
        """Verify 400/404/405/422 status errors map to CapabilityUnsupported."""
        policy = RequestPolicy(reasoning_allowed=True, permitted_destinations=frozenset({"dest1"}))
        mapping = ModelMapping(model="gpt-4o", version="1", destination="remote")

        status_error = APIStatusError(
            message="Not supported",
            response=MagicMock(status_code=404, headers={}),
            body=None,
        )

        mock_client = AsyncMock()
        mock_client.chat.completions.create.side_effect = status_error

        @asynccontextmanager
        async def mock_async_openai(*args, **kwargs):
            yield mock_client

        with patch("core.model_gateway.client.AsyncOpenAI", side_effect=mock_async_openai), \
             patch.object(mock_gateway, "_http_client", return_value=MagicMock()):  # noqa: SIM117  # style-only; nested with kept
            with pytest.raises(CapabilityUnsupported):
                await mock_gateway.chat("fast", mapping, policy, [{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_discover_models_returns_model_ids(self, mock_gateway) -> None:
        """Verify discover_models retrieves model IDs via client.models.list."""
        mock_client = AsyncMock()
        mock_page = MagicMock()
        mock_item1 = MagicMock(id="gpt-4o")
        mock_item2 = MagicMock(id="gpt-4o-mini")
        mock_page.data = [mock_item1, mock_item2]
        mock_client.models.list.return_value = mock_page

        @asynccontextmanager
        async def mock_async_openai(*args, **kwargs):
            yield mock_client

        with patch("core.model_gateway.client.AsyncOpenAI", side_effect=mock_async_openai), \
             patch.object(mock_gateway, "_http_client", return_value=MagicMock()):
            models = await mock_gateway.discover_models()

        assert models == ["gpt-4o", "gpt-4o-mini"]
