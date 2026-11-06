"""Behavior tests for the optional metadata-only Langfuse exporter: gating, scrubbing and queue delivery."""

import asyncio
import time
from typing import Any
from uuid import uuid4

import httpx
import pytest

from core import langfuse_export as lf

_ENV = {
    "LANGFUSE_ENABLED": "true",
    "TELEMETRY_EGRESS_APPROVED": "true",
    "LANGFUSE_BASE_URL": "https://lf.example.com",
    "LANGFUSE_PUBLIC_KEY": "pk",
    "LANGFUSE_SECRET_KEY": "sk",
}


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(lf, "_queue", None)
    monkeypatch.setattr(lf, "_queue_loop", None)
    monkeypatch.setattr(lf, "_worker", None)


def _enable(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    for name, value in {**_ENV, **overrides}.items():
        monkeypatch.setenv(name, value)


def test_configuration_requires_both_opt_in_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    assert lf._configuration() is None
    _enable(monkeypatch, TELEMETRY_EGRESS_APPROVED="false")
    assert lf._configuration() is None
    _enable(monkeypatch, LANGFUSE_ENABLED="false")
    assert lf._configuration() is None


def test_configuration_returns_endpoint_and_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch, LANGFUSE_BASE_URL="https://lf.example.com/")
    assert lf._configuration() == ("https://lf.example.com/api/public/otel/v1/traces", "pk", "sk")


@pytest.mark.parametrize(
    "url",
    [
        "http://lf.example.com",
        "https://user:pw@lf.example.com",
        "https://lf.example.com/path",
        "https://lf.example.com?x=1",
        "https://lf.example.com#frag",
        "",
        "https://[bad",
    ],
)
def test_configuration_rejects_unsafe_base_urls(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    _enable(monkeypatch, LANGFUSE_BASE_URL=url)
    assert lf._configuration() is None


@pytest.mark.parametrize("missing", ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"])
def test_configuration_requires_both_keys(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    _enable(monkeypatch, **{missing: "  "})
    assert lf._configuration() is None


def test_attribute_uses_otlp_wire_types() -> None:
    assert lf._attribute("a", 3) == {"key": "a", "value": {"intValue": "3"}}
    assert lf._attribute("a", "x") == {"key": "a", "value": {"stringValue": "x"}}


def _attrs(span_doc: dict[str, Any]) -> dict[str, Any]:
    span = span_doc["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    return {a["key"]: next(iter(a["value"].values())) for a in span["attributes"]}


def _span(**overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "capability": "chat",
        "model_identity": "claude-x",
        "ok": True,
        "usage": {"tokens_in": 5, "tokens_out": 7},
        "trace": {},
        "started": time.perf_counter(),
    }
    params.update(overrides)
    return lf._span(**params)


def test_span_carries_usage_and_sanitized_model_and_status() -> None:
    doc = _span(capability="bad cap/!", model_identity="gpt 4")
    attrs = _attrs(doc)
    assert attrs["langfuse.observation.name"] == "model.bad_cap__"
    assert attrs["gen_ai.request.model"] == "gpt_4"
    assert attrs["gen_ai.usage.input_tokens"] == "5"
    assert attrs["gen_ai.usage.output_tokens"] == "7"
    span = doc["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span["status"] == {"code": 1}
    assert int(span["startTimeUnixNano"]) <= int(span["endTimeUnixNano"])
    assert _span(ok=False)["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["status"] == {"code": 2}


@pytest.mark.parametrize(
    "model",
    [
        "https://evil/x",
        "me@host",
        "Bearer abc",
        "sk-abcdefghijklmnopqrstuvwx",
        "ghp_" + "a" * 24,
        "eyJhbGci",
        "k=v",
    ],
)
def test_span_drops_secret_looking_model_identity(model: str) -> None:
    assert "gen_ai.request.model" not in _attrs(_span(model_identity=model))


def test_span_omits_invalid_usage_and_blank_capability() -> None:
    attrs = _attrs(_span(capability="", usage={"tokens_in": -1, "tokens_out": "9"}))
    assert attrs["langfuse.observation.name"] == "model.unknown"
    assert "gen_ai.usage.input_tokens" not in attrs
    assert "gen_ai.usage.output_tokens" not in attrs


def test_span_uses_first_valid_correlation_id_as_trace_id() -> None:
    request_id = uuid4()
    run_id = uuid4()
    doc = _span(
        trace={
            "request_id": str(request_id),
            "agent_run_id": str(run_id),
            "tool_call_id": "not-a-uuid",
        }
    )
    span = doc["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span["traceId"] == request_id.hex
    attrs = _attrs(doc)
    assert attrs["langfuse.trace.metadata.agent_run_id"] == str(run_id)
    assert "langfuse.trace.metadata.tool_call_id" not in attrs


def test_span_invalid_correlation_id_gets_fresh_trace_id() -> None:
    span = _span(trace={"request_id": "nope"})["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert len(span["traceId"]) == 32 and len(span["spanId"]) == 16


async def test_submit_is_noop_when_disabled() -> None:
    lf.submit_model_summary(
        capability="c",
        model_identity=None,
        ok=True,
        usage={},
        trace={},
        started=time.perf_counter(),
    )
    assert lf._queue is None


def test_submit_is_noop_without_running_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    lf.submit_model_summary(
        capability="c",
        model_identity=None,
        ok=True,
        usage={},
        trace={},
        started=time.perf_counter(),
    )
    assert lf._queue is None


async def test_submit_enqueues_then_worker_posts_with_basic_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500 if len(seen) == 1 else 204)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        lf.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    for _ in range(2):
        lf.submit_model_summary(
            capability="chat",
            model_identity="m",
            ok=True,
            usage={},
            trace={},
            started=time.perf_counter(),
        )
    assert lf._queue is not None
    await asyncio.wait_for(lf._queue.join(), 5)
    assert len(seen) == 2
    assert seen[0].url == "https://lf.example.com/api/public/otel/v1/traces"
    assert seen[0].headers["authorization"].startswith("Basic ")
    assert seen[0].headers["x-langfuse-ingestion-version"] == "4"
    assert lf._worker is not None
    lf._worker.cancel()


async def test_failed_send_is_swallowed_and_queue_still_drains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        lf.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    lf.submit_model_summary(
        capability="chat",
        model_identity=None,
        ok=True,
        usage={},
        trace={},
        started=time.perf_counter(),
    )
    assert lf._queue is not None
    await asyncio.wait_for(lf._queue.join(), 5)
    assert lf._worker is not None and not lf._worker.done()
    lf._worker.cancel()


async def test_full_queue_drops_new_summaries(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)
    queue.put_nowait({})
    monkeypatch.setattr(lf, "_queue", queue)
    monkeypatch.setattr(lf, "_queue_loop", asyncio.get_running_loop())
    lf.submit_model_summary(
        capability="c",
        model_identity=None,
        ok=True,
        usage={},
        trace={},
        started=time.perf_counter(),
    )
    assert queue.qsize() == 1 and lf._worker is None
