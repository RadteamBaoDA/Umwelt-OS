"""Prometheus registry, bounded labels, /metrics access policy, samplers, cron and breaker hooks."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Self

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from prometheus_client import generate_latest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import create_async_engine

from apps.worker.main import _observe_cron
from core import metrics
from core.config import Settings
from core.metrics_http import MetricsMiddleware, metrics_access, metrics_enabled


def _sample(name: str, **labels: str) -> float:
    """Read one sample value from the process registry (0.0 when the series does not exist)."""
    value = metrics.REGISTRY.get_sample_value(name, labels)
    return 0.0 if value is None else value


def _settings(token: str = "", environment: str = "production") -> Settings:
    """Settings carrying only the fields the access policy reads."""
    # model_construct skips the production-secret validators; only the fields the policy reads matter here.
    return Settings.model_construct(environment=environment, metrics_token=SecretStr(token))


def _app(settings: Settings) -> FastAPI:
    """A tiny app with a templated route behind the metrics middleware."""
    app = FastAPI()

    @app.get("/items/{item_id}")
    async def item(item_id: int) -> dict[str, int]:
        """Return the id."""
        return {"id": item_id}

    @app.get("/boom")
    async def boom() -> None:
        """Respond 500 through an HTTP error."""
        from fastapi import HTTPException

        raise HTTPException(status_code=503)

    app.add_middleware(MetricsMiddleware, settings=settings)
    return app


def _client(app: FastAPI, host: str = "127.0.0.1") -> AsyncClient:
    """Async client whose ASGI scope reports ``host`` as the direct peer."""
    return AsyncClient(transport=ASGITransport(app=app, client=(host, 5000)), base_url="http://test")


async def test_http_metrics_use_route_template_not_raw_path() -> None:
    """Raw ids never become label values; unmatched paths collapse to one series."""
    async with _client(_app(_settings())) as client:
        before = _sample("umwelt_http_requests_total", method="GET", route="/items/{item_id}", status="200")
        for item_id in (1, 2, 3):
            assert (await client.get(f"/items/{item_id}")).status_code == 200
        await client.get("/no/such/path/12345")
        await client.get("/boom")
    assert _sample("umwelt_http_requests_total", method="GET", route="/items/{item_id}", status="200") == before + 3
    assert _sample("umwelt_http_requests_total", method="GET", route="unmatched", status="404") >= 1
    assert _sample("umwelt_http_requests_total", method="GET", route="/boom", status="503") >= 1
    exposition = generate_latest(metrics.REGISTRY).decode()
    assert "/items/1" not in exposition
    assert "12345" not in exposition
    assert _sample("umwelt_http_request_duration_seconds_count", method="GET", route="/items/{item_id}") >= 3


async def test_metrics_endpoint_with_token_requires_bearer() -> None:
    """A configured token is the only way in, from any address."""
    async with _client(_app(_settings("s3cret-token")), host="10.0.0.5") as client:
        assert (await client.get("/metrics")).status_code == 401
        assert (await client.get("/metrics", headers={"Authorization": "Bearer wrong"})).status_code == 401
        response = await client.get("/metrics", headers={"Authorization": "Bearer s3cret-token"})
    assert response.status_code == 200
    assert "umwelt_http_requests_total" in response.text
    assert response.headers["cache-control"] == "no-store"


async def test_metrics_endpoint_disabled_in_production_without_token() -> None:
    """Production with no token: 404 even from loopback."""
    async with _client(_app(_settings()), host="127.0.0.1") as client:
        assert (await client.get("/metrics")).status_code == 404
    assert metrics_enabled(_settings()) is False
    assert metrics_enabled(_settings("t")) is True


async def test_metrics_endpoint_dev_allows_internal_direct_clients_only() -> None:
    """Development without a token: private direct peers pass; public peers and proxied requests do not."""
    app = _app(_settings(environment="development"))
    async with _client(app, host="172.18.0.4") as client:
        assert (await client.get("/metrics")).status_code == 200
        assert (await client.get("/metrics", headers={"X-Forwarded-For": "8.8.8.8"})).status_code == 404
    async with _client(app, host="8.8.8.8") as client:
        assert (await client.get("/metrics")).status_code == 404


def test_metrics_access_policy_unit() -> None:
    """The pure decision function."""
    assert metrics_access(_settings("t"), {b"authorization": b"Bearer t"}, None) == "allowed"
    assert metrics_access(_settings("t"), {b"authorization": b"Basic t"}, "127.0.0.1") == "unauthorized"
    assert metrics_access(_settings(), {}, "127.0.0.1") == "disabled"
    assert metrics_access(_settings(environment="dev"), {}, "localhost") == "disabled"  # hostnames are never "internal"


def test_label_helpers_bound_cardinality() -> None:
    """Labels are length-limited and stripped of odd characters."""
    assert metrics.safe_label("a b\nc") == "a_b_c"
    assert len(metrics.safe_label("x" * 1000)) == 96
    assert metrics.safe_label("") == "unknown"
    metrics.record_http("BREW", "/x", 200, 0.1)
    assert _sample("umwelt_http_requests_total", method="OTHER", route="/x", status="200") >= 1


def test_record_job_maps_ingestion_stages() -> None:
    """Ingestion jobs also feed the stage counter; other jobs do not."""
    before = _sample("umwelt_ingestion_stage_total", stage="normalize", outcome="returned")
    metrics.record_job("process_normalize_event", "returned", 0.2)
    metrics.record_job("translate_content", "ok", 0.2)
    assert _sample("umwelt_ingestion_stage_total", stage="normalize", outcome="returned") == before + 1
    assert _sample("umwelt_jobs_total", job="translate_content", outcome="ok") >= 1
    assert "stage=\"translate" not in generate_latest(metrics.REGISTRY).decode()


async def test_cron_wrapper_records_success_failure_and_preserves_behaviour() -> None:
    """Duration and last success on success; failure counter on error; result and exception unchanged."""
    async def good(ctx: dict[str, object]) -> int:
        """Succeed."""
        return 7

    async def bad(ctx: dict[str, object]) -> None:
        """Fail."""
        raise RuntimeError("x")

    wrapped_good, wrapped_bad = _observe_cron(good), _observe_cron(bad)
    assert wrapped_good.__name__ == "good"
    started = time.time()
    assert await wrapped_good({}) == 7
    with pytest.raises(RuntimeError):
        await wrapped_bad({})
    assert _sample("umwelt_cron_job_last_success_timestamp_seconds", job="good") >= started
    assert _sample("umwelt_cron_job_duration_seconds_count", job="good") == 1
    assert _sample("umwelt_cron_job_failures_total", job="bad") == 1
    assert _sample("umwelt_cron_job_last_success_timestamp_seconds", job="bad") == 0


class _FakeRedis:
    """Minimal sorted-set stand-in for ``zcard``/``zrange``."""

    def __init__(self, queues: dict[str, list[tuple[str, float]]]) -> None:
        """Store member/score pairs per queue key."""
        self.queues = queues

    async def zcard(self, key: str) -> int:
        """Return the member count."""
        return len(self.queues.get(key, []))

    async def zrange(self, key: str, start: int, stop: int, withscores: bool = False) -> list[tuple[str, float]]:
        """Return the lowest-scored member."""
        return sorted(self.queues.get(key, []), key=lambda item: item[1])[:1]


async def test_queue_sampler_depth_and_oldest_age() -> None:
    """Depth is the ZSET size; age is measured from the lowest due time and is never negative."""
    now = 1_000_000.0
    redis: Any = _FakeRedis({"arq:queue": [("a", (now - 42) * 1000), ("b", (now - 5) * 1000)], "arq:model": [("c", (now + 60) * 1000)]})
    await metrics.sample_arq_queues(redis, ["arq:queue", "arq:model", "arq:chat"], now=now)
    assert _sample("umwelt_arq_queue_depth", queue="arq:queue") == 2
    assert _sample("umwelt_arq_queue_oldest_job_age_seconds", queue="arq:queue") == pytest.approx(42)
    assert _sample("umwelt_arq_queue_oldest_job_age_seconds", queue="arq:model") == 0  # deferred, not waiting
    assert _sample("umwelt_arq_queue_depth", queue="arq:chat") == 0


async def test_sampler_loop_counts_failures_and_survives() -> None:
    """A failing sampler is counted; the other samplers keep running."""
    calls = 0

    async def failing() -> None:
        """Always fail."""
        raise RuntimeError("down")

    async def counting() -> None:
        """Count invocations."""
        nonlocal calls
        calls += 1

    task = asyncio.ensure_future(metrics.run_sampler_loop([("failing_test", failing), ("counting", counting)], interval=0.01))
    await asyncio.sleep(0.08)
    await metrics.stop_background([task])
    assert calls >= 2
    assert _sample("umwelt_metrics_sample_errors_total", sampler="failing_test") >= 2


async def test_event_loop_lag_detects_blocking() -> None:
    """A synchronous stall inside the loop shows up as lag."""
    task = asyncio.ensure_future(metrics.monitor_event_loop_lag(interval=0.02))
    await asyncio.sleep(0.05)
    time.sleep(0.2)  # noqa: ASYNC251 - deliberately block the loop
    await asyncio.sleep(0.05)
    await metrics.stop_background([task])
    assert _sample("umwelt_event_loop_lag_histogram_seconds_count") >= 1
    assert _sample("umwelt_event_loop_lag_histogram_seconds_bucket", le="+Inf") > _sample("umwelt_event_loop_lag_histogram_seconds_bucket", le="0.1")


async def test_pool_collector_reports_engine_pool() -> None:
    """Size, checked-out and overflow gauges come from the live engine pool."""
    engine = create_async_engine("postgresql+asyncpg://u:p@127.0.0.1:1/db", pool_size=3, max_overflow=2)
    metrics.register_engine(engine)
    exposition = generate_latest(metrics.REGISTRY).decode()
    assert "umwelt_db_pool_size" in exposition
    size_lines = [line for line in exposition.splitlines() if line.startswith("umwelt_db_pool_size{")]
    assert any(line.endswith(" 3.0") for line in size_lines)
    assert any(line.startswith("umwelt_db_pool_max_connections{") and line.endswith(" 5.0") for line in exposition.splitlines())
    await engine.dispose()


def test_breaker_metric_marks_open_window() -> None:
    """Rejections push the open-until timestamp into the future; unrelated providers stay closed."""
    metrics.record_breaker_open("Test Provider", 30)
    assert metrics.breaker_is_open("Test Provider")
    assert not metrics.breaker_is_open("Test Provider", now=time.time() + 31)
    assert not metrics.breaker_is_open("Other")
    assert _sample("umwelt_model_breaker_rejections_total", provider="Test_Provider") == 1


def test_first_token_histogram_recorded_via_model_call() -> None:
    """record_model_call feeds the time-to-first-token histogram in seconds."""
    from core.telemetry import record_model_call

    count_before = _sample("umwelt_chat_time_to_first_token_seconds_count", capability="streaming")
    sum_before = _sample("umwelt_chat_time_to_first_token_seconds_sum", capability="streaming")
    record_model_call("streaming", "chat", time.perf_counter(), True, None, None, first_token_ms=750.0)
    assert _sample("umwelt_chat_time_to_first_token_seconds_count", capability="streaming") == count_before + 1
    assert _sample("umwelt_chat_time_to_first_token_seconds_sum", capability="streaming") == pytest.approx(sum_before + 0.75)


def test_worker_metrics_server_off_by_default() -> None:
    """Port 0 starts nothing."""
    assert metrics.start_worker_metrics_server(0) is False


def test_worker_metrics_server_serves_registry() -> None:
    """A positive port serves the exposition on the configured bind address."""
    import socket
    import urllib.request

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert metrics.start_worker_metrics_server(port, "127.0.0.1") is True
    body = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5).read().decode()
    assert "umwelt_http_requests_total" in body


async def test_outbox_sampler_sets_backlog_and_lag() -> None:
    """The sampler turns the aggregate row into count and age gauges; an empty outbox reads zero."""
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from modules.observability.metrics_sampler import sample_outbox

    class _Result:
        """Single-row result."""

        def __init__(self, row: Any) -> None:
            """Keep the row."""
            self.row = row

        def one(self) -> Any:
            """Return the row."""
            return self.row

    class _Session:
        """Session returning a canned aggregate."""

        def __init__(self, row: Any) -> None:
            """Keep the row."""
            self.row = row

        async def __aenter__(self) -> Self:
            """Enter."""
            return self

        async def __aexit__(self, *_exc: object) -> None:
            """Exit."""

        async def execute(self, _statement: object) -> _Result:
            """Return the canned row."""
            return _Result(self.row)

    row = SimpleNamespace(pending=7, oldest=datetime.now(UTC) - timedelta(seconds=90))
    await sample_outbox(lambda: _Session(row))  # type: ignore[arg-type]
    assert _sample("umwelt_outbox_pending_count") == 7
    assert _sample("umwelt_outbox_oldest_pending_age_seconds") == pytest.approx(90, abs=2)
    await sample_outbox(lambda: _Session(SimpleNamespace(pending=0, oldest=None)))  # type: ignore[arg-type]
    assert _sample("umwelt_outbox_pending_count") == 0
    assert _sample("umwelt_outbox_oldest_pending_age_seconds") == 0


async def test_create_app_serves_metrics_without_adding_a_route() -> None:
    """The composed API answers /metrics from middleware (dev, internal peer) and keeps it out of the route table."""
    from apps.api.main import create_app

    app = create_app(Settings(public_origin="http://localhost:3000", csrf_signing_secret="test-csrf-signing-secret"))
    assert all(getattr(route, "path", "") != "/metrics" for route in app.routes)
    async with _client(app) as client:
        response = await client.get("/metrics")
    assert response.status_code == 200
    assert "umwelt_db_pool_size" in response.text
