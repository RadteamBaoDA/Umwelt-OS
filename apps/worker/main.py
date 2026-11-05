import asyncio
import logging
import time
from collections.abc import Callable
from datetime import timedelta
from functools import wraps
from typing import Any, ClassVar, cast

from arq.connections import RedisSettings
from arq.constants import default_queue_name
from arq.worker import func as _arq_func
from redis.asyncio import Redis
from sqlalchemy import delete, func, or_, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from apps.worker.cron_schedule import (
    CRON_MAX_TRIES,
    DAY,
    HOUR,
    CronSpec,
    build_cron,
    guarded_function,
)
from core.async_runtime import configure_default_executor
from core.auth.deletion_worker import recover_account_deletions
from core.auth.models import AuthSession
from core.config import Settings
from core.cron import (
    CRON_QUEUE,
    RUN_CRON_FUNCTION,
    clear_wake_flag,
    overdue_jobs,
    read_last_success,
    set_wake_hook,
    stable_offset,
    wake,
)
from core.database import make_session_factory
from core.heavy_work import HEAVY_JOB_MAX_TRIES
from core.ids import uuid7
from core.metrics import (
    monitor_event_loop_lag,
    spawn_background,
    start_worker_metrics_server,
    stop_background,
)
from core.model_gateway.pool import close_pinned_clients
from core.modules import effective_modules, register_modules, scheduled_job_owners
from core.system.health import (
    ARQ_CRON_WORKER_HEALTH_KEY,
    ARQ_WORKER_GENERATION_KEY,
    ARQ_WORKER_HEALTH_KEY,
)
from core.telemetry import install_log_redaction, instrument_job, set_process_role
from core.tracing import configure_tracing, instrument_engine, traced_job
from core.worker_dlq import dead_letter
from core.worker_queues import MODEL_QUEUE
from modules.agents.worker import (
    compose_agent_registry,
    process_agent_run,
    reconcile_agent_dispatch,
)
from modules.automations.worker import process_automation_run, reconcile_automation_runs
from modules.chat.user_input import recover_parked_input_runs
from modules.chat.worker import (
    CHAT_JOB_TIMEOUT,
    CHAT_QUEUE,
    process_chat_response,
    purge_expired_chat_runs,
    recover_chat_runs,
)
from modules.connectors.scheduler import dispatch_due_collections
from modules.connectors.worker import process_collection_request, reconcile_connectors
from modules.dashboard.worker import run_scheduled_brief, run_scheduled_highlights
from modules.ingestion.dispatcher import WORKER_BY_EVENT, dispatch_pending_work
from modules.ingestion.worker import (
    cleanup_storage_orphans,
    process_ingestion_event,
    process_normalize_event,
    process_uploaded_file,
    purge_collection_receipts,
)
from modules.knowledge.documents.worker import (
    process_document_cleanup,
    reconcile_document_agent_cleanup,
    reconcile_document_copied_stage_cleanup,
    reconcile_document_memory_cleanup,
)
from modules.knowledge.entities.worker import (
    process_document_ready,
    process_entity_extraction_work,
    recover_entity_extraction_work,
)
from modules.knowledge.temporal.worker import process_graph_operation, recover_graph_work
from modules.news.backfill_attribution import backfill_news_attribution
from modules.news.worker import process_news_document_ready, recover_news_work
from modules.observability.history import sample_system_health
from modules.observability.maintenance import run_retention_maintenance
from modules.search.indexing import index_pending_chunks
from modules.sources.worker import (
    process_source_memory_coverage,
    process_source_purge,
    reconcile_source_coverage,
)
from modules.timeline.worker import (
    process_timeline_extraction_work,
    recover_timeline_extraction_work,
)
from modules.translations.worker import (
    expire_translations,
    recover_translation_jobs,
    sweep_translation_orphans,
    translate_content,
)

_JOB_OWNERS = scheduled_job_owners(register_modules())
_BUILD_AVAILABLE = frozenset(module_id for module_id, item in effective_modules(()).items() if item.enabled)


def _gate_module_job(function: Callable[..., Any]) -> Callable[..., Any]:
    """Skip a declared owner job only when its module is not part of this build.

    Availability is the static descriptor registry (with dependency closure); no database read
    happens here. Per-workspace enablement belongs to each converted worker entrypoint, which
    calls ``module_is_enabled`` under its own workspace scope and leaves durable work untouched.
    """
    module_id = _JOB_OWNERS.get(function.__name__)
    if module_id is None:
        return function

    @wraps(function)
    async def guarded(ctx: dict[str, object], *args: Any, **kwargs: Any) -> Any:
        """Leave durable queued work untouched while its owner module is unavailable in this build."""
        if module_id not in _BUILD_AVAILABLE:
            return None
        return await function(ctx, *args, **kwargs)

    return guarded


HEAVY_SLOT_JOBS = frozenset({"process_uploaded_file", "process_graph_operation"})


def _arq_options(name: str) -> dict[str, Any]:
    """Per-function arq options: outbox consumers drop their result, heavy-slot jobs get a long retry budget."""
    options: dict[str, Any] = {}
    if name == "translate_content":
        options["keep_result"] = 0  # fixed `translation:{id}` job ids must be re-enqueueable by recovery
    if name in set(WORKER_BY_EVENT.values()):
        options["keep_result"] = 0
    if name in HEAVY_SLOT_JOBS:
        options["max_tries"] = HEAVY_JOB_MAX_TRIES
    return options


def _silence_cron_logs() -> None:
    """Show arq job lifecycle logs only for warnings/errors (cron jobs fire a dozen times every 5 s)."""
    logging.getLogger("arq.worker").setLevel(logging.WARNING)


# Offset added to METRICS_WORKER_PORT per worker process so all four can share one host / network
# namespace; mirrored in docker-compose.monitoring.yml, infrastructure/monitoring/prometheus.yml and
# docs/observability.md.
METRICS_PORT_OFFSETS = {"worker": 0, "chat-worker": 1, "model-worker": 2, "cron-worker": 3}


def _install_observability(ctx: dict[str, object], settings: Settings, engine: AsyncEngine, role: str) -> None:
    """Start optional worker metrics and tracing; both are off unless configured and never block startup.

    The metrics HTTP server binds ``METRICS_WORKER_PORT + METRICS_PORT_OFFSETS[role]`` (worker 0, chat
    worker 1, model worker 2, cron worker 3) so the worker processes can coexist on one host. The
    event-loop lag task is stored in ``ctx["metrics_tasks"]`` for cancellation in ``shutdown``. Tracing
    instruments this process's engine.
    """
    if settings.metrics_worker_port and start_worker_metrics_server(
        settings.metrics_worker_port + METRICS_PORT_OFFSETS[role], settings.metrics_worker_bind,
    ):
        ctx["metrics_tasks"] = [spawn_background(monitor_event_loop_lag(), f"{role}-loop-lag")]
    if configure_tracing(
        settings.otel_exporter_otlp_endpoint, service_name=f"umwelt-{role}",
        sample_ratio=settings.otel_traces_sample_ratio,
    ):
        instrument_engine(engine)


async def _boot_runtime(ctx: dict[str, object], *, role: str, pool_size: int, max_overflow: int) -> None:
    """Shared startup for the processes that run the full job ctx (main worker and cron worker).

    Installs settings, the shared dispatch/cursor state, log redaction, the bounded database pool,
    optional metrics/tracing for ``role``, the worker-owned agent registry and the event-driven wake
    hook. When registry composition fails the metrics tasks are stopped and the engine is disposed
    before the error propagates. Nested state is neither authority nor durable and resets on restart.
    """
    _silence_cron_logs()
    configure_default_executor()  # explicit default thread-pool size; see core.async_runtime
    settings = Settings()
    ctx["settings"] = settings
    ctx["ingestion_dispatch_scan_state"] = {"cursor": None, "lock": asyncio.Lock()}
    ctx["w2_cursor_state"] = {}  # dict[str,str] shared across ARQ ctx copies; see core/worker_cursors.py for key namespaces (_gen:/_unsynced:)
    install_log_redaction()
    set_process_role(role)
    engine, factory = make_session_factory(
        settings.database_url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        statement_timeout_ms=0,
        idle_tx_timeout_ms=settings.db_idle_tx_timeout_ms,
    )
    ctx["session_factory"] = factory
    ctx["db_engine"] = engine
    _install_observability(ctx, settings, engine, role)
    try:
        registry, admission, runtime = await compose_agent_registry(
            settings, cast(async_sessionmaker[AsyncSession], ctx["session_factory"]),
            cast(Redis, ctx["redis"]),
        )
        ctx["agent_tool_registry"] = registry
        ctx["agent_mcp_admission"] = admission
        ctx["agent_mcp_runtime"] = runtime
    except Exception:
        await stop_background(cast("list[asyncio.Task[None]]", ctx.pop("metrics_tasks", [])))
        await engine.dispose()
        raise
    redis = cast(Redis, ctx["redis"])

    async def wake_poller(job_name: str, defer_seconds: float = 1.0) -> None:
        """Enqueue an immediate run of a poller cron after a worker-side commit (best-effort)."""
        await wake(redis, job_name, defer_seconds=defer_seconds)

    set_wake_hook(wake_poller)


async def startup(ctx: dict[str, object]) -> None:
    """Load the bounded database pool and compose the worker-owned native/MCP agent registry.

    Keep one nested checkpoint and bounded-discovery lock shared across ARQ's copied
    invocation contexts; neither is authority or durable, and both reset on restart. Publishes a new
    worker generation for health once composition succeeded.
    """
    await _boot_runtime(ctx, role="worker", pool_size=WorkerSettings.max_jobs + 1, max_overflow=10)
    await cast(Redis, ctx["redis"]).set(ARQ_WORKER_GENERATION_KEY, uuid7().hex)


CRON_DB_POOL = (8 + 1, 5)  # (pool_size = CronWorkerSettings.max_jobs + 1, max_overflow); counted as 14 in docs/deployment.md


async def catch_up_missed_runs(ctx: dict[str, object]) -> list[str]:
    """Enqueue one lease-guarded run of every non-startup cron whose last success is older than 2x its period.

    Reads the persisted ``cron:last_success`` hash; jobs that never succeeded count as overdue. Runs are
    deferred by a stable 0-29 s offset so a restart does not stampede, use the job id
    ``catchup:<name>:<minute>`` (no duplicates across replicas) and pass through the same single-flight
    lease as scheduled runs via ``run_cron_job``. Returns the enqueued names; enqueue failures are logged and skipped.
    """
    redis = cast(Redis, ctx["redis"])
    periods = {spec.name: spec.period_seconds for spec in CRON_SPECS if not spec.run_at_startup}
    queued: list[str] = []
    bucket = int(time.time() // 60)  # replicas restarting together coalesce; the lease guards the rest
    for name in overdue_jobs(periods, await read_last_success(redis)):
        try:
            job = await cast(Any, redis).enqueue_job(
                RUN_CRON_FUNCTION, name, False, _job_id=f"catchup:{name}:{bucket}", _queue_name=CRON_QUEUE,
                _defer_by=stable_offset(name, 30, "catch-up"),
            )
        except Exception:
            logging.getLogger(__name__).warning("cron catch-up enqueue failed job=%s", name, exc_info=True)
            continue
        if job is not None:
            queued.append(name)
    return queued


async def cron_startup(ctx: dict[str, object]) -> None:
    """Cron-worker startup: full job ctx on its own bounded pool, then missed-run catch-up."""
    await _boot_runtime(ctx, role="cron-worker", pool_size=CRON_DB_POOL[0], max_overflow=CRON_DB_POOL[1])
    await catch_up_missed_runs(ctx)


CHAT_DB_POOL = (10, 10)  # fixed (pool_size, max_overflow); counted as 20 in docs/deployment.md and .env.example


async def chat_startup(ctx: dict[str, object]) -> None:
    """Minimal chat-worker startup: bounded DB pool only (no agent registry, no worker-generation key)."""
    _silence_cron_logs()
    configure_default_executor()  # explicit default thread-pool size; see core.async_runtime
    settings = Settings()
    ctx["settings"] = settings
    install_log_redaction()
    set_process_role("chat-worker")
    engine, factory = make_session_factory(
        settings.database_url,
        pool_size=CHAT_DB_POOL[0],
        max_overflow=CHAT_DB_POOL[1],
        statement_timeout_ms=settings.db_statement_timeout_ms,
        idle_tx_timeout_ms=settings.db_idle_tx_timeout_ms,
    )
    ctx["session_factory"] = factory
    ctx["db_engine"] = engine
    _install_observability(ctx, settings, engine, "chat-worker")


MODEL_DB_POOL = (4 + 1, 5)  # (pool_size = ModelWorkerSettings.max_jobs + 1, max_overflow); counted as 10 in docs/deployment.md


async def model_startup(ctx: dict[str, object]) -> None:
    """Minimal model-worker startup: bounded DB pool only (no agent registry, no worker-generation key)."""
    _silence_cron_logs()
    configure_default_executor()  # explicit default thread-pool size; see core.async_runtime
    settings = Settings()
    ctx["settings"] = settings
    install_log_redaction()
    set_process_role("model-worker")
    engine, factory = make_session_factory(
        settings.database_url,
        pool_size=MODEL_DB_POOL[0],
        max_overflow=MODEL_DB_POOL[1],
        statement_timeout_ms=0,  # long model calls hold no DB statement; mirrors the main worker
        idle_tx_timeout_ms=settings.db_idle_tx_timeout_ms,
    )
    ctx["session_factory"] = factory
    ctx["db_engine"] = engine
    _install_observability(ctx, settings, engine, "model-worker")


async def shutdown(ctx: dict[str, object]) -> None:
    """Release every worker process's runtime in dependency order (all four worker settings share this hook).

    Clears the wake hook, cancels the metrics tasks, stops new worker MCP admissions, closes the pooled
    model-gateway HTTP clients and finally disposes the database engine when present; the engine is
    disposed even when closing the HTTP clients fails.
    """
    set_wake_hook(None)
    await stop_background(cast("list[asyncio.Task[None]]", ctx.get("metrics_tasks", [])))
    admission = ctx.get("agent_mcp_admission")
    if admission is not None:
        cast(Any, admission).stop()
    try:
        await close_pinned_clients()  # pooled model-gateway HTTP clients
    finally:
        engine = ctx.get("db_engine")
        if engine is not None:
            await cast(AsyncEngine, engine).dispose()


async def purge_expired_sessions(ctx: dict[str, object]) -> int:
    """Delete at most 1000 absolute-expired or idle-expired auth sessions per run and commit the cleanup transaction."""
    settings = ctx.get("settings")
    idle_hours = getattr(settings, "session_idle_hours", 8)
    idle_cutoff = func.now() - timedelta(hours=idle_hours if isinstance(idle_hours, int) else 8)
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    async with factory() as session:
        expired = (
            select(AuthSession.token_hash)
            .where(or_(
                AuthSession.expires_at <= func.now(),
                func.coalesce(AuthSession.last_seen_at, AuthSession.created_at) <= idle_cutoff,
            ))
            .order_by(AuthSession.expires_at)
            .limit(1000)
        )
        result = cast(
            CursorResult[Any],
            await session.execute(delete(AuthSession).where(AuthSession.token_hash.in_(expired))),
        )
        await session.commit()
        return result.rowcount or 0


def _register(
    function: Callable[..., Any], *, max_tries: int, timeout: float, queue: str = default_queue_name,
) -> Any:
    """Wrap a job with its dead-letter recorder and trace span, then apply per-function arq options.

    ``traced_job`` is outermost so the producer's ``_trace_carrier`` kwarg is stripped before the
    dead-letter recorder hashes the arguments, and the failure bookkeeping runs inside the job span.
    The effective try limit is the per-function override (heavy-slot jobs) or the worker default
    ``max_tries``; every attempt after which arq will not run the job again (permanent failure, timeout,
    or exhausted tries) is persisted to ``worker_dead_letters`` (see
    ``core.worker_dlq``). Returns the arq ``Function`` when options apply, else the wrapped coroutine.
    """
    options = _arq_options(function.__name__)
    wrapped = traced_job(dead_letter(
        function, max_tries=int(options.get("max_tries", max_tries)), queue=queue, timeout_seconds=timeout,
    ))
    return _arq_func(wrapped, **options) if options else wrapped


# Every recurring job with its period. Phases inside the period come from a stable hash of the job
# name (apps/worker/cron_schedule.py), so no more than a few jobs share a second. Pollers whose work is
# also triggered by an enqueue (dispatch_pending_work via the outbox commit hook, index_pending_chunks
# via the same hook's ready events) keep a slow safety-net period instead of 5 s.
CRON_SPECS: tuple[CronSpec, ...] = (
    CronSpec(purge_expired_sessions, HOUR),
    CronSpec(recover_account_deletions, 30, run_at_startup=True),
    CronSpec(purge_collection_receipts, HOUR, minute=7),
    CronSpec(run_retention_maintenance, HOUR),
    CronSpec(sample_system_health, 60),
    CronSpec(cleanup_storage_orphans, 300),
    CronSpec(dispatch_pending_work, 30, run_at_startup=True),
    CronSpec(index_pending_chunks, 60),
    CronSpec(reconcile_connectors, 5, run_at_startup=True),
    CronSpec(dispatch_due_collections, 15, run_at_startup=True),
    CronSpec(reconcile_document_memory_cleanup, 5, run_at_startup=True),
    CronSpec(reconcile_document_agent_cleanup, 5, run_at_startup=True),
    CronSpec(reconcile_document_copied_stage_cleanup, 5, run_at_startup=True),
    CronSpec(reconcile_source_coverage, 30, run_at_startup=True),
    CronSpec(recover_entity_extraction_work, 60),
    CronSpec(recover_timeline_extraction_work, 60),
    CronSpec(recover_graph_work, 5, run_at_startup=True),
    CronSpec(recover_news_work, 60, run_at_startup=True),
    CronSpec(backfill_news_attribution, 1800, run_at_startup=True),
    CronSpec(run_scheduled_brief, 60, run_at_startup=True),
    CronSpec(run_scheduled_highlights, 60, run_at_startup=True),
    CronSpec(purge_expired_chat_runs, 900),
    CronSpec(reconcile_agent_dispatch, 5, run_at_startup=True),
    CronSpec(reconcile_automation_runs, 5, run_at_startup=True),
    CronSpec(recover_translation_jobs, 60),
    CronSpec(sweep_translation_orphans, 300),
    CronSpec(expire_translations, DAY, hour=3, minute=17),
)
CHAT_CRON_SPECS: tuple[CronSpec, ...] = (
    CronSpec(recover_chat_runs, 15),
    CronSpec(recover_parked_input_runs, 30),
)


WORKER_JOB_TIMEOUT = 180
MODEL_JOB_TIMEOUT = Settings().model_worker_job_timeout_seconds


class WorkerSettings:
    """ARQ worker registration, recurring schedules, retry/concurrency bounds, and lifecycle hooks."""
    # Queue delay / duration / outcome telemetry wraps only event-driven jobs (labelled by function
    # name); cron polls stay unwrapped so no-op polls record and log nothing.
    functions: ClassVar[list[Any]] = [
        purge_expired_sessions, instrument_job(process_ingestion_event, success_return_outcome="returned"),
        instrument_job(process_normalize_event, success_return_outcome="returned"),
        instrument_job(process_uploaded_file, success_return_outcome="returned"), process_source_purge,
        process_source_memory_coverage, process_document_cleanup,
        reconcile_connectors, dispatch_due_collections, process_collection_request,
        instrument_job(process_document_ready, success_return_outcome="returned"),
        instrument_job(process_graph_operation),
        instrument_job(process_news_document_ready), purge_expired_chat_runs,
        instrument_job(process_agent_run, run_id_kind="agent_run_id"), instrument_job(process_automation_run),
    ]
    functions = [_register(_gate_module_job(function), max_tries=5, timeout=WORKER_JOB_TIMEOUT) for function in functions]
    # Outbox consumers are re-enqueued under the fixed id `ingestion:{event.id}` per continuation; a
    # kept arq result (default 3600 s) would turn those enqueues into no-ops. Nothing reads arq
    # results, and the in-progress job key still dedupes concurrently queued jobs.
    # Heavy-slot jobs retry on contention; with several job slots the default max_tries would be
    # exhausted (and the job id blocked for keep_result_s) before the slot holder finishes.
    # Cron jobs run on CronWorkerSettings (queue arq:cron); only event-driven functions stay here.
    redis_settings = RedisSettings.from_dsn(Settings().redis_url)

    # Heavy local work (parsing, embedding, graph, browser reads) is serialized across
    # processes by `core.heavy_work.heavy_job_slot` and retries on contention, so extra slots only
    # let light jobs (cron polls, cleanup stages, agent/model calls bounded by ModelGateway's
    # own slots) run beside a long job instead of queueing behind it. Entity/timeline extraction
    # and translation run on ModelWorkerSettings (queue arq:model), not here. Heavy jobs that lose the slot
    # retry under HEAVY_JOB_MAX_TRIES. Six keeps the DB pool bounded (pool_size = max_jobs + 1).
    max_jobs = 6
    max_tries = 5
    # The graph owner budget is 150 seconds; ARQ must leave time for durable
    # uncertainty publication and cancellation before the 180-second lease ends.
    job_timeout = WORKER_JOB_TIMEOUT
    health_check_key = ARQ_WORKER_HEALTH_KEY
    health_check_interval = 15
    # Graceful drain: on SIGTERM stop taking new jobs and let running ones finish for up to
    # job_timeout + 5 s before cancelling (cancelled jobs are retried). compose stop_grace_period must
    # exceed this (240 s); see docs/deployment.md.
    job_completion_wait = job_timeout + 5
    on_startup = startup
    on_shutdown = shutdown


_CRON_GUARDED: dict[str, Callable[..., Any]] = {
    spec.name: guarded_function(spec, _gate_module_job) for spec in CRON_SPECS
}


async def run_cron_job(ctx: dict[str, Any], job_name: str, is_wake: bool = True) -> Any:
    """Run the named cron job once through its single-flight lease (wake-up and catch-up entry point).

    A plain arq function, deliberately not a ``CronJob``: arq keeps CronJob in-progress keys for 60 s and
    fixed job ids block on kept results, which made wake-ups slower than the safety-net poll. Wakes and
    catch-ups enqueue this with a unique job id instead. ``is_wake`` marks a wake-up so that meeting a
    held lease leaves a rerun request for the holder. Clears the wake debounce flag first so commits that
    land during the run queue the next wake. Unknown names are ignored (stale job from another build).
    """
    function = _CRON_GUARDED.get(job_name)
    if function is None:
        return None
    redis = ctx.get("redis")
    if is_wake and redis is not None:
        await clear_wake_flag(redis, job_name)
    return await function({**ctx, "cron_wake": is_wake})


class CronWorkerSettings:
    """Dedicated arq worker for every recurring job, so polls never occupy ingestion ``max_jobs`` slots.

    Cron jobs are registered here only (WorkerSettings keeps the event-driven consumers). Each job is
    lease-guarded (``core.cron.single_flight``) with a hashed phase; wake-ups and missed-run catch-up are
    enqueued on ``CRON_QUEUE`` under the ``cron:<function name>`` job name this worker registers. Heavy
    work inside a cron still serializes through ``core.heavy_work.heavy_job_slot`` across processes.
    """
    # traced_job strips the ``_trace_carrier`` that a tracing API adds to wake-up enqueues.
    functions: ClassVar[list[Any]] = [_arq_func(traced_job(run_cron_job), max_tries=CRON_MAX_TRIES)]
    cron_jobs: ClassVar[list[object]] = [build_cron(spec, _gate_module_job) for spec in CRON_SPECS]
    redis_settings = RedisSettings.from_dsn(Settings().redis_url)
    queue_name = CRON_QUEUE
    max_jobs = 8
    max_tries = CRON_MAX_TRIES  # only ``Retry`` (heavy-slot contention) consumes tries; other errors never rerun
    # No kept results: cron/wake outcomes live in core.cron bookkeeping; a stored failed result (default
    # 3600 s) would otherwise block re-enqueueing under the same id.
    keep_result = 0
    job_timeout = 180
    health_check_key = ARQ_CRON_WORKER_HEALTH_KEY
    health_check_interval = 15
    job_completion_wait = job_timeout + 5
    on_startup = cron_startup
    on_shutdown = shutdown


class ModelWorkerSettings:
    """Dedicated arq worker for remote-model background jobs (entity/timeline extraction, translation).

    Isolated from the main worker so slow provider calls never hold its job slots or the
    cross-process heavy slot that chunk embedding needs. Recovery crons run on CronWorkerSettings and
    only enqueue onto this queue.
    """
    functions: ClassVar[list[Any]] = [
        process_entity_extraction_work, process_timeline_extraction_work, translate_content,
    ]
    functions = [_register(_gate_module_job(instrument_job(function)), max_tries=5, timeout=MODEL_JOB_TIMEOUT, queue=MODEL_QUEUE)
                 for function in functions]
    redis_settings = RedisSettings.from_dsn(Settings().redis_url)
    queue_name = MODEL_QUEUE
    max_jobs = 4
    job_timeout = MODEL_JOB_TIMEOUT
    max_tries = 5
    health_check_key = "arq:model:health-check"
    health_check_interval = 15
    job_completion_wait = job_timeout + 5  # compose stop_grace_period must exceed this; see docs/deployment.md
    on_startup = model_startup
    on_shutdown = shutdown


class ChatWorkerSettings:
    """Dedicated arq worker for chat generation, isolated from the main worker's cron and heavy jobs."""
    # keep_result=0 (worker-level too: arq finish_failed_job ignores the per-function value): neither a
    # gated return nor an interrupted/failed job may block the fixed job id, or recovery could not re-enqueue.
    keep_result = 0
    functions: ClassVar[list[Any]] = [
        _arq_func(traced_job(_gate_module_job(instrument_job(process_chat_response))), keep_result=0),
    ]
    cron_jobs: ClassVar[list[object]] = [build_cron(spec, _gate_module_job) for spec in CHAT_CRON_SPECS]
    redis_settings = RedisSettings.from_dsn(Settings().redis_url)
    queue_name = CHAT_QUEUE
    max_jobs = 15  # keeps 5 of the shared 10+10 DB pool free for cron/rerank sessions (pool_timeout 5 s)
    job_timeout = CHAT_JOB_TIMEOUT
    max_tries = 1
    health_check_key = "arq:chat:health-check"
    health_check_interval = 15
    job_completion_wait = job_timeout + 5  # compose stop_grace_period must exceed this; see docs/deployment.md
    on_startup = chat_startup
    on_shutdown = shutdown
