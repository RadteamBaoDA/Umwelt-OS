"""Unit tests for core.model_gateway modules.

Covers ModelGateway client, RequestPolicy evaluation, capability caching,
transport networking policies, capacity leasing via Redis, retry logic,
and error mapping.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import aclosing, asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openai import APIStatusError, APITimeoutError
from redis.exceptions import ConnectionError as RedisConnectionError
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
        redis.eval = AsyncMock(return_value=0)

        gateway = ModelGateway(
            redis=redis,
            base_url="https://api.openai.com/v1",
            api_key="sk-test",
            destination_id="openai",
        )

        async with gateway._slot():
            pass

        assert redis.eval.await_count == 2  # acquire + release

    @pytest.mark.asyncio
    async def test_slot_acquisition_failure_raises_model_gateway_error(self) -> None:
        """Verify _slot raises ModelGatewayError when Redis fails."""
        redis = AsyncMock()
        redis.eval.side_effect = RedisError("Redis unavailable")

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
        redis.eval = AsyncMock(return_value=0)
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


class _LeaseRedis:
    """In-memory stand-in for the lease subset of Redis (set NX, token-matched release/refresh)."""
    def __init__(self) -> None:
        self.keys: dict[str, str] = {}
        self.refreshes = 0
        self.acquires = 0
        self.eval_errors: list[Exception] = []

    async def eval(self, script: str, n: int, *args: object) -> int:
        await asyncio.sleep(0)  # real interleaving between callers
        if self.eval_errors:
            raise self.eval_errors.pop(0)
        keys, token = [str(k) for k in args[:n]], str(args[n])
        if "'NX'" in script:
            self.acquires += 1
            for i, k in enumerate(keys):
                if k not in self.keys:
                    self.keys[k] = token
                    return i
            return -1
        if "pexpire" in script:
            if self.keys.get(keys[0]) != token:
                return 0
            self.refreshes += 1
            return 1
        removed = 0
        for k in keys:
            if self.keys.get(k) == token:
                del self.keys[k]
                removed += 1
        return removed

def _lease_gateway(redis: _LeaseRedis, timeout: float = 5.0) -> ModelGateway:
    return ModelGateway(redis=redis, base_url="https://x/v1", api_key="k",  # type: ignore[arg-type]
                        destination_id="d", timeout_seconds=timeout)


class TestModelGatewayCapacityAndTimeouts:
    """P14-T2: lease wait is bounded alone; calls keep their own deadlines."""

    @pytest.mark.asyncio
    async def test_slots_then_extra_caller_times_out_on_acquisition(self) -> None:

        redis = _LeaseRedis()
        gw = _lease_gateway(redis)
        with patch("core.model_gateway.client._SLOTS", 2), \
             patch("core.model_gateway.client._LEASE_WAIT_SECONDS", 0.2):
            async with gw._slot(), gw._slot():
                assert len(redis.keys) == 2
                with pytest.raises(ModelGatewayError, match="Model capacity is unavailable"):
                    async with gw._slot():
                        raise AssertionError("must not acquire")
                assert len(redis.keys) == 2
            assert redis.keys == {}

    @pytest.mark.asyncio
    async def test_waiter_proceeds_when_slot_frees(self) -> None:
        redis = _LeaseRedis()
        gw = _lease_gateway(redis)
        with patch("core.model_gateway.client._SLOTS", 1),              patch("core.model_gateway.client._LEASE_WAIT_SECONDS", 2.0):
            async with gw._slot():
                waiter = asyncio.create_task(gw._with_slot(lambda: asyncio.sleep(0, "ok")))
                await asyncio.sleep(0.15)
                assert not waiter.done()
            assert await waiter == "ok"

    @pytest.mark.asyncio
    async def test_slot_body_not_bounded_by_lease_wait(self) -> None:
        redis = _LeaseRedis()
        gw = _lease_gateway(redis)
        with patch("core.model_gateway.client._LEASE_WAIT_SECONDS", 0.1):
            async with gw._slot():
                await asyncio.sleep(0.3)  # longer than lease wait and timeout_seconds
        assert redis.keys == {}

    @pytest.mark.asyncio
    async def test_non_stream_total_deadline_enforced_and_lease_released(self) -> None:
        redis = _LeaseRedis()
        gw = _lease_gateway(redis, timeout=0.1)
        with pytest.raises(ModelGatewayError, match="timed out"):
            await gw._with_slot(lambda: asyncio.sleep(5))
        assert redis.keys == {}

    @pytest.mark.asyncio
    async def test_lease_released_on_error_and_cancellation(self) -> None:
        redis = _LeaseRedis()
        gw = _lease_gateway(redis)
        with pytest.raises(RuntimeError):
            async with gw._slot():
                raise RuntimeError("boom")
        assert redis.keys == {}
        started = asyncio.Event()

        async def hold() -> None:
            async with gw._slot():
                started.set()
                await asyncio.sleep(10)

        task = asyncio.create_task(hold())
        await started.wait()
        assert len(redis.keys) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert redis.keys == {}

    @pytest.mark.asyncio
    async def test_lease_ttl_refreshed_and_release_only_own_token(self) -> None:
        redis = _LeaseRedis()
        gw = _lease_gateway(redis)
        with patch("core.model_gateway.client._LEASE_REFRESH_SECONDS", 0.05):
            async with gw._slot():
                await asyncio.sleep(0.25)
                key = next(iter(redis.keys))
                redis.keys[key] = "someone-else"  # lease expired and was re-taken
        assert redis.refreshes >= 2
        assert redis.keys == {key: "someone-else"}

    @pytest.mark.asyncio
    async def test_long_stream_not_cut_off_and_fences_run_per_attempt(self) -> None:
        redis = _LeaseRedis()
        redis.get = AsyncMock(return_value=json.dumps({  # type: ignore[attr-defined]
            "result": "supported", "gateway_identity": "legacy", "model": "m", "version": "1"}))
        before = AsyncMock()
        after = AsyncMock()
        gw = _lease_gateway(redis, timeout=0.1)
        gw.before_send = before
        policy = RequestPolicy(reasoning_allowed=True, permitted_destinations=frozenset({"d"}))
        mapping = ModelMapping(model="m", version="1", destination="remote")

        class Chunk:
            def model_dump(self, **_: object) -> dict[str, int]:
                return {"n": 1}

        async def chunks():  # type: ignore[no-untyped-def]
            for _ in range(5):
                await asyncio.sleep(0.05)  # each read within timeout; total 0.25 s > 0.1 s
                yield Chunk()

        calls = 0

        async def create(**_: object):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            if calls == 1:
                raise APITimeoutError(request=MagicMock())
            return chunks()

        client = MagicMock()
        client.chat.completions.create = create

        @asynccontextmanager
        async def fake_openai(*_a: object, **_k: object):  # type: ignore[no-untyped-def]
            yield client

        with patch("core.model_gateway.client.AsyncOpenAI", side_effect=fake_openai), \
             patch.object(gw, "_http_client", return_value=MagicMock()):
            lines = [x async for x in gw.stream("fast", mapping, policy, [], after_send=after)]
        assert len(lines) == 6 and lines[-1] == "data: [DONE]"
        assert before.await_count == 2 and after.await_count == 2  # per attempt
        assert redis.keys == {}

    @pytest.mark.asyncio
    async def test_nine_concurrent_callers_peak_eight_one_unavailable(self) -> None:
        redis = _LeaseRedis()
        gw = _lease_gateway(redis)
        live = peak = 0

        async def work() -> str:
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.3)
            live -= 1
            return "ok"

        with patch("core.model_gateway.client._LEASE_WAIT_SECONDS", 0.2):
            results = await asyncio.gather(*(gw._with_slot(work) for _ in range(9)), return_exceptions=True)
        errors = [r for r in results if isinstance(r, ModelGatewayError)]
        assert peak == 8 and results.count("ok") == 8
        assert len(errors) == 1 and "Model capacity is unavailable" in str(errors[0])
        assert redis.keys == {}

    @pytest.mark.asyncio
    async def test_acquire_is_one_round_trip_per_poll(self) -> None:
        redis = _LeaseRedis()
        async with _lease_gateway(redis)._slot():
            assert redis.acquires == 1

    @pytest.mark.asyncio
    async def test_transient_redis_connection_error_retried_until_deadline(self) -> None:
        redis = _LeaseRedis()
        redis.eval_errors = [RedisConnectionError("Too many connections")] * 2
        gw = _lease_gateway(redis)
        async with gw._slot():
            assert len(redis.keys) == 1
        assert redis.keys == {}
        redis.eval_errors = [RedisConnectionError("down")] * 1000
        with patch("core.model_gateway.client._LEASE_WAIT_SECONDS", 0.3), \
             pytest.raises(ModelGatewayError, match="Model capacity is unavailable"):
            async with gw._slot():
                raise AssertionError("must not acquire")

    @pytest.mark.asyncio
    async def test_cancel_during_acquire_sweeps_possibly_applied_lease(self) -> None:
        redis = _LeaseRedis()
        gw = _lease_gateway(redis)
        real = redis.eval

        async def applied_then_hang(script: str, n: int, *args: object) -> int:
            if "'NX'" in script:
                await real(script, n, *args)  # server applied it; the reply never arrives
                await asyncio.sleep(10)
            return await real(script, n, *args)

        redis.eval = applied_then_hang  # type: ignore[method-assign]
        with patch("core.model_gateway.client._LEASE_WAIT_SECONDS", 0.1), \
             pytest.raises(ModelGatewayError, match="Model capacity is unavailable"):
            async with gw._slot():
                raise AssertionError("must not acquire")
        assert redis.keys == {}

    @pytest.mark.asyncio
    async def test_redis_error_during_call_maps_to_gateway_error(self) -> None:
        gw = _lease_gateway(_LeaseRedis())

        async def boom() -> None:
            raise RedisError("x")

        with pytest.raises(ModelGatewayError, match="request failed"):
            await gw._with_slot(boom)

    @staticmethod
    def _stream_fixture(create):  # type: ignore[no-untyped-def]
        redis = _LeaseRedis()
        redis.get = AsyncMock(return_value=json.dumps({  # type: ignore[attr-defined]
            "result": "supported", "gateway_identity": "legacy", "model": "m", "version": "1"}))
        gw = _lease_gateway(redis, timeout=0.1)
        policy = RequestPolicy(reasoning_allowed=True, permitted_destinations=frozenset({"d"}))
        mapping = ModelMapping(model="m", version="1", destination="remote")
        client = MagicMock()
        client.chat.completions.create = create

        @asynccontextmanager
        async def fake_openai(*_a: object, **_k: object):  # type: ignore[no-untyped-def]
            yield client

        return redis, gw, policy, mapping, fake_openai

    @pytest.mark.asyncio
    async def test_slow_stream_open_times_out_and_after_send_runs(self) -> None:
        async def create(**_: object):  # type: ignore[no-untyped-def]
            await asyncio.sleep(5)

        redis, gw, policy, mapping, fake_openai = self._stream_fixture(create)
        after = AsyncMock()
        with patch("core.model_gateway.client.AsyncOpenAI", side_effect=fake_openai), \
             patch.object(gw, "_http_client", return_value=MagicMock()), \
             pytest.raises(ModelGatewayError, match="stream failed"):
            [x async for x in gw.stream("fast", mapping, policy, [], after_send=after)]
        assert after.await_count == 2  # both attempts released the fence
        assert redis.keys == {}

    @pytest.mark.asyncio
    async def test_consumer_stopping_stream_early_releases_slot(self) -> None:
        class Chunk:
            def model_dump(self, **_: object) -> dict[str, int]:
                return {"n": 1}

        async def chunks():  # type: ignore[no-untyped-def]
            for _ in range(5):
                yield Chunk()

        async def create(**_: object):  # type: ignore[no-untyped-def]
            return chunks()

        redis, gw, policy, mapping, fake_openai = self._stream_fixture(create)
        with patch("core.model_gateway.client.AsyncOpenAI", side_effect=fake_openai), \
             patch.object(gw, "_http_client", return_value=MagicMock()):
            async with aclosing(gw.stream("fast", mapping, policy, [])) as gen:
                async for _line in gen:
                    assert len(redis.keys) == 1
                    break
        assert redis.keys == {}
