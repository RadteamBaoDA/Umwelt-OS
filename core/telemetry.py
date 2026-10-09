"""Trace context, bounded in-process metrics, usage records and log/telemetry redaction.

Design rules (Phase 11 T1):
* Instrumentation observes; it never changes the outcome of a request or job. Every public
  recording helper swallows its own failures (``_isolated``) so a telemetry fault cannot fail a
  knowledge write, tool call or agent result.
* Metrics are process-local, bounded and content-free. Processes publish a JSON snapshot to Redis
  (short TTL) so the API can show worker numbers; Redis loss only hides numbers.
* Labels are enumerated or route templates, never raw paths, IDs, prompts or document text.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, TypeVar

from pydantic import BaseModel, Field

logger = logging.getLogger("bbd.telemetry")

# --------------------------------------------------------------------------- trace context


class TraceContext(BaseModel):
    """Correlation IDs carried through one request or job via contextvars.

    ``request_id`` is the API request ID; the run IDs are the stable durable identifiers of the
    ingestion, agent and tool-call records. Unset fields stay ``None``.
    """

    request_id: str | None = None
    ingestion_run_id: str | None = None
    agent_run_id: str | None = None
    tool_call_id: str | None = None


_trace: contextvars.ContextVar[TraceContext] = contextvars.ContextVar("trace_context", default=TraceContext())  # noqa: B039  # TraceContext is an immutable dataclass


def current_trace() -> TraceContext:
    """Return the trace context bound to the current task (empty when none)."""
    return _trace.get()


@contextmanager
def bind_trace(**ids: str | None) -> Iterator[TraceContext]:
    """Merge non-null IDs into the current trace context for the duration of the block."""
    merged = current_trace().model_copy(update={k: str(v) for k, v in ids.items() if v is not None})
    token = _trace.set(merged)
    try:
        yield merged
    finally:
        _trace.reset(token)


def set_trace(**ids: str | None) -> None:
    """Merge non-null IDs into the current context without a scope (the job/request wrapper resets it)."""
    _trace.set(current_trace().model_copy(update={k: str(v) for k, v in ids.items() if v is not None}))


# --------------------------------------------------------------------------- redaction

# Redaction rules, applied to every log line and to any mapping handed to ``redact_mapping``:
#  1. Authorization / cookie / API-key style headers and fields: the whole value is replaced.
#  2. ``Bearer <token>`` / ``Basic <token>`` credentials anywhere in text.
#  3. URL userinfo (``scheme://user:pass@host``) keeps scheme and host only.
#  4. Query parameters named like secrets (token, key, secret, password, signature, code...) and ``q``/``query`` search text.
#  5. Provider key shapes (sk-..., ghp_/gho_/github_pat_..., xox?-..., AIza..., JWT triples).
#  6. Fields named prompt/messages/content/document/text/body/input/output are replaced by a
#     length marker (raw prompts and documents are never logged by default).
_MASK = "[redacted]"
_SECRET_KEY = re.compile(
    r"(authorization|cookie|set-cookie|api[-_]?key|secret|password|passwd|token|credential|"
    r"csrf|session|signature|private[-_]?key)", re.IGNORECASE)
_CONTENT_KEY = re.compile(r"^(prompt|messages|content|document|documents|text|body|input|output|completion|query)$", re.IGNORECASE)
_TEXT_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + _MASK),
    (re.compile(r"(?i)\b(authorization|set-cookie|cookie|x-api-key|x-csrf-token)\s*[:=]\s*[^\r\n]+"), r"\1: " + _MASK),
    (re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/\s:@]+(?::[^/\s@]*)?@"), r"\1" + _MASK + "@"),
    (re.compile(r"(?i)([?&](?:[a-z_]*(?:token|key|secret|password|signature|code|sig)[a-z_]*)=)[^&\s#]+"), r"\1" + _MASK),
    (re.compile(r"(?i)([?&](?:q|query)=)[^&\s#]+"), r"\1" + _MASK),
    (re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{16,}"), _MASK),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"), _MASK),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), _MASK),
    (re.compile(r"\bAIza[A-Za-z0-9_-]{30,}"), _MASK),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), _MASK),
)
_MAX_DEPTH = 6
_REDIS_TIMEOUT_SECONDS = 0.5
_SNAPSHOT_SCAN_LIMIT = 64


def redact_text(value: str) -> str:
    """Return ``value`` with credentials, credential URLs and provider keys masked."""
    for pattern, replacement in _TEXT_RULES:
        value = pattern.sub(replacement, value)
    return value


def redact_mapping(value: Any, _depth: int = 0) -> Any:
    """Return a deep copy safe to log: secret-named keys masked, content-named keys reduced to a length."""
    if _depth > _MAX_DEPTH:
        return _MASK
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if _SECRET_KEY.search(name):
                out[name] = _MASK
            elif _CONTENT_KEY.match(name):
                out[name] = f"[{len(item) if hasattr(item, '__len__') else 0} omitted]"
            else:
                out[name] = redact_mapping(item, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_mapping(item, _depth + 1) for item in value[:50]]
    if isinstance(value, str):
        return redact_text(value)
    return value


# Exact-type Decimal passes un-frozen so %d/%.2f keep working; its str/repr hold a digit coefficient only
# (repr is "Decimal('...')"). Matching by exact type keeps a subclass with a custom __str__ out.
_EXACT_NUMERIC_TYPES = (Decimal,)


def _freeze_arg(item: Any) -> Any:
    """Freeze one log arg by value so a handler's later __str__/__repr__ cannot emit unchecked text."""
    if type(item) in (int, float, bool, type(None), *_EXACT_NUMERIC_TYPES):
        return item
    if isinstance(item, int):  # int subclass (IntEnum...): drop its own __str__, keep %d/%s rendering
        return int.__int__(item)
    if isinstance(item, float):
        return float.__float__(item)
    return redact_text(item) if isinstance(item, str) else redact_text(str(redact_mapping(item)))


def _stable_record(record: logging.LogRecord, redacted: str) -> tuple[str, Any]:
    """Return (msg, args) whose rendering holds no secret yet keeps the positional shape formatters unpack.

    uvicorn's AccessFormatter needs the 5-tuple, so mask every str arg at once: masking one at a time can
    hide the context (e.g. ``cookie=``) that makes a neighbouring arg a secret. Fall back to the fully
    collapsed string with no args only when even that rendering is not redaction-stable.
    """
    args = record.args
    if isinstance(record.msg, str) and isinstance(args, tuple):
        trial = tuple(_MASK if isinstance(item, str) else item for item in args)
        try:
            text = record.msg % trial
        except (TypeError, ValueError):
            return redacted, None
        if redact_text(text) == text:
            return record.msg, trial
    return redacted, None


def install_log_redaction() -> None:
    """Scrub messages, extra fields, exception text and trace IDs before handlers see records."""
    if getattr(logging.Logger.makeRecord, "_bbd_redacting", False):
        return

    make_record = logging.Logger.makeRecord
    standard = logging.makeLogRecord({}).__dict__

    def safe_make_record(self: logging.Logger, *args: Any, **kwargs: Any) -> logging.LogRecord:
        """Scrub after ``extra`` has been merged, before the record can reach a handler."""
        record = make_record(self, *args, **kwargs)
        try:
            if isinstance(record.msg, Mapping):
                record.msg = redact_mapping(record.msg)
            if isinstance(record.args, Mapping):
                record.args = {k: _freeze_arg(v) for k, v in redact_mapping(record.args).items()}
            elif isinstance(record.args, tuple):
                # Redact by value: freeze non-primitives to their redacted str so a later __str__/__repr__
                # call by a handler can never emit text that was not checked here.
                record.args = tuple(_freeze_arg(item) for item in record.args)
            message = record.getMessage()
            redacted = redact_text(message)
            if redacted != message or not isinstance(record.msg, str):
                record.msg, record.args = _stable_record(record, redacted)
        except Exception:  # noqa: BLE001 - formatting faults must not fail the caller
            record.msg, record.args = "[unformattable log message]", None
        # Keep each field name attached so secret/content key rules apply at the record boundary.
        extras = {key: record.__dict__[key] for key in record.__dict__.keys() - standard.keys()}
        record.__dict__.update(redact_mapping(extras))
        for key, value in current_trace().model_dump().items():
            if value is not None:
                setattr(record, key, redact_mapping(value))
        if record.exc_info:
            try:
                record.exc_text = redact_text(logging.Formatter().formatException(record.exc_info))
            except Exception:  # noqa: BLE001 - never let traceback formatting break logging
                record.exc_text = "[exception omitted]"
            record.exc_info = None
        return record

    safe_make_record._bbd_redacting = True  # type: ignore[attr-defined]
    logging.Logger.makeRecord = safe_make_record  # type: ignore[method-assign]  # deliberate process-wide log redaction hook


def log_event(log: logging.Logger, level: int, event: str, **fields: Any) -> None:
    """Emit one structured ``event k=v`` line with trace IDs; fields are redacted and failures isolated.

    Callers must not use this for no-op polls: a poll that changed nothing emits nothing.
    """
    def emit() -> None:
        """Format and emit the line."""
        trace = {k: v for k, v in current_trace().model_dump().items() if v}
        payload = redact_mapping({**trace, **fields})
        log.log(level, "%s %s", event, json.dumps(payload, default=str, sort_keys=True))

    _isolated(emit)


# --------------------------------------------------------------------------- run metadata

class RunMeta(BaseModel):
    """Metadata-only run summary returned by each owning module's public ``list_run_meta`` (no content)."""

    kind: str
    id: str
    status: str
    error_code: str | None = None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None
    origin_run_id: str | None = None
    model_identity: str | None = None
    token_usage: Any = None  # raw provider usage mapping, or an int total (agent); None when unknown


# --------------------------------------------------------------------------- usage

class PriceRate(BaseModel):
    """Owner-configured per-1k-token rate; the estimate it yields is never authoritative billing."""

    model_identity: str
    input_per_1k: float = Field(ge=0)
    output_per_1k: float = Field(ge=0)
    rate_as_of: datetime


class Usage(BaseModel):
    """Usage of one model call; every unknown value stays ``None`` (never coerced to 0)."""

    model_identity: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    estimated_cost: float | None = None
    # An estimate always names the model and rate timestamp it used; it is not billing data.
    estimate_model: str | None = None
    estimate_rate_as_of: datetime | None = None
    latency_ms: float | None = None


def usage_from_response(response: Any, *, model_identity: str | None, latency_ms: float | None = None,
                        rate: PriceRate | None = None) -> Usage:
    """Build a ``Usage`` from a provider response dict; missing/invalid token fields remain null."""
    raw = response.get("usage") if isinstance(response, Mapping) else None
    raw = raw if isinstance(raw, Mapping) else {}

    def count(*names: str) -> int | None:
        """Return the first non-negative integer among ``names`` or None."""
        for name in names:
            item = raw.get(name)
            if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
                return item
        return None

    usage = Usage(model_identity=model_identity, tokens_in=count("prompt_tokens", "input_tokens"),
                  tokens_out=count("completion_tokens", "output_tokens"), latency_ms=latency_ms)
    if rate is not None and rate.model_identity == model_identity and usage.tokens_in is not None \
            and usage.tokens_out is not None:
        usage.estimated_cost = (usage.tokens_in * rate.input_per_1k + usage.tokens_out * rate.output_per_1k) / 1000
        usage.estimate_model, usage.estimate_rate_as_of = rate.model_identity, rate.rate_as_of
    return usage


# --------------------------------------------------------------------------- metrics

# Cardinality bounds: at most MAX_SERIES label-sets per metric name per process; overflow folds
# into a single {"overflow": "true"} series. Label values are truncated to 64 characters. Callers
# pass route templates, ARQ function names, enumerated outcomes and model aliases only.
MAX_SERIES = 200
MAX_LABEL_LEN = 64
BUCKETS_MS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 15000, 60000)
SNAPSHOT_TTL_SECONDS = 180
SNAPSHOT_KEY_PREFIX = "telemetry:metrics:"
_FLUSH_INTERVAL = 10.0

_Key = tuple[str, tuple[tuple[str, str], ...]]


class MetricsRegistry:
    """Bounded counters and fixed-bucket histograms; single-threaded asyncio use, no locks needed."""

    def __init__(self) -> None:
        """Create an empty registry."""
        self.counters: dict[_Key, float] = {}
        self.histograms: dict[_Key, list[float]] = {}  # [count, sum, *bucket counts]
        self._series: dict[str, int] = {}
        self.last_flush = 0.0

    def _key(self, name: str, labels: Mapping[str, str], store: dict[_Key, Any]) -> _Key:
        """Normalise labels and fold into the overflow series once the per-metric bound is hit."""
        key: _Key = (name, tuple(sorted((str(k)[:MAX_LABEL_LEN], str(v)[:MAX_LABEL_LEN]) for k, v in labels.items())))
        if key in store:
            return key
        if self._series.get(name, 0) >= MAX_SERIES:
            return (name, (("overflow", "true"),))
        self._series[name] = self._series.get(name, 0) + 1
        return key

    def inc(self, name: str, value: float = 1, /, **labels: str) -> None:
        """Add ``value`` to a counter."""
        key = self._key(name, labels, self.counters)
        self.counters[key] = self.counters.get(key, 0) + value

    def observe(self, name: str, value_ms: float, /, **labels: str) -> None:
        """Record a millisecond observation in a fixed-bucket histogram."""
        key = self._key(name, labels, self.histograms)
        row = self.histograms.setdefault(key, [0.0] * (2 + len(BUCKETS_MS) + 1))
        row[0] += 1
        row[1] += value_ms
        row[2 + next((i for i, b in enumerate(BUCKETS_MS) if value_ms <= b), len(BUCKETS_MS))] += 1

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe copy: counters and histograms as lists of {name, labels, ...} rows."""
        return {
            "buckets_ms": list(BUCKETS_MS),
            "counters": [{"name": n, "labels": dict(lb), "value": v} for (n, lb), v in self.counters.items()],
            "histograms": [{"name": n, "labels": dict(lb), "count": int(r[0]), "sum_ms": round(r[1], 3),
                            "buckets": [int(x) for x in r[2:]]} for (n, lb), r in self.histograms.items()],
        }


registry = MetricsRegistry()
_PROCESS_ROLE = "api"


def set_process_role(role: str) -> None:
    """Name this process (``api`` or ``worker``) for the published snapshot key."""
    global _PROCESS_ROLE
    _PROCESS_ROLE = role[:16]


F = TypeVar("F", bound=Callable[..., Any])


def _isolated(call: Callable[[], Any]) -> None:
    """Run an optional telemetry action; log and swallow any failure."""
    try:
        call()
    except Exception as exc:  # noqa: BLE001 - telemetry must never affect application transactions
        logger.debug("telemetry sink failure (%s)", type(exc).__name__)


def count(name: str, value: float = 1, /, **labels: str) -> None:
    """Failure-isolated counter increment."""
    _isolated(lambda: registry.inc(name, value, **labels))


def observe_ms(name: str, started: float, **labels: str) -> None:
    """Failure-isolated histogram observation of ``perf_counter() - started`` in milliseconds."""
    _isolated(lambda: registry.observe(name, (time.perf_counter() - started) * 1000, **labels))


def record_model_call(capability: str, alias: str | None, started: float, ok: bool, response: Any = None,
                      model_identity: str | None = None, first_token_ms: float | None = None) -> Usage | None:
    """Record model-call latency/outcome and token totals; unknown usage is counted, never zeroed."""
    result: list[Usage | None] = [None]

    def work() -> None:
        """Update metrics for the call."""
        latency = (time.perf_counter() - started) * 1000
        labels = {"capability": capability, "alias": alias or "unknown"}
        registry.observe("model_call_ms", latency, **labels, outcome="ok" if ok else "error")
        registry.inc("model_calls_total", **labels, outcome="ok" if ok else "error")
        if first_token_ms is not None:
            registry.observe("model_first_token_ms", first_token_ms, **labels)
        if ok:
            usage = usage_from_response(response, model_identity=model_identity, latency_ms=latency)
            result[0] = usage
            if usage.tokens_in is None and usage.tokens_out is None:
                registry.inc("model_usage_unknown_total", **labels)
            for field, metric in (("tokens_in", "model_tokens_in_total"), ("tokens_out", "model_tokens_out_total")):
                if getattr(usage, field) is not None:
                    registry.inc(metric, getattr(usage, field), **labels)

    _isolated(work)
    # The optional exporter receives only the summary already exposed by observability; it never
    # receives the provider response, prompt, completion, exception text, or credentials.
    def export_summary() -> None:
        """Offer this model-call summary to the optional bounded remote telemetry queue."""
        from core.langfuse_export import submit_model_summary

        summary = result[0].model_dump() if result[0] is not None else {}
        submit_model_summary(
            capability=capability,
            model_identity=model_identity,
            ok=ok,
            usage=summary,
            trace=current_trace().model_dump(),
            started=started,
        )

    _isolated(export_summary)
    return result[0]


# --------------------------------------------------------------------------- Redis publication

async def flush_metrics(redis: Any, *, force: bool = False) -> None:
    """Publish this process's snapshot to Redis (throttled); a Redis failure is swallowed."""
    now = time.monotonic()
    if not force and now - registry.last_flush < _FLUSH_INTERVAL:
        return
    registry.last_flush = now
    try:
        await asyncio.wait_for(
            redis.set(f"{SNAPSHOT_KEY_PREFIX}{_PROCESS_ROLE}:{os.getpid()}", json.dumps(registry.snapshot()),
                      ex=SNAPSHOT_TTL_SECONDS),
            timeout=_REDIS_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("telemetry snapshot publish failed (%s)", type(exc).__name__)


async def read_snapshots(redis: Any) -> list[dict[str, Any]]:
    """Read at most 16 snapshots, 64 scan results and 0.5 seconds of Redis work."""
    out: list[dict[str, Any]] = []
    try:
        async with asyncio.timeout(_REDIS_TIMEOUT_SECONDS):
            scanned = 0
            async for key in redis.scan_iter(match=f"{SNAPSHOT_KEY_PREFIX}*", count=16):
                scanned += 1
                if scanned > _SNAPSHOT_SCAN_LIMIT:
                    break
                if len(out) >= 16:
                    break
                raw = await redis.get(key)
                if raw:
                    data = json.loads(raw)
                    data["process"] = (key.decode() if isinstance(key, bytes) else key)[len(SNAPSHOT_KEY_PREFIX):]
                    out.append(data)
    except Exception as exc:  # noqa: BLE001
        logger.debug("telemetry snapshot read failed (%s)", type(exc).__name__)
    return out


def instrument_job(fn: F, *, run_id_kind: str | None = None, success_return_outcome: str = "ok") -> F:  # noqa: UP047  # keep TypeVar/TypeAlias spelling; PEP 695 rewrite is style-only
    """Wrap an ARQ job to record queue delay, duration and outcome by function name; behavior is unchanged.

    ``run_id_kind`` ("agent_run_id"/"ingestion_run_id") binds the first argument; domain jobs can
    label normal returns "returned" when durable state determines success. Exceptions (including
    arq ``Retry``) propagate untouched. The function-name label comes from the fixed worker registry.
    """
    name = fn.__name__

    @functools.wraps(fn)
    async def wrapper(ctx: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        """Time the job, bind trace IDs, publish metrics, then return or re-raise the job's own result."""
        started = time.perf_counter()
        token = _trace.set(current_trace())
        outcome = "ok"
        try:
            def pre() -> None:
                """Bind the run id and record how long the job waited in the queue."""
                if run_id_kind and args:
                    set_trace(**{run_id_kind: str(args[0])})
                enqueued = ctx.get("enqueue_time")
                if isinstance(enqueued, datetime):
                    delay = (datetime.now(UTC) - enqueued.astimezone(UTC)).total_seconds() * 1000
                    registry.observe("queue_delay_ms", max(delay, 0.0), job=name)

            _isolated(pre)
            return await fn(ctx, *args, **kwargs)
        except BaseException as exc:
            outcome = "retry" if type(exc).__name__ == "Retry" else "error"
            raise
        finally:
            if outcome == "ok":
                outcome = success_return_outcome
            observe_ms("job_duration_ms", started, job=name, outcome=outcome)
            count("jobs_total", job=name, outcome=outcome)
            _trace.reset(token)
            redis = ctx.get("redis")
            if redis is not None:
                # The job result is already determined; cancellation or a stalled sink must not replace it.
                try:
                    await asyncio.wait_for(flush_metrics(redis), timeout=_REDIS_TIMEOUT_SECONDS)
                except asyncio.CancelledError:
                    task = asyncio.current_task()
                    if task is not None and task.cancelling():
                        raise
                except Exception:  # noqa: BLE001, S110  # telemetry errors never replace the job outcome
                    pass

    return wrapper  # type: ignore[return-value]


def timed(metric: str, **labels: str) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Decorator recording an async function's duration into ``metric`` with fixed labels."""
    def deco(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        """Wrap ``fn``."""
        @functools.wraps(fn)
        async def inner(*args: Any, **kwargs: Any) -> Any:
            """Run ``fn`` and observe its duration regardless of outcome."""
            started = time.perf_counter()
            try:
                return await fn(*args, **kwargs)
            finally:
                observe_ms(metric, started, **labels)
        return inner
    return deco
