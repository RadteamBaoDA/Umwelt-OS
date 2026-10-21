import asyncio
import json
import logging
import math
import random
import re
import secrets
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, aclosing, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

from core.config import Settings
from core.model_gateway.cache import capability_key
from core.model_gateway.policy import may_send
from core.model_gateway.schemas import CapabilityResult, ModelMapping, RequestPolicy
from core.model_gateway.transport import EndpointNetworkPolicyError, approved_http_client, body_sent
from core.telemetry import record_model_call
from core.workspaces.schemas import InternalJobScope, Scope, WorkspaceContext

_LEASE_PREFIX = "bbd:model-gateway:slot:"
_SLOTS = Settings().model_gateway_slots  # MODEL_GATEWAY_SLOTS, read once per process
_LEASE_WAIT_SECONDS = 10.0
# A stream holds its slot for the whole generation, so it queues longer than short calls (no DB locks held while waiting).
_STREAM_LEASE_WAIT_SECONDS = 120.0
_CONNECT_TIMEOUT_SECONDS = 5.0
_LEASE_TTL_SECONDS = 60
_LEASE_REFRESH_SECONDS = 20.0
_REFRESH = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('pexpire', KEYS[1], ARGV[2]) else return 0 end"
# One round trip per poll: take the first free slot (owner token, PX TTL) or return -1.
_ACQUIRE = "for i, k in ipairs(KEYS) do if redis.call('set', k, ARGV[1], 'NX', 'PX', ARGV[2]) then return i - 1 end end return -1"
# Token-checked; one key (normal release) or all slots (sweep after an acquire cut short mid-flight).
_RELEASE = "local n = 0 for _, k in ipairs(KEYS) do if redis.call('get', k) == ARGV[1] then n = n + redis.call('del', k) end end return n"
# Per-upstream circuit breaker (keyed by gateway identity, which tracks provider URL + key): after
# _BREAKER_THRESHOLD counted failures inside _BREAKER_WINDOW_MS the open flag blocks calls for
# _BREAKER_OPEN_MS, so a dead upstream is not hammered by every worker. Fixed window from first failure.
_BREAKER_PREFIX = "bbd:model-gateway:breaker:"
_BREAKER_THRESHOLD = 5
_BREAKER_WINDOW_MS = 60_000
_BREAKER_OPEN_MS = 30_000
_BREAKER_MAX_OPEN_MS = 300_000
# A 429 Retry-After at or above this opens the breaker instead of sleeping inside a held lease.
_RETRY_AFTER_INLINE_SECONDS = 2.0
# KEYS: fail counter, open flag. ARGV: window ms, threshold, forced open ms (0 = none), default open ms.
_BREAKER_RECORD = (
    "local n = redis.call('INCR', KEYS[1]) "
    "if n == 1 then redis.call('PEXPIRE', KEYS[1], ARGV[1]) end "
    "local ms = tonumber(ARGV[3]) "
    "if ms <= 0 and n >= tonumber(ARGV[2]) then ms = tonumber(ARGV[4]) end "
    "if ms > 0 then redis.call('SET', KEYS[2], '1', 'PX', ms) end "
    "return n"
)

_LOG = logging.getLogger(__name__)


def _api_base(url: str) -> str:
    """Append /v1 unless the path already carries a version segment (Gemini /v1beta/openai, Groq /openai/v1)."""
    return url if re.search(r"/v1(beta\d*)?(/|$)", urlsplit(url).path) else f"{url}/v1"


def _raise_if_policy_denied(exc: BaseException) -> None:
    """The SDK wraps transport errors as APIConnectionError; surface a network-policy denial without retrying."""
    if isinstance(exc.__cause__, EndpointNetworkPolicyError):
        raise ModelGatewayError("Model gateway network policy denied the destination") from exc


def _retry_delay() -> float:
    """Jittered pause before the single retry so concurrent workers do not retry in lockstep."""
    return random.uniform(0.25, 1.0)


def _retry_after_seconds(exc: APIStatusError) -> float | None:
    """Parse a Retry-After header (delta-seconds or HTTP-date) from a provider response; None when absent or garbage."""
    try:
        raw = exc.response.headers.get("retry-after")
    except AttributeError:
        return None
    if not isinstance(raw, str) or not raw.strip():
        return None
    raw = raw.strip()
    try:
        seconds = float(raw)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(raw) - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
    # NaN/inf are not a usable hint; negative (past date) means "retry now".
    if math.isnan(seconds) or math.isinf(seconds):
        return None
    return max(seconds, 0.0)


def _retryable_status(status: int) -> bool:
    """Statuses that signal a transient upstream problem: retried once and counted toward the breaker."""
    return status in {408, 425, 429} or status >= 500


class ModelGatewayError(RuntimeError):
    """Base exception for unavailable, rejected, or invalid model gateway operations."""


class ModelGatewayUnavailable(ModelGatewayError):
    """Raised without contacting the upstream while its circuit breaker is open (or a long 429 Retry-After applies).

    The message must differ from "Model capability is not supported", which the entity worker matches verbatim.
    """

    def __init__(self, retry_after: float) -> None:
        """Record the seconds until the upstream may be tried again."""
        super().__init__("Model gateway is temporarily unavailable")
        self.retry_after = retry_after


class _GatewayTimeout(ModelGatewayError):
    """Whole-call deadline exceeded (counts toward the breaker, unlike lease or Redis failures)."""


class PrivacyPolicyDenied(ModelGatewayError):
    """Raised when request policy forbids sending a capability to the configured destination."""


class CapabilityUnsupported(ModelGatewayError):
    """Raised when the gateway rejects a requested model capability."""


@dataclass(frozen=True, slots=True)
class ModelGateway:
    """OpenAI-compatible client enforcing privacy policy, capability verification, bounded concurrency, and approved endpoint networking."""
    redis: Redis = field(repr=False)
    base_url: str | None = field(repr=False)
    api_key: str = field(repr=False)
    destination_id: str
    timeout_seconds: float = 20.0
    scope: Scope = field(kw_only=True)
    gateway_identity: str = field(kw_only=True)
    configuration_revision: int = field(kw_only=True)
    before_send: Callable[[], Awaitable[None]] = field(kw_only=True, repr=False)
    approved_endpoint_cidrs: tuple[str, ...] = ()
    allow_public_endpoints: bool = False

    def __post_init__(self) -> None:
        """Freeze resolved identity/credentials for one request; reject missing scope or fence.

        before_send must freshly check actual flag, exact HTTP session where applicable,
        access/privacy/config/resource fences and release SQL transaction before returning.
        App composition cannot reuse or mutate this client for another caller's settings.
        """
        # The SDK DEBUG request log contains JSON request bodies. Keep prompts
        # and indexed source text out of logs even when OPENAI_LOG=debug is set.
        logging.getLogger("openai").setLevel(logging.WARNING)
        if not isinstance(self.scope, (WorkspaceContext, InternalJobScope)) or not callable(self.before_send):
            raise ValueError("Explicit scope and fresh before_send callback are required")  # noqa: TRY004  # constructor contract raises ValueError for bad wiring
        if isinstance(self.scope, WorkspaceContext) and self.scope.role != "owner":
            raise PrivacyPolicyDenied("Workspace owner required for model execution")
        if len(self.gateway_identity) != 64 or any(char not in "0123456789abcdef" for char in self.gateway_identity):
            raise ValueError("Gateway identity must be a nonsecret SHA256 digest")
        if type(self.configuration_revision) is not int or self.configuration_revision < 0:
            raise ValueError("Explicit configuration revision is required")
        object.__setattr__(self, "base_url", self.base_url.rstrip("/") if self.base_url else None)

    def _policy_matches(self, policy: RequestPolicy) -> bool:
        """Compare typed principal/config identity before capability/cache/transport access."""
        actor = self.scope.actor_user_id if isinstance(self.scope, InternalJobScope) else self.scope.user_id
        return (policy.workspace_id == self.scope.workspace_id and policy.actor_user_id == actor
                and policy.membership_revision == self.scope.membership_revision
                and policy.gateway_identity == self.gateway_identity
                and policy.configuration_revision == self.configuration_revision)

    def _capability_key(self, role: str, mapping: ModelMapping, capability: str) -> str:
        """Resolve only this immutable client's capability namespace, never a global role."""
        actor = self.scope.actor_user_id if isinstance(self.scope, InternalJobScope) else self.scope.user_id
        return capability_key(role, mapping.model, mapping.version, capability, self.gateway_identity,
                              workspace_id=self.scope.workspace_id, actor_user_id=actor)

    async def _capability_supported(self, role: str, mapping: ModelMapping, policy: RequestPolicy, capability: str) -> bool:
        """Read exact principal/config cache evidence; malformed, expired or mismatched values deny."""
        try:
            result = CapabilityResult.model_validate_json(await self.redis.get(self._capability_key(role, mapping, capability)))
            expiry = datetime.fromisoformat(result.expires_at)
        except (ValueError, TypeError):
            return False
        return (result.result == "supported" and expiry.tzinfo is not None and expiry > datetime.now(UTC)
                and result.workspace_id == self.scope.workspace_id and result.actor_user_id == policy.actor_user_id
                and result.membership_revision == self.scope.membership_revision
                and result.configuration_revision == self.configuration_revision
                and result.gateway_identity == self.gateway_identity and result.role == role
                and result.capability == capability and result.model == mapping.model and result.version == mapping.version)

    def _http_client(self, base_url: str) -> httpx.AsyncClient:
        """Create a redirect-disabled HTTP client pinned to an endpoint approved by network policy."""
        try:
            return approved_http_client(base_url, self.approved_endpoint_cidrs, allow_global=self.allow_public_endpoints)
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

    def _slot(self) -> AbstractAsyncContextManager[None]:
        """Lease a gateway slot with the default (short) acquisition wait."""
        return self._lease(_LEASE_WAIT_SECONDS)

    def _timeout(self) -> httpx.Timeout:
        """Overall timeout with a short connect bound so a silent-drop host fails fast."""
        return httpx.Timeout(self.timeout_seconds, connect=min(_CONNECT_TIMEOUT_SECONDS, self.timeout_seconds))

    @asynccontextmanager
    async def _lease(self, wait_seconds: float) -> AsyncIterator[None]:
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
                async with asyncio.timeout(wait_seconds):
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
                raise _GatewayTimeout("Model gateway request timed out") from exc
            except RedisError as exc:
                raise ModelGatewayError("Model gateway request failed") from exc

    def _breaker_keys(self) -> tuple[str, str]:
        """Return (fail counter, open flag) Redis keys for this upstream; identity is a nonsecret digest."""
        base = f"{_BREAKER_PREFIX}{self.gateway_identity}"
        return f"{base}:fail", f"{base}:open"

    async def _breaker_check(self) -> None:
        """Raise ModelGatewayUnavailable while this upstream's open flag exists.

        Fails open: any Redis error, or a non-integer reply (mocked clients), lets the call proceed.
        """
        try:
            _, open_key = self._breaker_keys()
            exists = await self.redis.exists(open_key)
            # Only a real positive int means open; test doubles return truthy mocks.
            if not isinstance(exists, int) or isinstance(exists, bool) or exists <= 0:
                return
            pttl = await self.redis.pttl(open_key)
        except RedisError:
            _LOG.debug("model gateway breaker check failed; proceeding", exc_info=True)
            return
        remaining = pttl if isinstance(pttl, int) and pttl > 0 else _BREAKER_OPEN_MS
        raise ModelGatewayUnavailable(remaining / 1000)

    async def _breaker_fail(self, open_ms: int = 0) -> None:
        """Count one upstream failure; open the breaker at the threshold or for open_ms (provider Retry-After).

        Redis errors are swallowed: the breaker is advisory and must never mask the real failure.
        """
        fail_key, open_key = self._breaker_keys()
        try:
            await cast("Awaitable[Any]", self.redis.eval(
                _BREAKER_RECORD, 2, fail_key, open_key,
                str(_BREAKER_WINDOW_MS), str(_BREAKER_THRESHOLD), str(open_ms), str(_BREAKER_OPEN_MS)))
        except RedisError:
            _LOG.warning("model gateway breaker record failed", exc_info=True)

    async def _breaker_ok(self) -> None:
        """Reset the failure counter after a successful call (plain delete); Redis errors are ignored."""
        try:
            await self.redis.delete(self._breaker_keys()[0])
        except RedisError:
            _LOG.debug("model gateway breaker reset failed", exc_info=True)

    async def _on_retryable_status(self, exc: APIStatusError, attempt: int) -> bool:
        """Decide what a transient HTTP status means; True means the caller should retry now (already slept).

        Raises ModelGatewayUnavailable for a 429 whose Retry-After is too long to sleep inside a held
        lease (the breaker is opened for that duration, capped). On the final attempt the failure is
        counted and False is returned so the caller maps the error.
        """
        if exc.status_code == 429:
            wait = _retry_after_seconds(exc)
            if wait is not None and wait >= _RETRY_AFTER_INLINE_SECONDS:
                # The provider told us when to come back: honour it for every worker via the breaker.
                capped = min(wait, _BREAKER_MAX_OPEN_MS / 1000)
                await self._breaker_fail(open_ms=int(capped * 1000))
                raise ModelGatewayUnavailable(capped) from exc
            if attempt == 0:
                await asyncio.sleep(wait if wait is not None else _retry_delay())
                return True
        elif attempt == 0:
            await asyncio.sleep(_retry_delay())
            return True
        await self._breaker_fail()
        return False

    async def _request(
        self,
        role: str,
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
        if not probe and before_send is None:
            raise ValueError("before_send_required")  # fail closed: every non-probe call needs a per-call fence
        if not self._policy_matches(policy) or not may_send(policy, role, mapping, self.destination_id, bool(self.api_key), capability):
            raise PrivacyPolicyDenied("Model request denied by privacy policy")
        if self.base_url is None or mapping is None:
            raise ModelGatewayError("Model gateway is not configured")
        if not probe and not await self._capability_supported(role, mapping, policy, capability):
            raise ModelGatewayError("Model capability is not supported")
        if not probe:
            # Probes must be able to reach a tripped upstream, otherwise it could never be re-verified.
            await self._breaker_check()
        body = {**payload, "model": mapping.model}
        base_url = _api_base(self.base_url)

        async def send() -> Any:
            """Execute a chat, embedding, or rerank SDK call with bounded retries and normalize its response."""
            async with AsyncOpenAI(
                base_url=base_url,
                api_key=self.api_key or "not-configured",
                timeout=self._timeout(),
                max_retries=0,
                http_client=self._http_client(base_url),
            ) as client:
                for attempt in range(2):
                    try:
                        # Keep the gateway-owned settings check even when a scoped
                        # operation adds its own source/evidence freshness fence.
                        await self.before_send()
                        if before_send is not None and before_send is not self.before_send:
                            await before_send()
                        if not self._policy_matches(policy) or not may_send(policy, role, mapping, self.destination_id, bool(self.api_key), capability):
                            raise PrivacyPolicyDenied("Model request denied by privacy policy")
                        # Gated (P2-3): with no after_send the ContextVar is never touched, so the
                        # transport sends request.stream unwrapped exactly as before.
                        hook = body_sent.set(after_send) if after_send is not None else None
                        try:
                            if path == "chat/completions":
                                response = await client.chat.completions.create(**body)
                            elif path == "embeddings":
                                response = await client.embeddings.create(**body)
                            elif path == "rerank":
                                response = await client.post("/rerank", cast_to=dict[str, Any], body=body)
                            elif path == "decisions":
                                # OpenRouter Decisions (alpha) lives beside /api/v1 at /api/alpha; same host, so the
                                # pinned http client and endpoint policy above already cover it.
                                response = await client.post(
                                    re.sub(r"/v1$", "/alpha", base_url) + "/decisions", cast_to=dict[str, Any], body=body)
                            else:
                                raise ModelGatewayError("Unsupported model gateway operation")
                        finally:
                            if hook is not None:
                                body_sent.reset(hook)
                            # Fallback release (connect/write failure, timeout); after_send is idempotent.
                            if after_send is not None:
                                await after_send()
                    except (APITimeoutError, APIConnectionError) as exc:
                        _raise_if_policy_denied(exc)  # a policy denial is not an upstream fault: no retry, no count
                        if attempt == 0:
                            await asyncio.sleep(_retry_delay())
                            continue
                        await self._breaker_fail()
                        raise ModelGatewayError("Model gateway request failed") from exc
                    except APIStatusError as exc:
                        if _retryable_status(exc.status_code) and await self._on_retryable_status(exc, attempt):
                            continue
                        if exc.status_code in {400, 404, 405, 422}:
                            raise CapabilityUnsupported("The configured gateway rejected this capability") from exc
                        raise ModelGatewayError(f"Model gateway returned HTTP {exc.status_code}") from exc
                    if hasattr(response, "model_dump"):
                        return response.model_dump(mode="json", exclude_none=True)
                    if isinstance(response, dict):
                        return response
                    raise ModelGatewayError("Model gateway returned an invalid response")
            raise ModelGatewayError("Model gateway request failed")

        # Telemetry only: labels are capability and configured role; model name is identity, never a label.
        started = time.perf_counter()
        try:
            response = await self._with_slot(send)
        except _GatewayTimeout:
            await self._breaker_fail()
            record_model_call(capability, role, started, False)
            raise
        except BaseException:
            record_model_call(capability, role, started, False)
            raise
        await self._breaker_ok()
        record_model_call(capability, role, started, True, response, mapping.model)
        return response

    async def discover_models(self) -> list[str]:
        """List model IDs from the configured gateway while holding capacity and enforcing endpoint policy."""
        if self.base_url is None or not (self.api_key or self.destination_id.startswith("local:")):
            raise ModelGatewayError("Model gateway is not configured")
        base_url = _api_base(self.base_url)

        async def send() -> list[str]:
            """Call the configured model-list endpoint and return valid model IDs; map SDK and network failures to the gateway error."""
            await self.before_send()
            async with AsyncOpenAI(base_url=base_url, api_key=self.api_key,
                                   timeout=self._timeout(), max_retries=0,
                                   http_client=self._http_client(base_url)) as client:
                try:
                    page = await client.models.list()
                except (APIConnectionError, APITimeoutError, APIStatusError) as exc:
                    _raise_if_policy_denied(exc)
                    # Static reason only (no upstream body/URL): lets the owner tell auth, timeout and DNS/TLS apart.
                    if isinstance(exc, APIStatusError):
                        reason = f"upstream returned HTTP {exc.status_code}"
                    elif isinstance(exc, APITimeoutError):
                        reason = "upstream timed out"
                    else:
                        reason = f"could not connect to upstream ({type(exc.__cause__ or exc).__name__})"
                    raise ModelGatewayError(f"Model gateway discovery failed: {reason}") from exc
                model_ids: list[str] = [item.id for item in page.data if isinstance(item.id, str) and item.id]
                return model_ids

        models = await self._with_slot(send)
        # Discovery skips the open check (it is how a tripped upstream is re-verified) but success closes it.
        await self._breaker_ok()
        return models

    async def discover_decision_models(self) -> dict[str, Any]:
        """Fetch the raw OpenRouter ``/models?output_modalities=decisions`` listing under capacity and endpoint policy.

        The caller validates and bounds the payload (``parse_decision_models``). Errors carry a static reason only.
        """
        if self.base_url is None or not self.api_key:
            raise ModelGatewayError("Model gateway is not configured")
        base_url = _api_base(self.base_url)

        async def send() -> dict[str, Any]:
            """GET the filtered model list and map SDK/network failures to the gateway error."""
            await self.before_send()
            async with AsyncOpenAI(base_url=base_url, api_key=self.api_key, timeout=self._timeout(), max_retries=0,
                                   http_client=self._http_client(base_url)) as client:
                try:
                    page = await client.get("/models", cast_to=dict[str, Any],
                                            options={"params": {"output_modalities": "decisions"}})
                except (APIConnectionError, APITimeoutError, APIStatusError) as exc:
                    _raise_if_policy_denied(exc)
                    if isinstance(exc, APIStatusError):
                        reason = f"upstream returned HTTP {exc.status_code}"
                    elif isinstance(exc, APITimeoutError):
                        reason = "upstream timed out"
                    else:
                        reason = "could not connect to upstream"
                    raise ModelGatewayError(f"Model gateway discovery failed: {reason}") from exc
                return page if isinstance(page, dict) else {}

        return await self._with_slot(send)

    async def decisions(
        self, role: str, mapping: ModelMapping | None, policy: RequestPolicy,
        state: list[Any], questions: dict[str, Any], probe: bool = False, *,
        before_send: Callable[[], Awaitable[None]] | None = None,
    ) -> Any:
        """Ask an OpenRouter Decisions-API model (alpha) the given questions about ``state``.

        Uses the ``structured`` capability slot (verified by Test routing) and the same privacy, breaker, lease,
        retry and endpoint fences as chat calls.
        """
        return await self._request(
            role, mapping, policy, "structured", "decisions", {"state": state, "questions": questions}, probe, before_send)

    async def chat(
        self, role: str, mapping: ModelMapping | None, policy: RequestPolicy,
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
            role, mapping, policy, "chat", "chat/completions", payload, probe, before_send, after_send
        )

    async def stream(
        self, role: str, mapping: ModelMapping | None, policy: RequestPolicy,
        messages: list[dict[str, Any]], probe: bool = False, *,
        after_send: Callable[[], Awaitable[None]] | None = None,
        before_send: Callable[[], Awaitable[None]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """Stream with telemetry and fresh authorization before each request opening.

        Every before_send closes its short SQL transaction before provider I/O. ``after_send``
        (idempotent) runs as soon as the request body is handed to the transport and again when
        request creation succeeds or fails; it performs optional operation cleanup or send-fence release.
        Telemetry is observational; errors and cancellation propagate, and missing usage stays null.
        Optional ``tools``/``tool_choice``/``max_tokens`` are forwarded only when provided; passing
        ``tools`` additionally requires the verified ``tools`` capability (CapabilityUnsupported otherwise).
        """
        started = time.perf_counter()
        first_ms: float | None = None
        usage: Any = None
        ok = False
        try:
            async with aclosing(self._stream_inner(
                role, mapping, policy, messages, probe, after_send=after_send, before_send=before_send,
                tools=tools, tool_choice=tool_choice, max_tokens=max_tokens,
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
            record_model_call("streaming", role, started, ok, {"usage": usage} if usage else None,
                              mapping.model if mapping else None, first_ms)

    async def _stream_inner(
        self, role: str, mapping: ModelMapping | None, policy: RequestPolicy,
        messages: list[dict[str, Any]], probe: bool = False, *,
        after_send: Callable[[], Awaitable[None]] | None = None,
        before_send: Callable[[], Awaitable[None]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        max_tokens: int | None = None,
    ) -> AsyncGenerator[str, None]:
        """Open each fenced request attempt, then stream without retrying emitted chunks.

        ``tools`` (OpenAI function specs) require both ``streaming`` and ``tools`` capabilities to
        pass ``may_send``; an unverified ``tools`` capability raises CapabilityUnsupported before any I/O.
        ``tool_choice`` and ``max_tokens`` are forwarded only when not None.
        """
        capabilities = ("streaming", "tools") if tools else ("streaming",)
        if not self._policy_matches(policy) or not all(may_send(policy, role, mapping, self.destination_id, bool(self.api_key), c) for c in capabilities) or self.base_url is None or mapping is None:
            raise PrivacyPolicyDenied("Model request denied by privacy policy")
        if not probe and not await self._capability_supported(role, mapping, policy, "streaming"):
            raise ModelGatewayError("Streaming capability has not been verified")
        if tools and not probe and not await self._capability_supported(role, mapping, policy, "tools"):
            raise CapabilityUnsupported("Tool calling capability has not been verified")
        extra: dict[str, Any] = {}
        if tools:
            extra["tools"] = tools
        if tool_choice is not None:
            extra["tool_choice"] = tool_choice
        if max_tokens is not None:
            extra["max_tokens"] = max_tokens
        if not probe:
            await self._breaker_check()
        async with self._lease(_STREAM_LEASE_WAIT_SECONDS):
            base_url = _api_base(self.base_url)
            async with AsyncOpenAI(
                base_url=base_url,
                api_key=self.api_key or "not-configured",
                timeout=self._timeout(),
                max_retries=0,
                http_client=self._http_client(base_url),
            ) as client:
                emitted = False
                for attempt in range(2):
                    try:
                        await self.before_send()
                        if before_send is not None and before_send is not self.before_send:
                            await before_send()
                        if not self._policy_matches(policy) or not all(may_send(policy, role, mapping, self.destination_id, bool(self.api_key), c) for c in capabilities):
                            raise PrivacyPolicyDenied("Model request denied by privacy policy")
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
                                    **extra,
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
                        await self._breaker_ok()
                        return
                    except (APITimeoutError, APIConnectionError, TimeoutError) as exc:
                        _raise_if_policy_denied(exc)
                        if attempt == 1 or emitted:
                            # Mid-stream failures say little about upstream health; count only pre-first-token ones.
                            if not emitted:
                                await self._breaker_fail()
                            raise ModelGatewayError("Model gateway stream failed") from exc
                        await asyncio.sleep(_retry_delay())
                    except RedisError as exc:
                        raise ModelGatewayError("Model gateway request failed") from exc
                    except APIStatusError as exc:
                        # A status error after chunks were emitted is never retried or counted.
                        if not emitted and _retryable_status(exc.status_code) and await self._on_retryable_status(exc, attempt):
                            continue
                        if exc.status_code in {400, 404, 405, 422}:
                            raise CapabilityUnsupported("The configured gateway rejected streaming") from exc
                        raise ModelGatewayError(f"Model gateway returned HTTP {exc.status_code}") from exc

    async def embed(
        self, role: str, mapping: ModelMapping | None, policy: RequestPolicy,
        inputs: list[str], probe: bool = False, *,
        before_send: Callable[[], Awaitable[None]] | None = None,
    ) -> Any:
        """Request embeddings through gateway policy with a fresh per-attempt authorization check."""
        return await self._request(
            role, mapping, policy, "embeddings", "embeddings", {"input": inputs}, probe, before_send
        )

    async def structured(
        self, role: str, mapping: ModelMapping | None, policy: RequestPolicy,
        messages: list[dict[str, Any]], schema: dict[str, Any], probe: bool = False, *,
        max_tokens: int | None = None, temperature: float | None = None,
        before_send: Callable[[], Awaitable[None]] | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> Any:
        """Request schema-constrained output with bounded completion options and per-attempt authorization.

        ``extra_body`` carries provider-specific request keys (e.g. OpenRouter ``provider``) verbatim.
        """
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
        if extra_body:
            payload["extra_body"] = extra_body
        return await self._request(
            role, mapping, policy, "structured", "chat/completions", payload, probe, before_send
        )

    async def tools(
        self,
        role: str,
        mapping: ModelMapping | None,
        policy: RequestPolicy,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        probe: bool = False,
        *,
        max_tokens: int | None = None,
        before_send: Callable[[], Awaitable[None]] | None = None,
        tool_choice: Any = None,
        extra_body: dict[str, Any] | None = None,
    ) -> Any:
        """Request bounded tool-capable chat through the gateway's policy and retry fences.

        Optional ``tool_choice`` (e.g. a forced function) and ``extra_body`` are forwarded only when set.

        The optional completion cap shares chat's 1–8192 validation. ``before_send`` runs on
        every gateway attempt, so callers can revalidate current authorization and source
        identities after a retry without bypassing capability, endpoint, slot, or privacy checks.
        """
        if max_tokens is not None and not 1 <= max_tokens <= 8192:
            raise ValueError("max_tokens must be between 1 and 8192")
        payload: dict[str, Any] = {"messages": messages, "tools": tools}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if extra_body:
            payload["extra_body"] = extra_body
        return await self._request(
            role, mapping, policy, "tools", "chat/completions", payload, probe, before_send
        )

    async def rerank(
        self, role: str, mapping: ModelMapping | None, policy: RequestPolicy,
        query: str, documents: list[str], probe: bool = False, *,
        before_send: Callable[[], Awaitable[None]] | None = None,
        after_send: Callable[[], Awaitable[None]] | None = None,
    ) -> Any:
        """Request bounded reranking with fresh authorization and optional attempt cleanup."""
        return await self._request(
            role, mapping, policy, "reranking", "rerank",
            {"query": query, "documents": documents}, probe, before_send, after_send,
        )
