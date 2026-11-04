import asyncio
import json
import logging
import math
import random
import re
import secrets
import time
import weakref
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, aclosing, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal, NoReturn, Protocol, cast
from urllib.parse import urlsplit

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

from core.config import Settings
from core.metrics import record_breaker_open
from core.model_gateway import anthropic
from core.model_gateway.policy import may_send
from core.model_gateway.pool import pinned_client
from core.model_gateway.schemas import (
    HOSTED_PROVIDER_ENDPOINTS,
    HOSTED_PROVIDER_KINDS,
    PROVIDER_KINDS_WITHOUT_MAX_TOKENS,
    ModelMapping,
    ProviderKind,
    RequestPolicy,
)
from core.model_gateway.transport import EndpointNetworkPolicyError, body_sent
from core.model_gateway.wire import (
    CHAT_PATH,
    UnsupportedWireOperation,
    WireProtocol,
    WireSession,
    wire_protocol_for,
)
from core.telemetry import record_model_call
from core.workspaces.schemas import InternalJobScope, Scope, WorkspaceContext

_LEASE_PREFIX = "bbd:model-gateway:slot:"
_SLOTS = Settings().model_gateway_slots  # MODEL_GATEWAY_SLOTS, read once per process
_LEASE_WAIT_SECONDS = 10.0
# Connection-level Redis errors are retried this long (pool exhaustion is transient) before the lease
# degrades to the per-process fallback semaphore.
_REDIS_PROBE_SECONDS = 1.0
# Weakly keyed by loop so entries vanish with a closed loop (no id() reuse, no leak).
_LOCAL_SLOTS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, tuple[int, asyncio.Semaphore]]" = weakref.WeakKeyDictionary()
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


def _local_semaphore() -> asyncio.Semaphore:
    """Per-process (per event loop) fallback semaphore sized like ``MODEL_GATEWAY_SLOTS``; see ``_lease``."""
    loop = asyncio.get_running_loop()
    entry = _LOCAL_SLOTS.get(loop)
    if entry is None or entry[0] != _SLOTS:
        entry = _LOCAL_SLOTS[loop] = (_SLOTS, asyncio.Semaphore(_SLOTS))
    return entry[1]


def _api_base(url: str) -> str:
    """Append /v1 unless the path already carries a version segment (Gemini /v1beta/openai, Groq /openai/v1)."""
    return url if re.search(r"/v1(beta\d*)?(/|$)", urlsplit(url).path) else f"{url}/v1"


def hosted_endpoint_equivalent(kind: str, url: str | None) -> bool:
    """True iff ``kind`` is a hosted vendor and ``url`` resolves to the same effective API base as its fixed endpoint.

    Only scheme and host are lowercased (the path is case-sensitive) and a trailing slash is ignored, so
    ``https://API.openai.com`` equals ``https://api.openai.com/v1``. Pure; no I/O.
    """
    fixed = HOSTED_PROVIDER_ENDPOINTS.get(kind)
    if not fixed or not url:
        return False
    parts = urlsplit(url.rstrip("/"))
    lowered = parts._replace(scheme=parts.scheme.lower(), netloc=parts.netloc.lower()).geturl()
    return _api_base(lowered) == _api_base(fixed)


def effective_provider_kind(kind: str, url: str | None) -> str:
    """Return ``"custom"`` for a hosted kind whose stored URL is not the vendor endpoint, else ``kind`` unchanged.

    Legacy rows (e.g. ``openai`` pointed at a proxy) keep working as ``custom`` without touching URL or key.
    """
    if kind in HOSTED_PROVIDER_KINDS and url and not hosted_endpoint_equivalent(kind, url):
        return "custom"
    return kind


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


_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


def _json_instruction(schema: dict[str, Any]) -> dict[str, str]:
    """System message asking for exactly one JSON value matching ``schema`` (the json_schema ``schema`` field if present)."""
    body = schema.get("schema", schema) if isinstance(schema, dict) else schema
    return {"role": "system", "content": (
        "Reply with exactly one JSON value that validates against this JSON Schema. "
        "No prose, no markdown, no code fences. JSON Schema: " + json.dumps(body, separators=(",", ":")))}


def _extract_json(text: str) -> str:
    """Strip reasoning tags and code fences, then cut to the outermost JSON object/array; unparseable text is returned as-is."""
    cleaned = _THINK.sub("", text).strip()
    fenced = _FENCE.match(cleaned)
    if fenced:
        cleaned = fenced.group(1).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = cleaned.find(opener), cleaned.rfind(closer)
        if start != -1 and end > start:
            candidate = cleaned[start:end + 1]
            try:
                json.loads(candidate)
            except ValueError:
                continue
            return candidate
    return cleaned


def _with_extracted_json(response: Any) -> Any:
    """Replace the first choice's text content with its extracted JSON; other shapes pass through for caller validation."""
    choices = response.get("choices") if isinstance(response, dict) else None
    message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        message["content"] = _extract_json(message["content"])
    return response


class ModelGatewayError(RuntimeError):
    """Base exception for unavailable, rejected, or invalid model gateway operations."""


class ModelGatewayUnavailable(ModelGatewayError):
    """Raised without contacting the upstream while its circuit breaker is open (or a long 429 Retry-After applies).

    It is a transient condition, never a capability verdict, and is not recorded as evidence.
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


class LocalCapabilityError(CapabilityUnsupported):
    """A request the gateway itself could not translate for the wire protocol (e.g. a forced ``tool_choice`` it cannot honour).

    Callers still see ``CapabilityUnsupported`` and fall back, but no provider was asked, so it says nothing
    about the model and is never recorded as capability evidence.
    """


class CapabilityEvidenceStore(Protocol):
    """Persistent per-model capability evidence supplied by the composing module (core never imports modules).

    Evidence is learned from real provider outcomes and is advisory: only a recorded ``unsupported`` for an
    optional capability with a fallback (tools, structured) changes behavior. Implementations must key by
    the model identity so a credential/endpoint/default change invalidates old evidence.
    """

    async def lookup(self, role: str, mapping: ModelMapping, capability: str) -> str | None:
        """Return the current ``supported``/``unsupported``/``failed`` result for this model capability, or None."""
        ...

    async def record(self, role: str, mapping: ModelMapping, capability: str,
                     result: Literal["supported", "unsupported", "failed"]) -> None:
        """Persist the outcome of a real call; may raise, the gateway swallows and logs only the error type."""
        ...


# An explicit "this model/endpoint cannot do tools" statement. A bare mention of "tool"/"function" is not enough:
# a 400 about a malformed tool schema, a bad tool_use_id pairing or a wrong argument must not mark the model
# unsupported (the evidence has no other way back than the TTL).
_TOOL_UNSUPPORTED_PHRASE = re.compile(
    r"(?:does not|doesn't|do not|don't|cannot|can't) support\s+(?:the\s+)?"
    r"(?:tools?\b|tool[ -]?(?:use|usage|calling|calls?)\b|function[ -]?(?:calling|calls?)\b|functions\b)"
    r"|(?:tools?|tool[ -]?(?:use|usage|calling)|function[ -]?(?:calling|calls?))\s+(?:is|are)\s+(?:not\s+supported|unsupported|not\s+available)"
    r"|support tool[ -]?use",
    re.IGNORECASE,
)
# Messages that are about one specific tool turn or tool definition, never about the model's capability.
_TOOL_MESSAGE_ERROR = re.compile(r"tool_use_id|tool_use_ids|tool_result|tool_call_id|tool_choice|input_schema|tools\.\d+|invalid[^.]{0,40}schema", re.IGNORECASE)
_TOOL_UNSUPPORTED_STATUSES = frozenset({404, 405})
# How long a recorded ``unsupported`` capability may short-circuit calls before the provider is asked again.
UNSUPPORTED_EVIDENCE_TTL_SECONDS = 3600
# Per-model memo that the provider rejected the large default ``max_tokens`` (see ModelGateway._remember_clamp).
_CLAMP_PREFIX = "bbd:model-gateway:maxtok-clamp:"
_CLAMP_TTL_SECONDS = 3600


def _status_of(exc: BaseException) -> int | None:
    """HTTP status of the upstream error behind a gateway error (cause chain, or the ``HTTP <n>:`` text)."""
    cause: BaseException | None = exc
    for _ in range(4):
        if isinstance(cause, APIStatusError):
            return cause.status_code
        cause = cause.__cause__ if cause is not None else None
    match = re.search(r"HTTP (\d{3})\b", str(exc))
    return int(match[1]) if match else None


def is_tool_capability_rejection(exc: BaseException) -> bool:
    """True when a gateway error says the model/endpoint cannot do tool calling at all.

    Only an explicit capability phrase (``does not support tools``, ``tool use is not supported``,
    ``function calling is not supported`` ...) or a 404/405 from the endpoint qualifies. Messages about
    ``tool_use_id``/``tool_result``/``tool_call_id`` pairing or a tool's schema never do.
    """
    text = str(exc)
    if _TOOL_MESSAGE_ERROR.search(text):
        return False
    if _TOOL_UNSUPPORTED_PHRASE.search(text):
        return True
    return _status_of(exc) in _TOOL_UNSUPPORTED_STATUSES


def _is_transient_failure(exc: BaseException) -> bool:
    """True for timeouts, connection/Redis faults, 429 and 5xx: outcomes that say nothing about the model.

    Walks the ``__cause__`` chain. Deterministic provider answers (400/401/403/404/422) are not transient.
    """
    cause: BaseException | None = exc
    for _ in range(4):
        if isinstance(cause, (APITimeoutError, APIConnectionError, TimeoutError, RedisError, _GatewayTimeout)):
            return True
        if isinstance(cause, APIStatusError):
            return cause.status_code >= 500 or cause.status_code == 429
        cause = cause.__cause__ if cause is not None else None
    return False


def _mentions_max_tokens(exc: APIStatusError) -> bool:
    """True for a 400/422 whose provider message names ``max_tokens`` (output limit), not a capability."""
    return exc.status_code in {400, 422} and "max_tokens" in str(exc.message or "").lower()


# Obvious credential shapes that providers sometimes echo back in error text: "Bearer x", vendor
# "sk-..." / "key-..." tokens, and long unbroken base64/hex runs. Best-effort, not a guarantee.
_SECRET_PATTERNS = re.compile(
    r"(?i:bearer\s+\S+)|\b(?:sk|key|pk|rk)-[A-Za-z0-9_\-]{8,}|[A-Za-z0-9+/]{32,}={0,2}",
)


def _redact_secrets(text: str) -> str:
    """Replace API-key-like tokens, bearer credentials and long base64/hex runs with ``[redacted]``."""
    return _SECRET_PATTERNS.sub("[redacted]", text)


_THINK_OPEN, _THINK_CLOSE = "<think>", "</think>"


class _ThinkSplitter:
    """Incrementally split a leading inline ``<think>...</think>`` block from answer text.

    Only a block at the very start of the content (after optional whitespace) counts as reasoning;
    a tag appearing later is ordinary answer text. Tags split across chunks are held back until they
    can be decided. ``flush`` releases whatever is still held when the stream ends.
    """

    def __init__(self) -> None:
        """Start undecided: the first non-space characters decide whether a think block opens."""
        self._state = "start"
        self._held = ""

    def feed(self, text: str) -> tuple[str, str]:
        """Consume one content chunk and return ``(reasoning, answer)`` text that is safe to emit now."""
        if self._state == "answer":
            return "", text
        self._held += text
        if self._state == "start":
            lead = self._held.lstrip()
            if lead.startswith(_THINK_OPEN):
                self._state, self._held = "think", lead[len(_THINK_OPEN):]
            elif _THINK_OPEN.startswith(lead):
                return "", ""  # possibly a split opening tag (or only whitespace so far)
            else:
                self._state, answer, self._held = "answer", self._held, ""
                return "", answer
        end = self._held.find(_THINK_CLOSE)
        if end >= 0:
            reasoning, answer = self._held[:end], self._held[end + len(_THINK_CLOSE):].lstrip("\n")
            self._state, self._held = "answer", ""
            return reasoning, answer
        # Hold back a trailing fragment that could still grow into the closing tag.
        keep = next((n for n in range(len(_THINK_CLOSE) - 1, 0, -1) if self._held.endswith(_THINK_CLOSE[:n])), 0)
        reasoning, self._held = self._held[:len(self._held) - keep], self._held[len(self._held) - keep:]
        return reasoning, ""

    def flush(self) -> tuple[str, str]:
        """Release held text at end of stream: undecided text is answer, an unclosed block is reasoning."""
        held, state = self._held, self._state
        self._held = ""
        return (held, "") if state == "think" else ("", held)


def _split_reasoning(payload: dict[str, Any], splitters: dict[int, _ThinkSplitter]) -> None:
    """Rewrite one decoded chunk in place so reasoning rides ``delta.reasoning_content``, not ``delta.content``.

    Provider-native ``reasoning`` is folded into ``reasoning_content``; inline think blocks in content are
    split out by a per-choice ``_ThinkSplitter``. Answer content therefore never contains think tags.
    """
    for choice in payload.get("choices") or []:
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        reasoning = delta.pop("reasoning", None)
        native = delta.pop("reasoning_content", None)
        parts = [t for t in (native, reasoning) if isinstance(t, str) and t]
        content = delta.get("content")
        if isinstance(content, str):
            think, answer = splitters.setdefault(choice.get("index", 0), _ThinkSplitter()).feed(content)
            if think:
                parts.append(think)
            if answer:
                delta["content"] = answer
            else:
                delta.pop("content")
        if parts:
            delta["reasoning_content"] = "".join(parts)


def _upstream_reason(exc: APIStatusError) -> str:
    """Return ``HTTP <status>: <upstream message>`` (secret-redacted, capped at 300 chars) for owner-facing errors.

    The SDK message is the provider's own error text; it is redacted before inclusion because some
    providers echo the offending key or Authorization header back.
    """
    text = _redact_secrets(" ".join(str(exc.message or "").split()))
    return f"HTTP {exc.status_code}: {text[:300]}" if text else f"HTTP {exc.status_code}"


@dataclass(slots=True)
class _StreamState:
    """Mutable retry bookkeeping of one streaming call (see ``ModelGateway._on_stream_status_error``)."""

    extra: dict[str, Any]
    attempt: int = 0  # transient-retry budget used (0 or 1); a clamp, effort drop or transient retry each consume it
    effort_dropped: bool = False  # reasoning_effort was dropped; the clamp may still use the retry after it
    emitted: bool = False  # a chunk reached the caller: nothing may be retried any more
    clamped: bool = False  # the max_tokens clamp retry was used


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
    # Namespace of capability evidence; defaults to gateway_identity (strict) when a caller has no split identity.
    model_identity: str = field(default="", kw_only=True)
    before_send: Callable[[], Awaitable[None]] = field(kw_only=True, repr=False)
    # Optional persistent capability evidence (see CapabilityEvidenceStore); None disables lookup and recording.
    evidence: CapabilityEvidenceStore | None = field(default=None, kw_only=True, repr=False)
    approved_endpoint_cidrs: tuple[str, ...] = ()
    allow_public_endpoints: bool = False
    # Provider kind selects the wire protocol: "anthropic" uses the native Messages API, everything else OpenAI-compatible.
    kind: ProviderKind = field(default="custom", kw_only=True)
    # Wire strategy chosen once from ``kind`` (registry lookup in core.model_gateway.wire); never passed by callers.
    protocol: WireProtocol = field(init=False, repr=False, compare=False)

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
        if not self.model_identity:
            object.__setattr__(self, "model_identity", self.gateway_identity)
        elif len(self.model_identity) != 64 or any(char not in "0123456789abcdef" for char in self.model_identity):
            raise ValueError("Model identity must be a nonsecret SHA256 digest")
        object.__setattr__(self, "base_url", self.base_url.rstrip("/") if self.base_url else None)
        object.__setattr__(self, "protocol", wire_protocol_for(self.kind))

    def _policy_matches(self, policy: RequestPolicy) -> bool:
        """Compare typed principal/config identity before capability/cache/transport access."""
        actor = self.scope.actor_user_id if isinstance(self.scope, InternalJobScope) else self.scope.user_id
        return (policy.workspace_id == self.scope.workspace_id and policy.actor_user_id == actor
                and policy.membership_revision == self.scope.membership_revision
                and policy.gateway_identity == self.gateway_identity
                and policy.configuration_revision == self.configuration_revision)

    async def _known_unsupported(self, role: str, mapping: ModelMapping, capability: str) -> bool:
        """True only when stored evidence says this optional capability is unsupported.

        Missing, ``supported``/``failed`` or unreadable evidence never blocks: a lookup failure is logged by
        type only and the real provider call decides.
        """
        if self.evidence is None:
            return False
        try:
            return await self.evidence.lookup(role, mapping, capability) == "unsupported"
        except Exception as exc:  # noqa: BLE001  # evidence is advisory and must never fail the user request
            _LOG.warning("Capability evidence lookup failed (%s)", type(exc).__name__)
            return False

    async def _record(self, role: str, mapping: ModelMapping | None, capability: str,
                      result: Literal["supported", "unsupported", "failed"]) -> None:
        """Record a real call outcome; failures never reach the caller (log type only)."""
        if self.evidence is None or mapping is None:
            return
        try:
            await self.evidence.record(role, mapping, capability, result)
        except Exception as exc:  # noqa: BLE001  # recording is best-effort and must never fail the user request
            _LOG.warning("Capability evidence record failed (%s)", type(exc).__name__)

    async def _record_failure(self, role: str, mapping: ModelMapping | None, capability: str, exc: BaseException,
                              *, rejected_tools: bool = False) -> None:
        """Record a deterministic provider failure as ``failed``; ``unsupported`` only for a clear tool rejection.

        Privacy denials, breaker-open errors and local translation errors are not provider outcomes and record nothing. Transient errors
        (timeouts, connection faults, 429, 5xx) record nothing either, so one blip never overwrites a stored
        ``supported``. Deterministic errors (400/401/403/404/422) are ``failed``; ``unsupported`` for ``tools``
        needs an explicit capability phrase or a 404/405 (see ``is_tool_capability_rejection``).
        """
        if not isinstance(exc, ModelGatewayError) or isinstance(
                exc, (PrivacyPolicyDenied, ModelGatewayUnavailable, LocalCapabilityError)):
            return
        if _is_transient_failure(exc):
            return
        await self._record(role, mapping, capability, "failed")
        if rejected_tools and isinstance(exc, CapabilityUnsupported) and is_tool_capability_rejection(exc):
            await self._record(role, mapping, "tools", "unsupported")

    def _configured_max_tokens(self, mapping: ModelMapping | None) -> int | None:
        """Owner-configured output limit of the mapping, or None for hosted kinds (server-side re-check)."""
        if mapping is None or self.kind in PROVIDER_KINDS_WITHOUT_MAX_TOKENS:
            return None
        return mapping.max_output_tokens

    def _clamp_key(self, mapping: ModelMapping) -> str:
        """Redis key of the "provider rejected the large default max_tokens" memo for this model identity."""
        return f"{_CLAMP_PREFIX}{self.model_identity}:{mapping.model}"

    async def _clamp_active(self, mapping: ModelMapping) -> bool:
        """True while a recent Anthropic ``max_tokens`` 400 says the default must be lowered; Redis errors read as False."""
        try:
            exists = await self.redis.exists(self._clamp_key(mapping))
        except RedisError:
            return False
        return isinstance(exists, int) and not isinstance(exists, bool) and exists > 0

    async def _remember_clamp(self, mapping: ModelMapping) -> None:
        """Remember for one hour that this model needs the lowered ``max_tokens``; best-effort, never raises."""
        try:
            await self.redis.set(self._clamp_key(mapping), "1", ex=_CLAMP_TTL_SECONDS)
        except RedisError:
            _LOG.debug("model gateway max_tokens clamp record failed", exc_info=True)

    async def _initial_max_tokens(self, mapping: ModelMapping, explicit: int | None) -> int | None:
        """Request ``max_tokens``: explicit caller value, else the configured limit, else (Anthropic, clamped) the low default.

        None means "send nothing": OpenAI-compatible providers then use their own default and the Anthropic
        translation uses its internal default.
        """
        if explicit is not None:
            return explicit
        configured = self._configured_max_tokens(mapping)
        if configured is not None:
            return configured
        if self.protocol.requires_max_tokens and await self._clamp_active(mapping):
            return anthropic.DEFAULT_MAX_TOKENS
        return None

    def _rejects_max_tokens(self, exc: APIStatusError) -> bool:
        """True when ``exc`` is a ``max_tokens`` parameter 400 from a protocol that mandates ``max_tokens``.

        OpenAI-compatible and OpenRouter/custom upstreams never get this reclassification: for them a 400
        naming ``max_tokens`` keeps its normal capability mapping.
        """
        return self.protocol.requires_max_tokens and _mentions_max_tokens(exc)

    async def _clamp_on_max_tokens_error(self, exc: APIStatusError, mapping: ModelMapping, current: int | None,
                                         *, stream: bool) -> int | None:
        """Lower value to retry once with after an Anthropic ``max_tokens`` 400, else None (no retry).

        Applies only to protocols that require ``max_tokens`` (Anthropic) when the effective limit exceeds ``DEFAULT_MAX_TOKENS``; the clamp
        is remembered per model identity for an hour. It is a parameter problem, never a capability verdict.
        """
        if not self._rejects_max_tokens(exc):
            return None
        effective = current or (anthropic.DEFAULT_STREAM_MAX_TOKENS if stream else anthropic.DEFAULT_MAX_TOKENS)
        if effective <= anthropic.DEFAULT_MAX_TOKENS:
            return None
        _LOG.warning("Provider rejected max_tokens (%s); retrying with %d", _upstream_reason(exc), anthropic.DEFAULT_MAX_TOKENS)
        await self._remember_clamp(mapping)
        return anthropic.DEFAULT_MAX_TOKENS

    def _http_client(self, base_url: str) -> httpx.AsyncClient:
        """Return the per-process shared redirect-disabled HTTP client pinned to an approved endpoint.

        One client per (origin, approved CIDRs, allow_global, protocol) is reused across calls
        (``core.model_gateway.pool``); the pinned transport still resolves and validates DNS on every
        request, so the SSRF/DNS-pinning guarantees equal those of a per-call client. Its ``aclose`` is a
        no-op for the SDK's context exit; the pool closes it at shutdown.
        """
        try:
            return pinned_client(
                base_url, self.approved_endpoint_cidrs, allow_global=self.allow_public_endpoints,
                protocol=type(self.protocol).__name__,
            )
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

        The ``MODEL_GATEWAY_SLOTS`` limit is global across API/worker processes because the slots are
        Redis keys. When Redis cannot serve the lease (a non-connection Redis error, or connection errors
        persisting past ``_REDIS_PROBE_SECONDS``) the call degrades to a per-process semaphore of the same
        size with a warning: the limit is then per process (up to processes x slots overall) until Redis
        returns, instead of failing every model call. The timeout bounds lease acquisition only (Redis
        probe plus local wait share ``wait_seconds``); the body runs outside it.
        """
        token = secrets.token_urlsafe(18)
        keys = [f"{_LEASE_PREFIX}{slot}" for slot in range(_SLOTS)]
        key = None
        attempted = False
        degraded = False
        refresher: asyncio.Task[None] | None = None
        started = time.monotonic()
        try:
            try:
                async with asyncio.timeout(wait_seconds):
                    probe_deadline = started + _REDIS_PROBE_SECONDS
                    while key is None:
                        attempted = True
                        try:
                            slot = int(await cast("Awaitable[Any]", self.redis.eval(
                                _ACQUIRE, len(keys), *keys, token, str(_LEASE_TTL_SECONDS * 1000))))
                        except (RedisConnectionError, RedisTimeoutError):
                            if time.monotonic() >= probe_deadline:
                                degraded = True
                                break
                            slot = -1  # transient (e.g. pool exhausted): retry until the probe/lease deadline
                        except RedisError:
                            degraded = True
                            break
                        if slot >= 0:
                            key = keys[slot]
                        else:
                            await asyncio.sleep(0.1 + random.uniform(0, 0.05))
            except TimeoutError as exc:
                raise ModelGatewayError("Model capacity is unavailable") from exc
            if degraded:
                _LOG.warning("Model gateway Redis unavailable; limiting slots per process")
                remaining = max(wait_seconds - (time.monotonic() - started), 0.05)
                async with self._local_slot(remaining):
                    yield
                return
            refresher = asyncio.create_task(self._keep_lease(cast("str", key), token))
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

    @asynccontextmanager
    async def _local_slot(self, wait_seconds: float) -> AsyncIterator[None]:
        """Hold one slot of the per-process fallback semaphore (degraded mode); raise when none frees in time."""
        semaphore = _local_semaphore()
        try:
            async with asyncio.timeout(wait_seconds):
                await semaphore.acquire()
        except TimeoutError as exc:
            raise ModelGatewayError("Model capacity is unavailable") from exc
        try:
            yield
        finally:
            semaphore.release()

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
        record_breaker_open(self.protocol.display_name, remaining / 1000)  # bounded label: protocol display name
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
            return
        await self._record_breaker_state(open_key)

    async def _record_breaker_state(self, open_key: str) -> None:
        """Export the open window to metrics right after a failure may have tripped the breaker.

        Without this the gauge only moved on rejected calls, so a freshly opened breaker with no traffic
        stayed invisible. Best effort: Redis errors and non-integer replies (test doubles) are ignored.
        """
        try:
            pttl = await self.redis.pttl(open_key)
        except (RedisError, AttributeError):
            return
        if isinstance(pttl, int) and not isinstance(pttl, bool) and pttl > 0:
            record_breaker_open(self.protocol.display_name, pttl / 1000, rejected=False)

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

    def _wire_session(self, http: httpx.AsyncClient, client: AsyncOpenAI, base_url: str) -> WireSession:
        """Bundle the per-call transport handles a ``WireProtocol`` needs (pinned http client, SDK client, credentials)."""
        return WireSession(http=http, client=client, base_url=base_url, api_key=self.api_key, timeout=self._timeout())

    def _raise_status_error(self, exc: APIStatusError, rejected: str) -> NoReturn:
        """Map a terminal provider HTTP error to the gateway's exception types; never returns.

        A ``max_tokens`` 400 from a protocol that requires it is a parameter problem (``ModelGatewayError``);
        otherwise 400/404/405/422 mean the capability (``rejected``: "this capability" / "streaming") is
        unsupported, and everything else is a plain gateway error.
        """
        if self._rejects_max_tokens(exc):
            raise ModelGatewayError(f"Model gateway error ({_upstream_reason(exc)})") from exc
        if exc.status_code in {400, 404, 405, 422}:
            raise CapabilityUnsupported(f"Gateway rejected {rejected} ({_upstream_reason(exc)})") from exc
        raise ModelGatewayError(f"Model gateway error ({_upstream_reason(exc)})") from exc

    async def _check_request(self, role: str, mapping: ModelMapping | None, policy: RequestPolicy, capability: str,
                             path: str, *, probe: bool, before_send: Callable[[], Awaitable[None]] | None) -> ModelMapping:
        """Run the pre-I/O fences of one request and return the non-None mapping.

        Order matters: per-call fence presence, privacy decision, configuration, protocol path support,
        stored ``unsupported`` tools evidence, then the breaker (skipped for probes so a tripped upstream can be
        re-verified). Raises ValueError/PrivacyPolicyDenied/ModelGatewayError/CapabilityUnsupported/Unavailable.
        """
        if not probe and before_send is None:
            raise ValueError("before_send_required")  # fail closed: every non-probe call needs a per-call fence
        if not self._policy_matches(policy) or not may_send(policy, role, mapping, self.destination_id, bool(self.api_key), capability):
            raise PrivacyPolicyDenied("Model request denied by privacy policy")
        if self.base_url is None or mapping is None:
            raise ModelGatewayError("Model gateway is not configured")
        if not self.protocol.supports_path(path):
            # e.g. Anthropic has no embeddings, rerank or decisions API: report unsupported before any I/O.
            raise CapabilityUnsupported(f"{self.protocol.display_name} does not offer {capability}")
        if not probe and capability == "tools" and await self._known_unsupported(role, mapping, "tools"):
            raise CapabilityUnsupported("Tool calling is recorded as unsupported for this model")
        if not probe:
            # Probes must be able to reach a tripped upstream, otherwise it could never be re-verified.
            await self._breaker_check()
        return mapping

    async def _attempt_once(self, session: WireSession, path: str, body: dict[str, Any],
                            after_send: Callable[[], Awaitable[None]] | None) -> Any:
        """Execute one wire call, wiring the body-sent hook and always running the idempotent ``after_send`` fallback.

        Gated (P2-3): with no ``after_send`` the ContextVar is never touched, so the transport sends
        ``request.stream`` unwrapped. Raises ModelGatewayError for an operation the protocol lacks.
        """
        hook = body_sent.set(after_send) if after_send is not None else None
        try:
            return await self.protocol.create(session, path, body)
        except UnsupportedWireOperation:
            raise ModelGatewayError("Unsupported model gateway operation") from None
        finally:
            if hook is not None:
                body_sent.reset(hook)
            # Fallback release (connect/write failure, timeout); after_send is idempotent.
            if after_send is not None:
                await after_send()

    @staticmethod
    def _normalize_response(response: Any) -> Any:
        """Return a plain dict for an SDK model or dict reply; anything else is an invalid gateway response."""
        if hasattr(response, "model_dump"):
            return response.model_dump(mode="json", exclude_none=True)
        if isinstance(response, dict):
            return response
        raise ModelGatewayError("Model gateway returned an invalid response")

    async def _send_with_retries(
        self, role: str, mapping: ModelMapping, policy: RequestPolicy, capability: str, path: str,
        body: dict[str, Any], base_url: str,
        before_send: Callable[[], Awaitable[None]] | None, after_send: Callable[[], Awaitable[None]] | None,
    ) -> Any:
        """Execute a chat, embedding, or rerank call with at most two attempts and normalize its response.

        Each attempt re-runs the gateway-owned ``before_send`` plus the caller's fence and re-checks policy.
        A ``max_tokens`` 400 clamps once (mutating ``body``); transient statuses retry once via the shared
        breaker logic. The protocol-specific wire call is delegated to ``self.protocol``.
        """
        http = self._http_client(base_url)
        async with AsyncOpenAI(
            base_url=base_url,
            api_key=self.api_key or "not-configured",
            timeout=self._timeout(),
            max_retries=0,
            http_client=http,
        ) as client:
            session = self._wire_session(http, client, base_url)
            for attempt in range(2):
                try:
                    # Keep the gateway-owned settings check even when a scoped
                    # operation adds its own source/evidence freshness fence.
                    await self.before_send()
                    if before_send is not None and before_send is not self.before_send:
                        await before_send()
                    if not self._policy_matches(policy) or not may_send(policy, role, mapping, self.destination_id, bool(self.api_key), capability):
                        raise PrivacyPolicyDenied("Model request denied by privacy policy")
                    response = await self._attempt_once(session, path, body, after_send)
                except (APITimeoutError, APIConnectionError) as exc:
                    _raise_if_policy_denied(exc)  # a policy denial is not an upstream fault: no retry, no count
                    if attempt == 0:
                        await asyncio.sleep(_retry_delay())
                        continue
                    await self._breaker_fail()
                    raise ModelGatewayError("Model gateway request failed") from exc
                except APIStatusError as exc:
                    if attempt == 0 and (lowered := await self._clamp_on_max_tokens_error(
                            exc, mapping, body.get("max_tokens"), stream=False)) is not None:
                        body["max_tokens"] = lowered
                        continue
                    if _retryable_status(exc.status_code) and await self._on_retryable_status(exc, attempt):
                        continue
                    self._raise_status_error(exc, "this capability")
                return self._normalize_response(response)
        raise ModelGatewayError("Model gateway request failed")

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
        *,
        record_as: str | None = None,
        record_failure: bool = True,
    ) -> Any:
        """Check policy, then send a bounded, retried gateway request; map transport and provider errors.

        Privacy is always decided for ``capability`` before any I/O. There is no "verified" gate: missing evidence
        never blocks, real provider errors propagate with their redacted message. Only a stored ``unsupported``
        for ``tools`` short-circuits (the caller has a text-only fallback). The outcome is recorded as evidence
        under ``record_as`` (default ``capability``); ``record_failure=False`` leaves failures to the caller.
        A protocol without the requested path (Anthropic: only ``chat/completions``) raises CapabilityUnsupported
        before I/O. Protocol errors are mapped to the same ``openai`` exception types, so retry handling is shared.
        """
        mapping = await self._check_request(role, mapping, policy, capability, path, probe=probe, before_send=before_send)
        body = {**payload, "model": mapping.model}
        if path == CHAT_PATH and body.get("max_tokens") is None:
            body.pop("max_tokens", None)
            if (limit := await self._initial_max_tokens(mapping, None)) is not None:
                body["max_tokens"] = limit
        base_url = _api_base(cast("str", self.base_url))  # non-None: guaranteed by _check_request

        async def send() -> Any:
            """Run the retried wire call for ``_with_slot`` (lease and total deadline are applied by the caller)."""
            return await self._send_with_retries(
                role, mapping, policy, capability, path, body, base_url, before_send, after_send)

        # Telemetry only: labels are capability and configured role; model name is identity, never a label.
        started = time.perf_counter()
        try:
            response = await self._with_slot(send)
        except _GatewayTimeout as exc:
            await self._breaker_fail()
            record_model_call(capability, role, started, False)
            if record_failure and not probe:
                await self._record_failure(role, mapping, record_as or capability, exc)
            raise
        except BaseException as exc:
            record_model_call(capability, role, started, False)
            if record_failure and not probe:
                await self._record_failure(role, mapping, record_as or capability, exc, rejected_tools=capability == "tools")
            raise
        await self._breaker_ok()
        if not probe and path != "decisions":
            await self._record(role, mapping, record_as or capability, "supported")
        record_model_call(capability, role, started, True, response, mapping.model)
        return response

    async def discover_models(self) -> list[str]:
        """List model IDs from the configured gateway while holding capacity and enforcing endpoint policy.

        The wire protocol decides the listing call (Anthropic: native ``GET /models`` with ``x-api-key``); errors
        carry static reasons only.
        """
        if self.base_url is None or not (self.api_key or self.destination_id.startswith("local:")):
            raise ModelGatewayError("Model gateway is not configured")
        base_url = _api_base(self.base_url)

        async def send() -> list[str]:
            """Call the configured model-list endpoint and return valid model IDs; map SDK and network failures to the gateway error."""
            await self.before_send()
            http = self._http_client(base_url)
            async with AsyncOpenAI(base_url=base_url, api_key=self.api_key,
                                   timeout=self._timeout(), max_retries=0,
                                   http_client=http) as client:
                try:
                    return await self.protocol.list_models(self._wire_session(http, client, base_url))
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

        models = await self._with_slot(send)
        # Discovery skips the open check (it is how a tripped upstream is re-verified) but success closes it.
        await self._breaker_ok()
        return models

    async def list_openrouter_models(self, params: dict[str, str]) -> dict[str, Any]:
        """Fetch the raw OpenRouter ``/models`` listing for ``params`` under capacity and endpoint policy.

        ``params`` are pre-validated, allowlisted query parameters. The caller validates and bounds the payload.
        Errors carry a static reason only, never a URL, body or key.
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
                                            options={"params": params})
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

    async def discover_decision_models(self) -> dict[str, Any]:
        """Fetch the raw OpenRouter ``/models?output_modalities=decisions`` listing (see ``list_openrouter_models``)."""
        return await self.list_openrouter_models({"output_modalities": "decisions"})

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
        ``tools`` raises CapabilityUnsupported when stored evidence says the model rejects tools, or when the provider
        rejects them (recorded as unsupported). Real outcomes are recorded as capability evidence (first chunk =
        supported; provider error = failed); recording never fails the stream.
        Reasoning is surfaced as ``delta.reasoning_content`` (provider ``reasoning``/``reasoning_content``
        and leading inline ``<think>`` blocks); ``delta.content`` carries answer text only.
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
                        if not probe:
                            await self._record(role, mapping, "streaming", "supported")
                            if tools:
                                await self._record(role, mapping, "tools", "supported")
                    if '"usage"' in line and usage is None:
                        try:
                            usage = json.loads(line[6:]).get("usage")
                        except (ValueError, AttributeError):
                            usage = None
                    yield line
            ok = True
        except ModelGatewayError as exc:
            if not probe:
                await self._record_failure(role, mapping, "streaming", exc, rejected_tools=bool(tools))
            raise
        finally:
            record_model_call("streaming", role, started, ok, {"usage": usage} if usage else None,
                              mapping.model if mapping else None, first_ms)

    def _stream_allowed(self, policy: RequestPolicy, role: str, mapping: ModelMapping | None,
                        capabilities: tuple[str, ...]) -> bool:
        """True when the principal/config identity matches and privacy policy allows every streamed capability."""
        return self._policy_matches(policy) and all(
            may_send(policy, role, mapping, self.destination_id, bool(self.api_key), c) for c in capabilities)

    async def _stream_preflight(
        self, role: str, mapping: ModelMapping | None, policy: RequestPolicy, probe: bool,
        tools: list[dict[str, Any]] | None, tool_choice: Any, max_tokens: int | None, capabilities: tuple[str, ...],
    ) -> tuple[ModelMapping, dict[str, Any]]:
        """Run the pre-lease fences of a stream and build the protocol-neutral request extras.

        Order: privacy/config denial, stored ``unsupported`` tools evidence, extras (tools, tool_choice, resolved
        ``max_tokens``, owner-chosen ``reasoning_effort`` for the chat role), then the breaker (skipped for probes).
        Raises PrivacyPolicyDenied/CapabilityUnsupported/ModelGatewayUnavailable before any provider I/O.
        """
        if not self._stream_allowed(policy, role, mapping, capabilities) or self.base_url is None or mapping is None:
            raise PrivacyPolicyDenied("Model request denied by privacy policy")
        if tools and not probe and await self._known_unsupported(role, mapping, "tools"):
            raise CapabilityUnsupported("Tool calling is recorded as unsupported for this model")
        extra: dict[str, Any] = {}
        if tools:
            extra["tools"] = tools
        if tool_choice is not None:
            extra["tool_choice"] = tool_choice
        if (limit := await self._initial_max_tokens(mapping, max_tokens)) is not None:
            extra["max_tokens"] = limit
        # Owner-chosen reasoning effort (chat role only): request-gated models stream reasoning only when asked.
        if role == "chat" and not probe and mapping.reasoning_effort != "off":
            extra["reasoning_effort"] = mapping.reasoning_effort
        if not probe:
            await self._breaker_check()
        return mapping, extra

    async def _open_stream(self, session: WireSession, mapping: ModelMapping, messages: list[dict[str, Any]],
                           extra: dict[str, Any], after_send: Callable[[], Awaitable[None]] | None) -> Any:
        """Open one provider stream bounded by the header/first-byte timeout.

        I6/T7: the transport releases the send fence once the body is handed to ``transport.write``;
        publication is re-fenced per flush (T3). Gated (P2-3). Set/reset stay free of any ``yield`` (P3-2):
        reset fails across a context switch. ``after_send`` (idempotent) always runs as the fallback release.
        """
        hook = body_sent.set(after_send) if after_send is not None else None
        try:
            async with asyncio.timeout(self.timeout_seconds):
                return await self.protocol.open_stream(session, mapping.model, messages, extra)
        finally:
            if hook is not None:
                body_sent.reset(hook)
            if after_send is not None:
                await after_send()

    @staticmethod
    def _chunk_line(chunk: Any, splitters: dict[int, _ThinkSplitter]) -> str:
        """Encode one provider chunk (dict or SDK model) as an SSE ``data:`` line with reasoning split out."""
        payload = chunk if isinstance(chunk, dict) else chunk.model_dump(mode="json", exclude_none=True)
        _split_reasoning(payload, splitters)
        return f"data: {json.dumps(payload)}"

    @staticmethod
    def _flush_lines(splitters: dict[int, _ThinkSplitter]) -> list[str]:
        """SSE lines for text still held back by the think splitters at end of stream."""
        lines: list[str] = []
        for index, splitter in splitters.items():
            held_think, held_answer = splitter.flush()
            if held_think or held_answer:
                tail = {"reasoning_content": held_think} if held_think else {"content": held_answer}
                lines.append(f"data: {json.dumps({'choices': [{'index': index, 'delta': tail}]})}")
        return lines

    async def _on_stream_transport_error(self, exc: BaseException, state: _StreamState) -> None:
        """Handle a timeout/connection failure: sleep and return to retry once, else raise ModelGatewayError.

        A policy denial is raised without retry or breaker count. Mid-stream failures say little about
        upstream health, so only pre-first-token ones are counted toward the breaker.
        """
        _raise_if_policy_denied(exc)
        if state.attempt == 1 or state.emitted:
            if not state.emitted:
                await self._breaker_fail()
            raise ModelGatewayError("Model gateway stream failed") from exc
        await asyncio.sleep(_retry_delay())
        state.attempt += 1

    async def _on_stream_status_error(self, exc: APIStatusError, mapping: ModelMapping, state: _StreamState) -> None:
        """Handle a provider HTTP error: return to retry the open, or raise the mapped gateway error.

        Retries happen only before the first chunk. Dropping a rejected ``reasoning_effort`` shares the single
        transient retry budget (so a 5xx after it fails); lowering a rejected ``max_tokens`` (protocols that
        require it) also consumes it, except that it may follow an effort drop (at most three opens in total).
        A status error after chunks were emitted is never retried or counted.
        """
        extra = state.extra
        if (not state.emitted and state.attempt == 0 and "reasoning_effort" in extra and exc.status_code in {400, 422}
                and any(w in str(exc.message or "").lower() for w in ("reasoning", "thinking", "effort"))):
            # A wrong effort setting must never break chat: retry once without the parameter.
            _LOG.warning("Provider rejected reasoning_effort (%s); retrying without it", _upstream_reason(exc))
            del extra["reasoning_effort"]
            state.attempt += 1  # consumes the single retry, as before the refactor
            state.effort_dropped = True
            return
        # The clamp shares the single transient budget (attempt 0), except right after an effort drop (attempt 1).
        if (not state.emitted and not state.clamped and (state.attempt == 0 or (state.effort_dropped and state.attempt == 1))
                and (lowered := await self._clamp_on_max_tokens_error(
                exc, mapping, extra.get("max_tokens"), stream=True)) is not None):
            extra["max_tokens"] = lowered
            state.clamped = True
            state.attempt = 1  # the clamp consumes the transient retry, so clamp -> 503 fails after two opens
            return
        if not state.emitted and _retryable_status(exc.status_code) and await self._on_retryable_status(exc, state.attempt):
            state.attempt += 1
            return
        self._raise_status_error(exc, "streaming")

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
        pass ``may_send``; stored ``unsupported`` tools evidence raises CapabilityUnsupported before any I/O.
        Missing streaming/tools evidence never blocks.
        ``tool_choice`` and ``max_tokens`` are forwarded only when not None. A configured ``reasoning_effort``
        (chat role) is sent as the OpenAI-compatible ``reasoning_effort`` parameter; if the provider answers
        400/422 naming it (or ``thinking``/``effort``, as Anthropic does) before any chunk, it is dropped once with
        a warning and the request is retried. A ``max_tokens`` 400 (protocols that require it) is clamped once
        (sharing the transient retry, except directly after the effort drop). The wire format (OpenAI-compatible or native Anthropic Messages SSE) is
        delegated to ``self.protocol``; chunks always leave as OpenAI-shaped SSE lines.
        """
        capabilities = ("streaming", "tools") if tools else ("streaming",)
        mapping, extra = await self._stream_preflight(
            role, mapping, policy, probe, tools, tool_choice, max_tokens, capabilities)
        state = _StreamState(extra)
        async with self._lease(_STREAM_LEASE_WAIT_SECONDS):
            base_url = _api_base(cast("str", self.base_url))  # non-None: guaranteed by _stream_preflight
            http = self._http_client(base_url)
            async with AsyncOpenAI(
                base_url=base_url,
                api_key=self.api_key or "not-configured",
                timeout=self._timeout(),
                max_retries=0,
                http_client=http,
            ) as client:
                session = self._wire_session(http, client, base_url)
                splitters: dict[int, _ThinkSplitter] = {}
                while True:  # bounded: at most 3 opens (effort drop, then clamp, then success; otherwise one retry)
                    try:
                        await self.before_send()
                        if before_send is not None and before_send is not self.before_send:
                            await before_send()
                        if not self._stream_allowed(policy, role, mapping, capabilities):
                            raise PrivacyPolicyDenied("Model request denied by privacy policy")
                        stream = await self._open_stream(session, mapping, messages, state.extra, after_send)
                        async for chunk in stream:
                            state.emitted = True
                            yield self._chunk_line(chunk, splitters)
                        for line in self._flush_lines(splitters):
                            yield line
                        yield "data: [DONE]"
                        await self._breaker_ok()
                        return
                    except (APITimeoutError, APIConnectionError, TimeoutError) as exc:
                        await self._on_stream_transport_error(exc, state)
                    except RedisError as exc:
                        raise ModelGatewayError("Model gateway request failed") from exc
                    except APIStatusError as exc:
                        await self._on_stream_status_error(exc, mapping, state)

    async def embed(
        self, role: str, mapping: ModelMapping | None, policy: RequestPolicy,
        inputs: list[str], probe: bool = False, *,
        before_send: Callable[[], Awaitable[None]] | None = None,
    ) -> Any:
        """Request embeddings through gateway policy with a fresh per-attempt authorization check.

        Always returns the OpenAI shape ``{"data": [{"embedding": [...], "index": n}]}``. Cohere models
        (id contains ``cohere``) are sent the native v2 body (``texts``/``input_type``/``embedding_types``)
        and their ``embeddings.float`` reply is converted to that shape.
        """
        if mapping is None or "cohere" not in mapping.model.lower():
            return await self._request(
                role, mapping, policy, "embeddings", "embeddings", {"input": inputs}, probe, before_send
            )
        response = await self._request(
            role, mapping, policy, "embeddings", "embeddings-native",
            {"texts": inputs, "input_type": "search_document", "embedding_types": ["float"]}, probe, before_send,
        )
        vectors = response.get("embeddings") if isinstance(response, dict) else None
        if isinstance(vectors, dict):
            vectors = vectors.get("float")
        if isinstance(vectors, list):
            return {**response, "data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]}
        return response

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
        if probe or mapping is None:
            return await self._request(role, mapping, policy, "structured", "chat/completions", payload, probe, before_send)
        if await self._known_unsupported(role, mapping, "structured"):
            return await self._structured_prompted(role, mapping, policy, payload, messages, schema, before_send)
        try:
            return await self._request(
                role, mapping, policy, "structured", "chat/completions", payload, probe, before_send, record_failure=False)
        except CapabilityUnsupported:
            # Native json_schema was rejected: ask for JSON in the prompt over a plain chat completion and extract it
            # app-side. A failure here propagates with its own real message; success proves the model is chat-capable
            # and only native structured output is unsupported.
            response = await self._structured_prompted(role, mapping, policy, payload, messages, schema, before_send)
            await self._record(role, mapping, "structured", "unsupported")
            return response

    async def _structured_prompted(
        self, role: str, mapping: ModelMapping, policy: RequestPolicy, payload: dict[str, Any],
        messages: list[dict[str, Any]], schema: dict[str, Any], before_send: Callable[[], Awaitable[None]] | None,
    ) -> Any:
        """Send the prompted-JSON variant of a structured request (no ``response_format``) and extract the JSON.

        Privacy is still decided for ``structured``; callers still ``json.loads`` + schema-validate the content, so
        nothing loosens. Success is recorded as ``chat`` support, failures as ``failed`` chat evidence.
        """
        fallback = {key: value for key, value in payload.items() if key != "response_format"}
        fallback["messages"] = [_json_instruction(schema), *messages]
        response = await self._request(
            role, mapping, policy, "structured", "chat/completions", fallback, False, before_send, record_as="chat")
        return _with_extracted_json(response)

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
