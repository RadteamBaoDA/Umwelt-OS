"""Tracing: off by default, context propagates API -> arq job, exported spans are scrubbed."""

from __future__ import annotations

from typing import Any

import pytest
from arq.connections import ArqRedis
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from core import tracing


@pytest.fixture
def exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """Enable tracing against an in-memory exporter behind the real scrubbing processor."""
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(tracing._build_scrubbing_processor(SimpleSpanProcessor(memory)))
    monkeypatch.setattr(tracing, "_enabled", True)
    monkeypatch.setattr(trace, "get_tracer", lambda *_a, **_k: provider.get_tracer("test"))
    return memory


def _fake_enqueue(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace arq's enqueue_job with a recorder of (function, args, kwargs)."""
    sent: list[dict[str, Any]] = []

    async def record(self: ArqRedis, function: str, *args: Any, **kwargs: Any) -> None:
        """Capture the enqueue call."""
        sent.append({"function": function, "args": args, "kwargs": kwargs})

    monkeypatch.setattr(ArqRedis, "enqueue_job", record)
    return sent


def test_disabled_by_default_imports_nothing() -> None:
    """An empty endpoint is a no-op: no provider, no OpenTelemetry SDK import, no kwarg injected."""
    assert tracing.configure_tracing("", service_name="x") is False
    assert tracing.tracing_enabled() is False
    assert tracing.inject_carrier() is None


async def test_enqueue_is_untouched_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """With tracing off the job kwargs are exactly what the caller passed."""
    sent = _fake_enqueue(monkeypatch)
    redis = object.__new__(tracing.TracingArqRedis)
    await redis.enqueue_job("job", "a", _job_id="j", k=1)
    assert sent == [{"function": "job", "args": ("a",), "kwargs": {"_job_id": "j", "k": 1}}]


async def test_disabled_worker_still_strips_carrier() -> None:
    """A worker with tracing off must not pass the producer's carrier to the job function."""
    seen: dict[str, Any] = {}

    async def job(ctx: dict[str, Any], value: str, **kwargs: Any) -> str:
        """Record what the job received."""
        seen.update(kwargs)
        return value

    wrapped = tracing.traced_job(job)
    assert wrapped.__name__ == "job"
    assert await wrapped({}, "v", extra=1, **{tracing.TRACE_KWARG: {"traceparent": "x"}}) == "v"
    assert seen == {"extra": 1}


async def test_trace_context_propagates_through_fake_arq(
    monkeypatch: pytest.MonkeyPatch, exporter: InMemorySpanExporter,
) -> None:
    """A request span and the worker job span it enqueued share one trace; the job never sees the carrier."""
    sent = _fake_enqueue(monkeypatch)
    redis = object.__new__(tracing.TracingArqRedis)
    tracer = trace.get_tracer("api")
    with tracer.start_as_current_span("request"):
        await redis.enqueue_job("process_thing", "arg", _job_id="thing:1")
    kwargs = {k: v for k, v in sent[0]["kwargs"].items() if not k.startswith("_job")}
    assert sent[0]["kwargs"]["_job_id"] == "thing:1"
    assert "traceparent" in kwargs[tracing.TRACE_KWARG]

    received: dict[str, Any] = {}

    async def process_thing(ctx: dict[str, Any], arg: str, **job_kwargs: Any) -> None:
        """Pretend job."""
        received.update(job_kwargs)

    await tracing.traced_job(process_thing)({}, "arg", **kwargs)
    assert received == {}  # the carrier was stripped; _job_id is an enqueue option, not a job kwarg
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert spans["job process_thing"].context.trace_id == spans["request"].context.trace_id
    assert spans["job process_thing"].parent is not None
    assert spans["job process_thing"].parent.span_id == spans["request"].context.span_id


async def test_enqueue_without_active_span_adds_nothing(
    monkeypatch: pytest.MonkeyPatch, exporter: InMemorySpanExporter,
) -> None:
    """No current span means nothing to propagate."""
    sent = _fake_enqueue(monkeypatch)
    await object.__new__(tracing.TracingArqRedis).enqueue_job("job")
    assert sent[0]["kwargs"] == {}


async def test_job_failure_is_reraised_and_span_recorded(
    monkeypatch: pytest.MonkeyPatch, exporter: InMemorySpanExporter,
) -> None:
    """Exceptions pass through the traced wrapper unchanged."""
    async def boom(ctx: dict[str, Any]) -> None:
        """Always fail."""
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        await tracing.traced_job(boom)({})
    assert [s.name for s in exporter.get_finished_spans()] == ["job boom"]


def test_scrub_attributes_removes_sensitive_data() -> None:
    """Bodies, headers, query strings, userinfo, SQL literals and Redis arguments never survive."""
    cleaned = tracing.scrub_attributes({
        "http.url": "https://user:pw@host.example/path?token=secret#frag",
        "http.request.header.authorization": "Bearer x",
        "http.request.body": "{...}",
        "db.system": "postgresql",
        "db.statement": "SELECT * FROM t WHERE email = 'a@b.c' AND id = 42",
        "db.statement.parameters": "('a@b.c',)",
        "http.status_code": 200,
    })
    assert cleaned == {
        "http.url": "https://host.example/path",
        "db.system": "postgresql",
        "db.statement": "SELECT * FROM t WHERE email = ? AND id = ?",
        "http.status_code": 200,
    }
    redis_attrs = tracing.scrub_attributes({"db.system": "redis", "db.statement": "SET session:abc secret-value"})
    assert redis_attrs["db.statement"] == "SET"


def test_exported_spans_are_scrubbed(exporter: InMemorySpanExporter) -> None:
    """The processor rewrites attributes of finished spans before export."""
    tracer = trace.get_tracer("t")
    with tracer.start_as_current_span("db", attributes={
        "db.system": "postgresql", "db.statement": "UPDATE u SET pw = 'hunter2'", "http.request.body": "secret",
    }):
        pass
    (span,) = exporter.get_finished_spans()
    assert dict(span.attributes or {}) == {"db.system": "postgresql", "db.statement": "UPDATE u SET pw = ?"}



async def test_failed_job_exports_no_sql_parameters(exporter: InMemorySpanExporter) -> None:
    """A DBAPIError carrying bound parameters leaks nothing via attributes, events or status."""
    from sqlalchemy.exc import DBAPIError

    async def boom(ctx: dict[str, Any]) -> None:
        """Fail like a database driver echoing bound parameters."""
        raise DBAPIError("UPDATE users SET email=$1", ("secret@example.com",), Exception("bad secret@example.com"))

    with pytest.raises(DBAPIError):
        await tracing.traced_job(boom)({})
    (span,) = exporter.get_finished_spans()
    dumped = repr(dict(span.attributes or {})) + repr(span.status.description)
    for event in span.events:
        dumped += repr(dict(event.attributes or {}))
    assert "secret" not in dumped
    assert span.status.status_code is trace.StatusCode.ERROR
    assert [e.attributes["exception.type"] for e in span.events if e.name == "exception"]
