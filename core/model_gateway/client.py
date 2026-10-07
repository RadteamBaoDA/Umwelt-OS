import asyncio
import json
import logging
import random
import secrets
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import aclosing, asynccontextmanager
from typing import Any, cast

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

from core.model_gateway.cache import capability_key
from core.model_gateway.policy import may_send
from core.model_gateway.schemas import ModelMapping, RequestPolicy
from core.model_gateway.transport import EndpointNetworkPolicyError, approved_http_client, body_sent
from core.telemetry import record_model_call

_LEASE_PREFIX = "bbd:model-gateway:slot:"
_SLOTS = 8
_LEASE_WAIT_SECONDS = 10.0
_LEASE_TTL_SECONDS = 60
_LEASE_REFRESH_SECONDS = 20.0
_REFRESH = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('pexpire', KEYS[1], ARGV[2]) else return 0 end"
# One round trip per poll: take the first free slot (owner token, PX TTL) or return -1.
_ACQUIRE = "for i, k in ipairs(KEYS) do if redis.call('set', k, ARGV[1], 'NX', 'PX', ARGV[2]) then return i - 1 end end return -1"
# Token-checked; one key (normal release) or all slots (sweep after an acquire cut short mid-flight).
_RELEASE = "local n = 0 for _, k in ipairs(KEYS) do if redis.call('get', k) == ARGV[1] then n = n + redis.call('del', k) end end return n"


class ModelGatewayError(RuntimeError):
    """Base exception for unavailable, rejected, or invalid model gateway operations."""


class PrivacyPolicyDenied(ModelGatewayError):
    """Raised when request policy forbids sending a capability to the configured destination."""


class CapabilityUnsupported(ModelGatewayError):
    """Raised when the gateway rejects a requested model capability."""


class ModelGateway:
    """OpenAI-compatible client enforcing privacy policy, capability verification, bounded concurrency, and approved endpoint networking."""
    def __init__(
        self,
        redis: Redis,
        base_url: str | None,
        api_key: str,
        destination_id: str,
        timeout_seconds: float = 20.0,
        gateway_identity: str = "legacy",
        before_send: Callable[[], Awaitable[None]] | None = None,
        approved_endpoint_cidrs: tuple[str, ...] = (),
    ) -> None:
        # The SDK DEBUG request log contains JSON request bodies. Keep prompts
        # and indexed source text out of logs even when OPENAI_LOG=debug is set.
        """Initialize gateway credentials, identity, timeouts, pre-send hook, and approved destination networks."""
        logging.getLogger("openai").setLevel(logging.WARNING)
        self.redis = redis
        self.base_url = base_url.rstrip("/") if base_url else None
        self.api_key = api_key
        self.destination_id = destination_id
        self.timeout_seconds = timeout_seconds
        self.gateway_identity = gateway_identity
        self.before_send = before_send
        self.approved_endpoint_cidrs = approved_endpoint_cidrs

    def _http_client(self, base_url: str) -> httpx.AsyncClient:
        """Create a redirect-disabled HTTP client pinned to an endpoint approved by network policy."""
        try:
            return approved_http_client(base_url, self.approved_endpoint_cidrs)
        except EndpointNetworkPolicyError as exc:
            raise ModelGatewayError("Model gateway network policy is unavailable") from exc

    async def _keep_lease(self, key: str, token: str) -> None:
        """Extend the lease TTL while the call runs; only our token is refreshed."""
        while True:
            await asyncio.sleep(_LEASE_REFRESH_SECONDS)
            try:
                await cast("Awaitable[Any]", self.redis.eval(_REFRESH, 1, key, token, str(_LEASE_TTL_SECONDS * 1000)))
            except RedisError:
                pass

    @asynccontextmanager
    async def _slot(self) -> AsyncIterator[None]:
        """Acquire one of the Redis-backed gateway leases (bounded wait), hold it with a refreshed TTL, release only our token.

        The timeout bounds lease acquisition only; the body runs outside it.
        """
        token = secrets.token_urlsafe(18)
        keys = [f"{_LEASE_PREFIX}{slot}" for slot in range(_SLOTS)]
        key = None
        attempted = False
        refresher: asyncio.Task[None] | None = None
        try:
            try:
                async with asyncio.timeout(_LEASE_WAIT_SECONDS):
                    while key is None:
                        attempted = True
                        try:
                            slot = int(await cast("Awaitable[Any]", self.redis.eval(
                                _ACQUIRE, len(keys), *keys, token, str(_LEASE_TTL_SECONDS * 1000))))
                        except (RedisConnectionError, RedisTimeoutError):
                            slot = -1  # transient (e.g. pool exhausted): retry until the lease deadline
                        if slot >= 0:
                            key = keys[slot]
                        else:
                            await asyncio.sleep(0.1 + random.uniform(0, 0.05))
            except (RedisError, TimeoutError) as exc:
                raise ModelGatewayError("Model capacity is unavailable") from exc
            refresher = asyncio.create_task(self._keep_lease(key, token))
            yield
        finally:
            if refresher is not None:
                refresher.cancel()
            # An acquire cut short mid-flight may still have been applied server-side: sweep all slots by token.
            held = [key] if key is not None else keys if attempted else []
            if held:
                try:
                    await asyncio.shield(cast("Awaitable[Any]", self.redis.eval(_RELEASE, len(held), *held, token)))
                except RedisError:
                    pass

    async def _with_slot[T](self, call: Callable[[], Awaitable[T]]) -> T:
        """Run one non-stream gateway operation under a lease with a total deadline (lease wait excluded)."""
        async with self._slot():
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    return await call()
            except TimeoutError as exc:
                raise ModelGatewayError("Model gateway request timed out") from exc
            except RedisError as exc:
                raise ModelGatewayError("Model gateway request failed") from exc

    async def _request(
        self,
        alias: str,
        mapping: ModelMapping | None,
        policy: RequestPolicy,
        capability: str,
        path: str,
        payload: dict[str, Any],
        probe: bool = False,
        before_send: Callable[[], Awaitable[None]] | None = None,
        after_send: Callable[[], Awaitable[None]] | None = None,
    ) -> Any:
        """Check policy and cached capability before sending a bounded, retried gateway request; map transport and provider errors."""
        if not may_send(policy, alias, mapping, self.destination_id, bool(self.api_key), capability):
            raise PrivacyPolicyDenied("Model request denied by privacy policy")
        if self.base_url is None or mapping is None:
            raise ModelGatewayError("Model gateway is not configured")
        if not probe:
            key = capability_key(alias, mapping.model, mapping.version, capability, self.gateway_identity)
            stored = await self.redis.get(key)
            try:
                capability_result = json.loads(stored) if stored else {}
            except (TypeError, json.JSONDecodeError):
                capability_result = {}
            if (capability_result.get("result") != "supported"
                    or capability_result.get("gateway_identity") != self.gateway_identity
                    or capability_result.get("model") != mapping.model
                    or capability_result.get("version") != mapping.version):
                raise ModelGatewayError("Model capability is not supported")
        body = {**payload, "model": mapping.model}
        base_url = self.base_url if self.base_url.endswith("/v1") else f"{self.base_url}/v1"

        async def send() -> Any:
            """Execute a chat, embedding, or rerank SDK call with bounded retries and normalize its response."""
            async with AsyncOpenAI(
                base_url=base_url,
                api_key=self.api_key or "not-configured",
                timeout=self.timeout_seconds,
                max_retries=0,
                http_client=self._http_client(base_url),
            ) as client:
                for attempt in range(2):
                    try:
                        # Keep the gateway-owned settings check even when a scoped
                        # operation adds its own source/evidence freshness fence.
                        if self.before_send is not None:
                            await self.before_send()
                        if before_send is not None and before_send is not self.before_send:
                            await before_send()
                        # Gated (P2-3): with no after_send the ContextVar is never touched, so the
                        # transport sends request.stream unwrapped exactly as before.
                        hook = body_sent.set(after_send) if after_send is not None else None
                        try:
                            if path == "chat/completions":
                                response = await client.chat.completions.create(**body)
                            elif path == "embeddings":
                                response = await client.embeddings.create(**body)
                            elif path == "rerank":
                                response = await client.post("/rerank", cast_to=dict, body=body)
                            else:
                                raise ModelGatewayError("Unsupported model gateway operation")
                        finally:
                            if hook is not None:
                                body_sent.reset(hook)
                            # Fallback release (connect/write failure, timeout); after_send is idempotent.
                            if after_send is not None:
                                await after_send()
                    except (APITimeoutError, APIConnectionError) as exc:
                        if attempt == 0:
                            continue
                        raise ModelGatewayError("Model gateway request failed") from exc
                    except APIStatusError as exc:
                        if exc.status_code in {408, 425, 429} or exc.status_code >= 500:  # noqa: SIM102  # style-only rewrite skipped to avoid touching control flow
                            if attempt == 0:
                                await asyncio.sleep(0.1)
                                continue
                        if exc.status_code in {400, 404, 405, 422}:
                            raise CapabilityUnsupported("The configured gateway rejected this capability") from exc
                        raise ModelGatewayError(f"Model gateway returned HTTP {exc.status_code}") from exc
                    except EndpointNetworkPolicyError as exc:
                        raise ModelGatewayError("Model gateway network policy denied the destination") from exc
                    if hasattr(response, "model_dump"):
                        return response.model_dump(mode="json", exclude_none=True)
                    if isinstance(response, dict):
                        return response
                    raise ModelGatewayError("Model gateway returned an invalid response")
            raise ModelGatewayError("Model gateway request failed")

        # Telemetry only: labels are capability and configured alias; model name is identity, never a label.
        started = time.perf_counter()
        try:
            response = await self._with_slot(send)
        except BaseException:
            record_model_call(capability, alias, started, False)
            raise
        record_model_call(capability, alias, started, True, response, mapping.model)
        return response

    async def discover_models(self) -> list[str]:
        """List model IDs from the configured gateway while holding capacity and enforcing endpoint policy."""
        if self.base_url is None or not self.api_key:
            raise ModelGatewayError("Model gateway is not configured")
        base_url = self.base_url if self.base_url.endswith("/v1") else f"{self.base_url}/v1"

        async def send() -> list[str]:
            """Call the configured model-list endpoint and return valid model IDs; map SDK and network failures to the gateway error."""
            if self.before_send is not None:
                await self.before_send()
            async with AsyncOpenAI(base_url=base_url, api_key=self.api_key,
                                   timeout=self.timeout_seconds, max_retries=0,
                                   http_client=self._http_client(base_url)) as client:
                try:
                    page = await client.models.list()
                except (APIConnectionError, APITimeoutError, APIStatusError) as exc:
                    raise ModelGatewayError("Model gateway discovery failed") from exc
                except EndpointNetworkPolicyError as exc:
                    raise ModelGatewayError("Model gateway network policy denied the destination") from exc
                model_ids: list[str] = [item.id for item in page.data if isinstance(item.id, str) and item.id]
                return model_ids

        return await self._with_slot(send)

    async def chat(
        self, alias: str, mapping: ModelMapping | None, policy: RequestPolicy,
        messages: list[dict[str, Any]], probe: bool = False, *,
        max_tokens: int | None = None, temperature: float | None = None,
        before_send: Callable[[], Awaitable[None]] | None = None,
        after_send: Callable[[], Awaitable[None]] | None = None,
    ) -> Any:
        """Send bounded chat requests under gateway policy and optional per-attempt authorization.

        ``after_send`` (idempotent) runs once the request body is handed to the transport and again
        after each attempt, so callers can release send fences before the response arrives.
        """
        if max_tokens is not None and not 1 <= max_tokens <= 8192:
            raise ValueError("max_tokens must be between 1 and 8192")
        if temperature is not None and not 0 <= temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        payload: dict[str, Any] = {"messages": messages}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        return await self._request(
            alias, mapping, policy, "chat", "chat/completions", payload, probe, before_send, after_send
        )

    async def stream(
        self, alias: str, mapping: ModelMapping | None, policy: RequestPolicy,
        messages: list[dict[str, Any]], probe: bool = False, *,
        after_send: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncIterator[str]:
        """Stream with telemetry and a per-attempt lock release after request opening.

        ``self.before_send`` runs immediately before every request attempt. ``after_send`` runs as
        soon as the request body is handed to the transport, and again when request creation
        succeeds or fails, so callers can release short-lived send locks (it must be idempotent).
        Telemetry is observational; errors and cancellation propagate, and missing usage stays null.
        """
        started = time.perf_counter()
        first_ms: float | None = None
        usage: Any = None
        ok = False
        try:
            async with aclosing(self._stream_inner(
                alias, mapping, policy, messages, probe, after_send=after_send,
            )) as inner:
                async for line in inner:
                    if first_ms is None:
                        first_ms = (time.perf_counter() - started) * 1000
                    if '"usage"' in line and usage is None:
                        try:
                            usage = json.loads(line[6:]).get("usage")
                        except (ValueError, AttributeError):
                            usage = None
                    yield line
            ok = True
        finally:
            record_model_call("streaming", alias, started, ok, {"usage": usage} if usage else None,
                              mapping.model if mapping else None, first_ms)

    async def _stream_inner(
        self, alias: str, mapping: ModelMapping | None, policy: RequestPolicy,
        messages: list[dict[str, Any]], probe: bool = False, *,
        after_send: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncGenerator[str, None]:
        """Open each fenced request attempt, then stream without retrying emitted chunks."""
        if not may_send(policy, alias, mapping, self.destination_id, bool(self.api_key), "streaming") or self.base_url is None or mapping is None:
            raise PrivacyPolicyDenied("Model request denied by privacy policy")
        if not probe:
            stored = await self.redis.get(capability_key(alias, mapping.model, mapping.version, "streaming", self.gateway_identity))
            try:
                capability_result = json.loads(stored) if stored else {}
            except (TypeError, json.JSONDecodeError):
                capability_result = {}
            if (capability_result.get("result") != "supported"
                    or capability_result.get("gateway_identity") != self.gateway_identity
                    or capability_result.get("model") != mapping.model
                    or capability_result.get("version") != mapping.version):
                raise ModelGatewayError("Streaming capability has not been verified")
        async with self._slot():
            base_url = self.base_url if self.base_url.endswith("/v1") else f"{self.base_url}/v1"
            async with AsyncOpenAI(
                base_url=base_url,
                api_key=self.api_key or "not-configured",
                timeout=self.timeout_seconds,
                max_retries=0,
                http_client=self._http_client(base_url),
            ) as client:
                emitted = False
                for attempt in range(2):
                    try:
                        if self.before_send is not None:
                            await self.before_send()
                        # I6/T7: the transport releases the send fence once the body is handed to
                        # transport.write; publication is re-fenced per flush (T3). Gated (P2-3).
                        # Keep set/reset free of any `yield` (P3-2): reset fails across a context switch.
                        hook = body_sent.set(after_send) if after_send is not None else None
                        try:
                            # Bound header/first-byte wait.
                            async with asyncio.timeout(self.timeout_seconds):
                                stream = await client.chat.completions.create(
                                    model=mapping.model,
                                    messages=cast("Any", messages),  # OpenAI param TypedDicts; built by prompt layer
                                    stream=True,
                                )
                        finally:
                            if hook is not None:
                                body_sent.reset(hook)
                            if after_send is not None:
                                await after_send()
                        async for chunk in stream:
                            emitted = True
                            yield f"data: {json.dumps(chunk.model_dump(mode='json', exclude_none=True))}"
                        yield "data: [DONE]"
                        return
                    except (APITimeoutError, APIConnectionError, TimeoutError) as exc:
                        if attempt == 1 or emitted:
                            raise ModelGatewayError("Model gateway stream failed") from exc
                    except RedisError as exc:
                        raise ModelGatewayError("Model gateway request failed") from exc
                    except APIStatusError as exc:
                        if exc.status_code in {408, 425, 429} or exc.status_code >= 500:  # noqa: SIM102  # style-only rewrite skipped to avoid touching control flow
                            if attempt == 0:
                                await asyncio.sleep(0.1)
                                continue
                        if exc.status_code in {400, 404, 405, 422}:
                            raise CapabilityUnsupported("The configured gateway rejected streaming") from exc
                        raise ModelGatewayError(f"Model gateway returned HTTP {exc.status_code}") from exc
                    except EndpointNetworkPolicyError as exc:
                        raise ModelGatewayError("Model gateway network policy denied the destination") from exc

    async def embed(
        self, alias: str, mapping: ModelMapping | None, policy: RequestPolicy,
        inputs: list[str], probe: bool = False, *,
        before_send: Callable[[], Awaitable[None]] | None = None,
    ) -> Any:
        """Request embeddings through gateway policy with a fresh per-attempt authorization check."""
        return await self._request(
            alias, mapping, policy, "embeddings", "embeddings", {"input": inputs}, probe, before_send
        )

    async def structured(
        self, alias: str, mapping: ModelMapping | None, policy: RequestPolicy,
        messages: list[dict[str, Any]], schema: dict[str, Any], probe: bool = False, *,
        max_tokens: int | None = None, temperature: float | None = None,
        before_send: Callable[[], Awaitable[None]] | None = None,
    ) -> Any:
        """Request schema-constrained output with bounded completion options and per-attempt authorization."""
        if max_tokens is not None and not 1 <= max_tokens <= 8192:
            raise ValueError("max_tokens must be between 1 and 8192")
        if temperature is not None and not 0 <= temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        payload: dict[str, Any] = {
            "messages": messages,
            "response_format": {"type": "json_schema", "json_schema": schema},
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        return await self._request(
            alias, mapping, policy, "structured", "chat/completions", payload, probe, before_send
        )

    async def tools(
        self,
        alias: str,
        mapping: ModelMapping | None,
        policy: RequestPolicy,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        probe: bool = False,
        *,
        max_tokens: int | None = None,
        before_send: Callable[[], Awaitable[None]] | None = None,
    ) -> Any:
        """Request bounded tool-capable chat through the gateway's policy and retry fences.

        The optional completion cap shares chat's 1–8192 validation. ``before_send`` runs on
        every gateway attempt, so callers can revalidate current authorization and source
        identities after a retry without bypassing capability, endpoint, slot, or privacy checks.
        """
        if max_tokens is not None and not 1 <= max_tokens <= 8192:
            raise ValueError("max_tokens must be between 1 and 8192")
        payload: dict[str, Any] = {"messages": messages, "tools": tools}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        return await self._request(
            alias, mapping, policy, "tools", "chat/completions", payload, probe, before_send
        )

    async def rerank(
        self, alias: str, mapping: ModelMapping | None, policy: RequestPolicy,
        query: str, documents: list[str], probe: bool = False, *,
        before_send: Callable[[], Awaitable[None]] | None = None,
        after_send: Callable[[], Awaitable[None]] | None = None,
    ) -> Any:
        """Request bounded reranking with fresh authorization and optional attempt cleanup."""
        return await self._request(
            alias, mapping, policy, "reranking", "rerank",
            {"query": query, "documents": documents}, probe, before_send, after_send,
        )
