"""OpenTelemetry tracing: off by default, zero overhead when off, scrubbed when on.

* ``configure_tracing`` is a no-op (and imports nothing from ``opentelemetry``) unless
  ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set.
* Trace context crosses the API -> arq boundary as a W3C ``traceparent`` carrier stored in the job
  kwargs under ``TRACE_KWARG``. ``TracingArqRedis.enqueue_job`` injects it; ``traced_job`` (applied to
  every worker function) pops it, so job functions never see it, and continues the trace.
* Nothing sensitive is exported: spans are filtered by ``ScrubbingSpanProcessor`` (no bodies or
  headers, URL query strings and userinfo removed, SQL literals and Redis arguments removed, bound
  SQL parameters never recorded).
"""

from __future__ import annotations

import functools
import logging
import re
from collections.abc import Callable, Mapping
from typing import Any, cast

from arq.connections import ArqRedis

logger = logging.getLogger("bbd.tracing")

TRACE_KWARG = "_trace_carrier"
_TRACER_NAME = "umwelt-os"
_enabled = False

# Attribute keys dropped outright (bodies, headers, bound parameters, raw queries).
_DROP_PREFIXES = (
    "http.request.header.",
    "http.response.header.",
    "http.request.body",
    "http.response.body",
    "db.statement.parameters",
    "db.query.parameter.",
    "db.operation.parameter.",
    "url.query",
    "http.request_content",
)
_URL_KEYS = frozenset({"http.url", "url.full", "http.target", "url.path", "http.route"})
_SQL_KEYS = frozenset({"db.statement", "db.query.text"})
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
_NUMBER_LITERAL = re.compile(r"\b\d+(?:\.\d+)?\b")
_MAX_STATEMENT = 512


def tracing_enabled() -> bool:
    """Return whether ``configure_tracing`` activated an exporter in this process."""
    return _enabled


def scrub_url(value: str) -> str:
    """Remove userinfo, query string and fragment from a URL or request target."""
    value = value.split("#", 1)[0].split("?", 1)[0]
    return re.sub(r"^([a-zA-Z][a-zA-Z0-9+.-]*://)[^/@]*@", r"\1", value)


def scrub_statement(system: str, value: str) -> str:
    """Reduce a database statement to a content-free shape.

    SQL: string and numeric literals become ``?`` (placeholders such as ``$1`` already carry no data)
    and the text is truncated. Redis (and any non-SQL system): only the command name is kept, since
    the arguments are keys and values.
    """
    if system in {"redis", "memcached"}:
        return value.strip().split(" ", 1)[0][:32]
    stripped = _NUMBER_LITERAL.sub("?", _STRING_LITERAL.sub("?", value))
    return stripped[:_MAX_STATEMENT]


def scrub_attributes(attributes: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of span ``attributes`` with sensitive data removed (pure function; see module docstring)."""
    system = str(attributes.get("db.system", attributes.get("db.system.name", "")))
    cleaned: dict[str, Any] = {}
    for key, value in attributes.items():
        if key.startswith(_DROP_PREFIXES):
            continue
        if key in _URL_KEYS and isinstance(value, str):
            value = scrub_url(value)
        elif key in _SQL_KEYS and isinstance(value, str):
            value = scrub_statement(system, value)
        cleaned[key] = value
    return cleaned


def _build_scrubbing_processor(delegate: Any) -> Any:
    """Create a SpanProcessor that scrubs each finished span's attributes before ``delegate`` batches it."""
    from opentelemetry.sdk.trace import SpanProcessor

    class ScrubbingSpanProcessor(SpanProcessor):
        """Scrub finished spans, then hand them to the wrapped (batching) processor."""

        def on_start(self, span: Any, parent_context: Any = None) -> None:
            """Forward span start to the wrapped processor."""
            delegate.on_start(span, parent_context)

        def on_end(self, span: Any) -> None:
            """Replace the span's attributes with the scrubbed copy; drop the span when that is impossible (fail closed)."""
            try:
                original = span._attributes
                scrubbed = scrub_attributes(dict(original or {}))
                for key in list(original or {}):
                    if key not in scrubbed or scrubbed[key] != original[key]:
                        _set_attribute(original, key, scrubbed.get(key))
                _scrub_events_and_status(span)
            except Exception:  # noqa: BLE001 - never export an unscrubbed span
                logger.debug("span scrub failed; span dropped")
                return
            delegate.on_end(span)

        def shutdown(self) -> None:
            """Shut down the wrapped processor."""
            delegate.shutdown()

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            """Flush the wrapped processor."""
            return bool(delegate.force_flush(timeout_millis))

    return ScrubbingSpanProcessor()


_EXCEPTION_DROP_KEYS = ("exception.message", "exception.stacktrace")


def _scrub_events_and_status(span: Any) -> None:
    """Remove free-text error content from a finished span's events and status (in place).

    Exception messages and stack traces can echo SQL ``[parameters: ...]`` or input values, so
    ``exception.message``/``exception.stacktrace`` are dropped (``exception.type`` is kept) and the
    status keeps its code but loses its description. Other event attributes go through
    ``scrub_attributes``. Raises on unexpected SDK shapes so the caller fails closed.
    """
    for event in list(getattr(span, "_events", None) or ()):
        attrs = getattr(event, "attributes", None)
        if not attrs:
            continue
        cleaned = scrub_attributes(dict(attrs))
        for key in list(attrs):
            if key in _EXCEPTION_DROP_KEYS or key not in cleaned:
                _set_attribute(attrs, key, None)
            elif cleaned[key] != attrs[key]:
                _set_attribute(attrs, key, cleaned[key])
    status = getattr(span, "_status", None)
    if status is not None and getattr(status, "description", None):
        from opentelemetry.trace import Status

        span._status = Status(status.status_code)


def _set_attribute(store: Any, key: str, value: Any) -> None:
    """Write or delete ``key`` in an SDK attribute store whose immutability flag may already be set."""
    immutable = getattr(store, "_immutable", False)
    if immutable:
        store._immutable = False
    try:
        if value is None:
            store._dict.pop(key, None)
        else:
            store._dict[key] = value
    finally:
        if immutable:
            store._immutable = True


def configure_tracing(endpoint: str, *, service_name: str, sample_ratio: float = 1.0) -> bool:
    """Install the OTLP exporter and global tracer provider when ``endpoint`` is non-empty.

    Returns:
        True when tracing was activated; False (nothing imported, nothing patched) when ``endpoint``
        is empty or the OpenTelemetry packages are unavailable.
    Side effects:
        Sets the global tracer provider, instruments httpx and redis, and flips ``tracing_enabled``.
        Idempotent per process. Exporting is asynchronous (batch processor); export failures are
        logged by the SDK and never reach request or job code.
    """
    global _enabled
    endpoint = endpoint.strip()
    if not endpoint:
        return False
    if _enabled:
        return True
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
    except ImportError:
        logger.warning(
            "OTEL_EXPORTER_OTLP_ENDPOINT is set but OpenTelemetry packages are not installed"
        )
        return False
    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name}),
        sampler=ParentBased(TraceIdRatioBased(sample_ratio)),
    )
    exporter = OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")
    provider.add_span_processor(_build_scrubbing_processor(BatchSpanProcessor(exporter)))
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()
    RedisInstrumentor().instrument()
    _enabled = True
    return True


def instrument_engine(engine: Any) -> None:
    """Trace SQL for an ``AsyncEngine``; no-op while tracing is off. Bound parameters are never recorded."""
    if not _enabled:
        return
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine, enable_commenter=False)


def instrument_fastapi(app: Any) -> None:
    """Trace inbound requests of ``app`` (health and metrics excluded); no-op while tracing is off.

    Must run before the app starts serving, because it adds a middleware.
    """
    if not _enabled:
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(
        app, excluded_urls="/health$,/metrics$", exclude_spans=["send", "receive"]
    )


def inject_carrier() -> dict[str, str] | None:
    """Return a W3C trace-context carrier for the current span, or None when tracing is off or no span is active."""
    if not _enabled:
        return None
    from opentelemetry import propagate, trace

    if not trace.get_current_span().get_span_context().is_valid:
        return None
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier or None


class TracingArqRedis(ArqRedis):
    """``ArqRedis`` that adds the caller's trace context to every enqueued job's kwargs while tracing is on."""

    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> Any:
        """Enqueue like arq, attaching ``TRACE_KWARG`` first. Arq options (``_job_id``, ``_queue_name``...) pass through.

        With tracing off this is a straight delegation: the job payload is byte-identical to the
        untraced path.
        """
        carrier = inject_carrier()
        if carrier is not None:
            kwargs[TRACE_KWARG] = carrier
        return await super().enqueue_job(function, *args, **kwargs)


def traced_job(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap an arq job: always strip ``TRACE_KWARG``; while tracing is on, run the job in a consumer span of the producer's trace.

    The wrapper keeps ``fn.__name__``. It never changes the job's return value or exceptions
    (the span records exceptions and re-raises). Stripping happens even when tracing is off here so
    an API with tracing on can feed a worker with tracing off.
    """
    name = fn.__name__

    @functools.wraps(fn)
    async def wrapper(ctx: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        """Continue the producer's trace around ``fn``."""
        carrier = kwargs.pop(TRACE_KWARG, None)
        if not _enabled:
            return await fn(ctx, *args, **kwargs)
        from opentelemetry import propagate, trace

        parent = propagate.extract(carrier) if isinstance(carrier, dict) else None
        tracer = trace.get_tracer(_TRACER_NAME)
        with tracer.start_as_current_span(
            f"job {name}",
            context=parent,
            kind=trace.SpanKind.CONSUMER,
            attributes={"messaging.system": "arq", "job.name": name},
        ):
            return await fn(ctx, *args, **kwargs)

    return cast(Callable[..., Any], wrapper)
