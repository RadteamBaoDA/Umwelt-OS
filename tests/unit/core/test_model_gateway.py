"""Unit tests for core.model_gateway modules.

Covers ModelGateway client, RequestPolicy evaluation, capability caching,
transport networking policies, capacity leasing via Redis, retry logic,
and error mapping.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import aclosing, asynccontextmanager
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
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

        with patch("core.model_gateway.client._LEASE_WAIT_SECONDS", 0.2),              patch("core.model_gateway.client._SLOTS", 8):
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


class _SlowModelServer:
    """Real HTTP/1.1 server on 127.0.0.1: reads the full Content-Length body, waits, then answers.

    ``statuses`` is consumed per request (default 200); ``bodies`` records each received body.
    """

    def __init__(self, delay: float, statuses: list[int] | None = None) -> None:
        self.delay = delay
        self.statuses = statuses or []
        self.bodies: list[bytes] = []
        self.request_lines: list[bytes] = []
        self.headers_sent_at: list[float] = []
        self.server: asyncio.Server | None = None
        self.release: asyncio.Event | None = None  # when set, headers wait for it instead of sleeping

    async def __aenter__(self) -> Self:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *_: object) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()

    @property
    def base_url(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:  # keep-alive: attempt 2 may reuse the connection
                try:
                    head = await reader.readuntil(b"\r\n\r\n")
                except (asyncio.IncompleteReadError, ConnectionError):
                    return
                lines = head.split(b"\r\n")
                self.request_lines.append(lines[0])
                length = next(int(x.split(b":")[1]) for x in lines if x.lower().startswith(b"content-length:"))
                body = await reader.readexactly(length)
                self.bodies.append(body)
                if self.release is not None:
                    try:
                        await asyncio.wait_for(self.release.wait(), 5)
                    except TimeoutError:
                        pass  # hook never fired before headers: the test's assertions fail, no hang
                    self.release.clear()
                else:
                    await asyncio.sleep(self.delay)
                status = self.statuses.pop(0) if self.statuses else 200
                payload = self._payload(lines[0], json.loads(body), status)
                ctype = b"text/event-stream" if payload.startswith(b"data:") else b"application/json"
                self.headers_sent_at.append(time.monotonic())
                writer.write(b"HTTP/1.1 %d X\r\nContent-Type: %s\r\nContent-Length: %d\r\n\r\n%s"
                             % (status, ctype, len(payload), payload))
                await writer.drain()
        finally:
            writer.close()

    @staticmethod
    def _payload(request_line: bytes, body: dict[str, object], status: int) -> bytes:
        if status != 200:
            return b'{"error": {"message": "busy"}}'
        if b"/rerank" in request_line:
            return b'{"results": [{"index": 0, "relevance_score": 1.0}]}'
        if body.get("stream"):
            chunk = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m",
                     "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}]}
            return b"data: " + json.dumps(chunk).encode() + b"\n\ndata: [DONE]\n\n"
        return json.dumps({"id": "c", "object": "chat.completion", "created": 0, "model": "m",
                           "choices": [{"index": 0, "finish_reason": "stop",
                                        "message": {"role": "assistant", "content": "hi"}}]}).encode()


def _loopback_gateway(base_url: str, timeout: float = 5.0) -> ModelGateway:
    """Real gateway through approved_http_client; transport.py has no built-in loopback deny, so 127.0.0.0/8 is approved."""
    redis = _LeaseRedis()
    redis.get = AsyncMock(return_value=json.dumps({  # type: ignore[attr-defined]
        "result": "supported", "gateway_identity": "legacy", "model": "m", "version": "1"}))
    return ModelGateway(redis=redis, base_url=base_url, api_key="k", destination_id="d",  # type: ignore[arg-type]
                        timeout_seconds=timeout, approved_endpoint_cidrs=("127.0.0.0/8",))


_POLICY = RequestPolicy(reasoning_allowed=True, embeddings_allowed=True, permitted_destinations=frozenset({"d"}))
_MAPPING = ModelMapping(model="m", version="1", destination="remote")
_BIG = "x" * 200_000  # several socket writes' worth of body


async def _call(gw: ModelGateway, op: str, after_send: Any) -> object:
    if op == "chat":
        return await gw.chat("fast", _MAPPING, _POLICY, [{"role": "user", "content": _BIG}], after_send=after_send)
    if op == "rerank":
        return await gw.rerank("reranker", _MAPPING, _POLICY, "q", [_BIG], after_send=after_send)
    return [x async for x in gw.stream("fast", _MAPPING, _POLICY, [{"role": "user", "content": _BIG}],
                                       after_send=after_send)]


@pytest.fixture
def written(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count bytes httpcore hands to its network stream (the AnyIO ``transport.write`` boundary)."""
    from httpcore._backends.anyio import AnyIOStream

    total = [0]
    original = AnyIOStream.write

    async def write(self: AnyIOStream, buffer: bytes, timeout: float | None = None) -> None:
        await original(self, buffer, timeout)
        total[0] += len(buffer)

    monkeypatch.setattr(AnyIOStream, "write", write)
    return total


def _fake_openai_with(create: Any) -> Any:
    client = MagicMock()
    client.chat.completions.create = create
    client.embeddings.create = create

    @asynccontextmanager
    async def fake_openai(*_a: object, **_k: object):  # type: ignore[no-untyped-def]
        yield client

    return fake_openai


class TestBodySentHook:
    """P14-T1: after_send fires when the request body is handed to transport.write (real httpcore)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("op", ["chat", "rerank", "stream"])
    async def test_after_send_fires_after_body_written_before_headers(self, op: str, written: list[int]) -> None:
        # rerank covers design T6: _apply_configured_reranking's evidence-lock release rides this hook.
        # stream covers D4/T7: the chat send fence is released before the response headers.
        calls: list[tuple[float, int, int]] = []
        async with _SlowModelServer(delay=0) as server:
            release = server.release = asyncio.Event()

            async def after_send() -> None:
                calls.append((time.monotonic(), written[0], len(server.headers_sent_at)))
                release.set()

            result = await _call(_loopback_gateway(server.base_url), op, after_send)
        assert result
        assert len(server.bodies) == 1 and len(server.headers_sent_at) == 1
        hook_at, written_then, headers_then = calls[0]
        assert headers_then == 0 and hook_at <= server.headers_sent_at[0]  # <=: coarse Windows clock; headers_then==0 is the real ordering proof
        # Every request byte (head + full body) had been handed to transport.write when the hook ran.
        assert written_then == written[0] and written_then > len(server.bodies[0]) > len(_BIG)
        assert len(calls) == 2  # hook once + the idempotent finally fallback
        assert server.request_lines[0].endswith(b"HTTP/1.1")

    @pytest.mark.asyncio
    async def test_real_rerank_response_parses(self) -> None:
        # Regression: cast_to=dict made openai 2.x raise ValueError on every real /rerank reply.
        async with _SlowModelServer(delay=0.0) as server:
            result = await _call(_loopback_gateway(server.base_url), "rerank", None)
        assert result == {"results": [{"index": 0, "relevance_score": 1.0}]}

    @pytest.mark.asyncio
    async def test_retry_fires_hook_per_attempt_and_refences(self) -> None:
        events: list[str] = []
        async with _SlowModelServer(delay=0, statuses=[503]) as server:
            release = server.release = asyncio.Event()

            async def before() -> None:
                events.append("before")

            async def after_send() -> None:
                events.append(f"after:{len(server.headers_sent_at)}")
                if len(server.headers_sent_at) < events.count("before"):  # this request's headers still pending
                    release.set()

            gw = _loopback_gateway(server.base_url)
            gw.before_send = before
            await _call(gw, "chat", after_send)
        assert len(server.bodies) == 2
        # attempt 1: before, hook (pre-headers), finally; attempt 2: before (re-fence), hook, finally
        assert events == ["before", "after:0", "after:1", "before", "after:1", "after:2"]

    @pytest.mark.asyncio
    async def test_callback_exception_never_retries_or_resends(self, caplog: pytest.LogCaptureFixture) -> None:
        calls = 0

        async def after_send() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("secret prompt text")

        async with _SlowModelServer(delay=0.1) as server:
            with caplog.at_level("WARNING", logger="core.model_gateway.transport"):
                result = await _call(_loopback_gateway(server.base_url), "chat", after_send)
        assert result and len(server.bodies) == 1  # no APIConnectionError retry, no second egress
        assert calls == 2  # the finally fallback still ran
        assert "RuntimeError" in caplog.text and "secret prompt text" not in caplog.text

    @pytest.mark.asyncio
    async def test_cancelled_error_from_callback_propagates(self) -> None:
        from core.model_gateway.transport import _NotifyOnEnd

        async def cancel() -> None:
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            [c async for c in _NotifyOnEnd(httpx.ByteStream(b"x"), cancel)]

    def test_approved_client_is_http1_only(self) -> None:
        # Pinned assumption: _NotifyOnEnd's ordering relies on httpcore's HTTP/1.1 write-then-pull body
        # loop. HTTP/2 frames bodies differently, so enabling it must fail here first.
        from core.model_gateway.transport import approved_http_client

        client = approved_http_client("https://example.com", ("0.0.0.0/0",))
        pool = client._transport._delegate._pool  # type: ignore[attr-defined]
        assert pool._http1 is True and pool._http2 is False

    @pytest.mark.asyncio
    async def test_ungated_transport_passes_stream_unwrapped(self) -> None:
        from core.model_gateway.transport import ApprovedEndpointTransport, _NotifyOnEnd, body_sent

        seen: list[httpx.Request] = []

        class Delegate(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                seen.append(request)
                return httpx.Response(200)

        transport = ApprovedEndpointTransport(httpx.URL("http://127.0.0.1:9"), ("127.0.0.0/8",), Delegate())
        request = httpx.Request("POST", "http://127.0.0.1:9/v1/x", content=b"{}")
        assert body_sent.get() is None
        await transport.handle_async_request(request)
        assert seen[0].stream is request.stream  # P2-3: byte-identical without after_send

        token = body_sent.set(AsyncMock())
        try:
            await transport.handle_async_request(request)
        finally:
            body_sent.reset(token)
        assert isinstance(seen[1].stream, _NotifyOnEnd)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("op", ["chat", "embed", "structured", "tools", "stream"])
    async def test_contextvar_never_set_without_after_send(self, op: str) -> None:
        from core.model_gateway import client as client_module
        from core.model_gateway.transport import body_sent

        seen: list[object] = []

        async def create(**_: object) -> object:
            seen.append(body_sent.get())
            if op == "stream":
                async def empty():  # type: ignore[no-untyped-def]
                    return
                    yield
                return empty()
            return {"ok": True}

        spy = MagicMock(wraps=body_sent)
        gw = _loopback_gateway("https://x")
        with patch("core.model_gateway.client.AsyncOpenAI", side_effect=_fake_openai_with(create)), \
             patch.object(gw, "_http_client", return_value=MagicMock()), \
             patch.object(client_module, "body_sent", spy):
            if op == "chat":
                await gw.chat("fast", _MAPPING, _POLICY, [])
            elif op == "embed":
                await gw.embed("fast", _MAPPING, _POLICY, ["x"])
            elif op == "structured":
                await gw.structured("fast", _MAPPING, _POLICY, [], {})
            elif op == "tools":
                await gw.tools("fast", _MAPPING, _POLICY, [], [])
            else:
                [x async for x in gw.stream("fast", _MAPPING, _POLICY, [])]
        assert seen == [None] and spy.set.call_count == 0 and spy.reset.call_count == 0

    @pytest.mark.asyncio
    async def test_hook_scoped_to_create_not_before_send(self) -> None:
        # A nested gateway call made inside before_send must not see (and fire) the outer hook.
        from core.model_gateway.transport import body_sent

        after = AsyncMock()
        during: dict[str, object] = {}

        async def before() -> None:
            during["before"] = body_sent.get()

        async def create(**_: object) -> object:
            during["create"] = body_sent.get()
            return {"ok": True}

        gw = _loopback_gateway("https://x")
        with patch("core.model_gateway.client.AsyncOpenAI", side_effect=_fake_openai_with(create)), \
             patch.object(gw, "_http_client", return_value=MagicMock()):
            await gw.chat("fast", _MAPPING, _POLICY, [], before_send=before, after_send=after)
        assert during == {"before": None, "create": after}
        assert body_sent.get() is None


class TestG1GatewayTimeouts:
    """P2-1/P2-2: configurable slots, long stream lease wait, short connect and DNS bounds."""

    def test_slots_default_is_24(self) -> None:
        from core.config import Settings
        assert Settings().model_gateway_slots == 24

    @pytest.mark.asyncio
    async def test_stream_uses_its_own_lease_wait_not_the_short_one(self) -> None:
        gw = _lease_gateway(_LeaseRedis())
        with patch("core.model_gateway.client._SLOTS", 1),              patch("core.model_gateway.client._LEASE_WAIT_SECONDS", 30.0),              patch("core.model_gateway.client._STREAM_LEASE_WAIT_SECONDS", 0.2):
            async with gw._slot():
                t0 = time.monotonic()
                with pytest.raises(ModelGatewayError, match="capacity"):
                    await gw.stream("fast", _MAPPING, _POLICY, [], probe=True).__anext__()
                assert time.monotonic() - t0 < 5  # stream wait (0.2 s), not the 30 s call wait

    @pytest.mark.asyncio
    async def test_short_calls_keep_short_wait_while_stream_wait_is_long(self) -> None:
        gw = _lease_gateway(_LeaseRedis())
        with patch("core.model_gateway.client._SLOTS", 1),              patch("core.model_gateway.client._LEASE_WAIT_SECONDS", 0.1),              patch("core.model_gateway.client._STREAM_LEASE_WAIT_SECONDS", 30.0):
            async with gw._slot():
                with pytest.raises(ModelGatewayError, match="capacity"):
                    await gw._with_slot(lambda: asyncio.sleep(0))

    def test_connect_timeout_is_bounded_to_five_seconds(self) -> None:
        t = _lease_gateway(_LeaseRedis(), timeout=180)._timeout()
        assert t.connect == 5.0 and t.read == 180
        assert _lease_gateway(_LeaseRedis(), timeout=2)._timeout().connect == 2

    @pytest.mark.asyncio
    async def test_sdk_receives_bounded_connect_timeout(self) -> None:
        seen: dict[str, Any] = {}

        @asynccontextmanager
        async def fake_openai(*_a: object, **k: Any):  # type: ignore[no-untyped-def]
            seen.update(k)
            raise ModelGatewayError("stop")
            yield

        gw = _lease_gateway(_LeaseRedis(), timeout=180)
        with patch("core.model_gateway.client.AsyncOpenAI", side_effect=fake_openai),              patch.object(gw, "_http_client", return_value=MagicMock()), pytest.raises(ModelGatewayError):
            await gw.stream("fast", _MAPPING, _POLICY, [], probe=True).__anext__()
        assert seen["timeout"].connect == 5.0

    @pytest.mark.asyncio
    async def test_dns_resolution_timeout_maps_to_policy_error(self) -> None:
        from core.model_gateway import transport as tr

        class Delegate(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                raise AssertionError

        t = tr.ApprovedEndpointTransport(httpx.URL("http://gw.example:80"), ("10.0.0.0/8",), Delegate())

        async def hang(*_a: object, **_k: object) -> None:
            await asyncio.sleep(30)

        loop = asyncio.get_running_loop()
        t0 = time.monotonic()
        with patch.object(tr, "DNS_TIMEOUT_SECONDS", 0.1), patch.object(loop, "getaddrinfo", hang),              pytest.raises(tr.EndpointNetworkPolicyError, match="resolution"):
            await t.handle_async_request(httpx.Request("GET", "http://gw.example:80/"))
        assert time.monotonic() - t0 < 2
