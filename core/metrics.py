"""Prometheus metrics: one dedicated registry, bounded labels, failure-isolated recording helpers.

Design rules:
* Instrumentation observes; it never changes the outcome of a request or job. Every ``record_*``
  helper swallows its own failures so a metrics fault cannot fail business logic.
* Label cardinality is bounded by construction: route *templates* (never raw paths), fixed job and
  queue names, provider display names, enumerated outcomes. User ids, URLs and prompts are never labels.
* The registry is process-local. The API serves it on ``/metrics`` (see ``core.metrics_http``);
  worker processes serve it on an optional dedicated port (``METRICS_WORKER_PORT``).
* Gauges that describe shared state (queue depth, outbox lag) are sampled by the API process only,
  so several replicas do not multiply the numbers inside a single scrape.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import weakref
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

logger = logging.getLogger("umwelt.metrics")

REGISTRY = CollectorRegistry(auto_describe=True)

_HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
_JOB_BUCKETS = (0.01, 0.05, 0.1, 0.5, 1.0, 2.5, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0)
_TTFT_BUCKETS = (0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0, 60.0)
_LAG_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5)

HTTP_REQUESTS = Counter(
    "umwelt_http_requests_total",
    "HTTP requests by method, route template and status code.",
    ("method", "route", "status"),
    registry=REGISTRY,
)
HTTP_DURATION = Histogram(
    "umwelt_http_request_duration_seconds",
    "HTTP request latency by method and route template.",
    ("method", "route"),
    buckets=_HTTP_BUCKETS,
    registry=REGISTRY,
)
ARQ_QUEUE_DEPTH = Gauge(
    "umwelt_arq_queue_depth",
    "Jobs waiting in an arq queue (sorted-set cardinality).",
    ("queue",),
    registry=REGISTRY,
)
ARQ_QUEUE_OLDEST_AGE = Gauge(
    "umwelt_arq_queue_oldest_job_age_seconds",
    "Age of the oldest due job in an arq queue (0 when empty).",
    ("queue",),
    registry=REGISTRY,
)
OUTBOX_PENDING = Gauge(
    "umwelt_outbox_pending_count", "Event-outbox rows waiting for delivery.", registry=REGISTRY
)
OUTBOX_LAG = Gauge(
    "umwelt_outbox_oldest_pending_age_seconds",
    "Age of the oldest undelivered outbox row (0 when none).",
    registry=REGISTRY,
)
OUTBOX_ROWS_TOTAL = Gauge(
    "umwelt_outbox_rows_total",
    "Estimated total event_outbox rows (pg_class.reltuples, not an exact count).",
    registry=REGISTRY,
)
OUTBOX_PURGE_BACKLOG = Gauge(
    "umwelt_outbox_purge_backlog",
    "Terminal event_outbox rows already past their retention cutoff (bounded count; 0 when the purge keeps up).",
    registry=REGISTRY,
)
CRON_DURATION = Histogram(
    "umwelt_cron_job_duration_seconds",
    "Scheduled job run time by job name.",
    ("job",),
    buckets=_JOB_BUCKETS,
    registry=REGISTRY,
)
CRON_LAST_SUCCESS = Gauge(
    "umwelt_cron_job_last_success_timestamp_seconds",
    "Unix time of the last successful run of a scheduled job.",
    ("job",),
    registry=REGISTRY,
)
CRON_FAILURES = Counter(
    "umwelt_cron_job_failures_total", "Failed runs of a scheduled job.", ("job",), registry=REGISTRY
)
JOBS = Counter(
    "umwelt_jobs_total",
    "Event-driven background jobs by function name and outcome.",
    ("job", "outcome"),
    registry=REGISTRY,
)
JOB_DURATION = Histogram(
    "umwelt_job_duration_seconds",
    "Event-driven background job run time by function name and outcome.",
    ("job", "outcome"),
    buckets=_JOB_BUCKETS,
    registry=REGISTRY,
)
INGESTION_STAGE = Counter(
    "umwelt_ingestion_stage_total",
    "Ingestion pipeline stage executions by stage and outcome.",
    ("stage", "outcome"),
    registry=REGISTRY,
)
BREAKER_OPEN_UNTIL = Gauge(
    "umwelt_model_breaker_open_until_timestamp_seconds",
    "Unix time until which the provider circuit breaker was last seen open (0 = never seen open).",
    ("provider",),
    registry=REGISTRY,
)
BREAKER_OPENED = Counter(
    "umwelt_model_breaker_rejections_total",
    "Model calls rejected because the provider breaker was open.",
    ("provider",),
    registry=REGISTRY,
)
EVENT_LOOP_LAG = Gauge(
    "umwelt_event_loop_lag_seconds",
    "Most recent event-loop scheduling delay (sleep overshoot).",
    registry=REGISTRY,
)
EVENT_LOOP_LAG_HIST = Histogram(
    "umwelt_event_loop_lag_histogram_seconds",
    "Event-loop scheduling delay distribution.",
    buckets=_LAG_BUCKETS,
    registry=REGISTRY,
)
CHAT_TTFT = Histogram(
    "umwelt_chat_time_to_first_token_seconds",
    "Time from model request to the first streamed token.",
    ("capability",),
    buckets=_TTFT_BUCKETS,
    registry=REGISTRY,
)
SAMPLE_ERRORS = Counter(
    "umwelt_metrics_sample_errors_total",
    "Failed periodic metric samples.",
    ("sampler",),
    registry=REGISTRY,
)

# Ingestion job function -> pipeline stage label (fixed set; job names come from the worker registry).
_INGESTION_STAGES = {
    "process_uploaded_file": "upload",
    "process_normalize_event": "normalize",
    "process_ingestion_event": "ingest",
}
_UNMATCHED_ROUTE = "unmatched"
_SAFE_LABEL = re.compile(r"[^A-Za-z0-9_:./{}\-]")
_MAX_LABEL = 96


def safe_label(value: object) -> str:
    """Return ``value`` as a bounded label string with unexpected characters removed."""
    return _SAFE_LABEL.sub("_", str(value))[:_MAX_LABEL] or "unknown"


def _isolated(call: Callable[[], None]) -> None:
    """Run a metrics action and swallow any failure; metrics must never alter application behaviour."""
    try:
        call()
    except Exception as exc:  # noqa: BLE001 - observability faults are never application errors
        logger.debug("metrics sink failure (%s)", type(exc).__name__)


def record_http(method: str, route: str | None, status: int, seconds: float) -> None:
    """Count one finished HTTP request and observe its latency.

    ``route`` must be the matched route *template* (``/api/v1/items/{item_id}``); ``None`` (no match,
    mounted sub-apps, 404 probes) collapses to one ``unmatched`` series so scanners cannot grow
    cardinality.
    """

    def work() -> None:
        """Update the request counter and latency histogram."""
        label = safe_label(route) if route else _UNMATCHED_ROUTE
        verb = method.upper() if method.upper() in _KNOWN_METHODS else "OTHER"
        HTTP_REQUESTS.labels(verb, label, str(status)).inc()
        HTTP_DURATION.labels(verb, label).observe(max(seconds, 0.0))

    _isolated(work)


_KNOWN_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


def record_job(job: str, outcome: str, seconds: float) -> None:
    """Record an event-driven job run and, for ingestion jobs, the matching pipeline stage outcome."""

    def work() -> None:
        """Update job and ingestion-stage series."""
        name, result = safe_label(job), safe_label(outcome)
        JOBS.labels(name, result).inc()
        JOB_DURATION.labels(name, result).observe(max(seconds, 0.0))
        stage = _INGESTION_STAGES.get(job)
        if stage is not None:
            INGESTION_STAGE.labels(stage, result).inc()

    _isolated(work)


def record_cron(job: str, seconds: float, ok: bool) -> None:
    """Record one scheduled-job run: duration always, last-success time on success, a failure count otherwise."""

    def work() -> None:
        """Update cron series."""
        name = safe_label(job)
        CRON_DURATION.labels(name).observe(max(seconds, 0.0))
        if ok:
            CRON_LAST_SUCCESS.labels(name).set(time.time())
        else:
            CRON_FAILURES.labels(name).inc()

    _isolated(work)


def record_first_token(capability: str, seconds: float) -> None:
    """Observe a streamed model call's time to first token."""
    _isolated(lambda: CHAT_TTFT.labels(safe_label(capability)).observe(max(seconds, 0.0)))


def record_breaker_open(provider: str, remaining_seconds: float, *, rejected: bool = True) -> None:
    """Note that a provider breaker will stay open for ``remaining_seconds`` more.

    ``rejected`` (default) also counts a rejected call; pass False when the breaker was merely observed
    open after a failure.
    """

    def work() -> None:
        """Update breaker series."""
        name = safe_label(provider)
        BREAKER_OPEN_UNTIL.labels(name).set(time.time() + max(remaining_seconds, 0.0))
        if rejected:
            BREAKER_OPENED.labels(name).inc()

    _isolated(work)


def breaker_is_open(provider: str, now: float | None = None) -> bool:
    """Return whether the last-seen open window for ``provider`` has not yet elapsed (test/diagnostic helper)."""
    until = float(BREAKER_OPEN_UNTIL.labels(safe_label(provider))._value.get())
    return until > (time.time() if now is None else now)


# --------------------------------------------------------------------------- DB pools


class _PoolCollector(Collector):
    """Expose size / checked-out / overflow of every live SQLAlchemy engine pool in this process.

    Engines are held weakly so disposed engines (tests, shutdown) disappear without explicit cleanup.
    Reading pool counters is a cheap in-memory operation, so it happens at scrape time.
    """

    def __init__(self) -> None:
        """Create an empty weak registry of engines."""
        self._engines: weakref.WeakSet[Any] = weakref.WeakSet()

    def register(self, engine: Any) -> None:
        """Track ``engine`` (an ``AsyncEngine`` or sync ``Engine``) until it is garbage collected."""
        self._engines.add(engine)

    def collect(self) -> Iterable[GaugeMetricFamily]:
        """Yield one sample per live engine for size, checked-out connections and overflow."""
        families = {
            "size": GaugeMetricFamily(
                "umwelt_db_pool_size", "Configured connections held by the pool.", labels=["pool"]
            ),
            "checked_out": GaugeMetricFamily(
                "umwelt_db_pool_checked_out", "Connections currently checked out.", labels=["pool"]
            ),
            "overflow": GaugeMetricFamily(
                "umwelt_db_pool_overflow",
                "Overflow connections currently open beyond pool size.",
                labels=["pool"],
            ),
            "max": GaugeMetricFamily(
                "umwelt_db_pool_max_connections",
                "Pool size plus allowed overflow.",
                labels=["pool"],
            ),
        }
        # Sorted by id for a stable label order within one scrape; labels are positional indexes.
        for index, engine in enumerate(sorted(self._engines, key=id)):
            pool = getattr(getattr(engine, "sync_engine", engine), "pool", None)
            if pool is None or not hasattr(pool, "checkedout"):
                continue
            label = [str(index)]
            size = int(pool.size())
            overflow = max(
                int(pool.overflow()), 0
            )  # QueuePool reports negative until the pool fills
            families["size"].add_metric(label, size)
            families["checked_out"].add_metric(label, int(pool.checkedout()))
            families["overflow"].add_metric(label, overflow)
            families["max"].add_metric(label, size + int(getattr(pool, "_max_overflow", 0)))
        return families.values()


POOL_COLLECTOR = _PoolCollector()
REGISTRY.register(POOL_COLLECTOR)


def register_engine(engine: Any) -> None:
    """Expose ``engine``'s pool gauges; failure-isolated and idempotent."""
    _isolated(lambda: POOL_COLLECTOR.register(engine))


# --------------------------------------------------------------------------- samplers

Sampler = Callable[[], Awaitable[None]]


async def sample_arq_queues(redis: Any, queues: Iterable[str], *, now: float | None = None) -> None:
    """Sample depth and oldest due-job age of each arq queue sorted set.

    arq stores one ZSET per queue scored by the job's due time in milliseconds. Jobs deferred into
    the future do not count as waiting, so the age is ``max(0, now - score)``. Two O(log n) Redis
    calls per queue; no job payload is read.
    """
    current = time.time() if now is None else now
    for queue in queues:
        depth = int(await redis.zcard(queue))
        oldest = await redis.zrange(queue, 0, 0, withscores=True)
        age = max(0.0, current - float(oldest[0][1]) / 1000.0) if oldest else 0.0
        ARQ_QUEUE_DEPTH.labels(queue).set(depth)
        ARQ_QUEUE_OLDEST_AGE.labels(queue).set(age)


async def run_sampler_loop(samplers: Iterable[tuple[str, Sampler]], interval: float = 15.0) -> None:
    """Run each ``(name, sampler)`` every ``interval`` seconds until cancelled; failures are counted, not raised."""
    named = list(samplers)
    while True:
        for name, sampler in named:
            try:
                await sampler()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a broken sampler must not kill the loop
                logger.debug("metrics sampler %s failed (%s)", name, type(exc).__name__)
                _isolated(SAMPLE_ERRORS.labels(safe_label(name)).inc)
        await asyncio.sleep(interval)


async def monitor_event_loop_lag(interval: float = 0.5) -> None:
    """Measure scheduling delay forever: sleep ``interval`` and record how much later than that the loop resumed."""
    while True:
        started = time.perf_counter()
        await asyncio.sleep(interval)
        lag = max(0.0, time.perf_counter() - started - interval)
        EVENT_LOOP_LAG.set(lag)
        EVENT_LOOP_LAG_HIST.observe(lag)


_background: set[asyncio.Task[None]] = set()


def spawn_background(coro: Awaitable[None], name: str) -> asyncio.Task[None]:
    """Start ``coro`` as a task that is strongly referenced until it ends; callers cancel via ``stop_background``."""
    task = asyncio.ensure_future(coro)
    task.set_name(name)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


async def stop_background(tasks: Iterable[asyncio.Task[None]]) -> None:
    """Cancel and await metric background tasks, ignoring their cancellation."""
    pending = list(tasks)
    for task in pending:
        task.cancel()
    for task in pending:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001, S110
            pass


def start_worker_metrics_server(port: int, bind: str = "127.0.0.1") -> bool:
    """Serve ``REGISTRY`` over plain HTTP on ``port`` in a daemon thread; ``port`` 0 leaves it off.

    Returns whether a server was started. A bind failure (port in use) is logged and returns False so
    a metrics problem never prevents the worker from starting.
    """
    if port <= 0:
        return False
    from prometheus_client import start_http_server

    try:
        start_http_server(port, addr=bind, registry=REGISTRY)
    except OSError as exc:
        logger.warning(
            "worker metrics server could not bind %s:%s (%s)", bind, port, type(exc).__name__
        )
        return False
    return True
