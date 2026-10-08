from collections.abc import Callable
from functools import wraps
from typing import Any, ClassVar, cast
from uuid import uuid4

from arq.connections import RedisSettings
from arq.cron import cron as _cron
from arq.worker import func as _arq_func
from redis.asyncio import Redis
from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from core.auth.models import AuthSession
from core.config import Settings
from core.database import make_session_factory
from core.heavy_work import HEAVY_JOB_MAX_TRIES
from core.modules import register_modules, scheduled_job_owners
from core.system.health import ARQ_WORKER_GENERATION_KEY, ARQ_WORKER_HEALTH_KEY
from core.telemetry import install_log_redaction, instrument_job, set_process_role
from modules.agents.worker import (
    compose_agent_registry,
    process_agent_run,
    reconcile_agent_dispatch,
)
from modules.automations.worker import process_automation_run, reconcile_automation_runs
from modules.chat.worker import (
    CHAT_JOB_TIMEOUT,
    CHAT_QUEUE,
    process_chat_response,
    purge_expired_chat_runs,
    recover_chat_runs,
)
from modules.connectors.worker import reconcile_connectors
from modules.dashboard.worker import run_scheduled_brief, run_scheduled_highlights
from modules.ingestion.dispatcher import WORKER_BY_EVENT, dispatch_pending_work
from modules.ingestion.worker import (
    cleanup_storage_orphans,
    process_ingestion_event,
    process_normalize_event,
    process_uploaded_file,
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
from modules.news.worker import process_news_document_ready, recover_news_work
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

_JOB_OWNERS = scheduled_job_owners(register_modules())


def _gate_module_job(function: Callable[..., Any]) -> Callable[..., Any]:
    """Refresh persisted module availability before dispatching a declared owner job."""
    module_id = _JOB_OWNERS.get(function.__name__)
    if module_id is None:
        return function

    @wraps(function)
    async def guarded(ctx: dict[str, object], *args: Any, **kwargs: Any) -> Any:
        """Leave durable queued work untouched while its owner module is disabled."""
        factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
        from modules.settings.public import read_module_availability

        async with factory() as session:
            lifecycle = await read_module_availability(session)
        if not next((item.enabled for item in lifecycle.modules if item.id == module_id), False):
            return None
        return await function(ctx, *args, **kwargs)

    return guarded


def _gate_backup_job(function: Callable[..., Any]) -> Callable[..., Any]:
    """Durably register worker execution before owner claims or external effects, then close it."""
    @wraps(function)
    async def guarded(ctx: dict[str, object], *args: Any, **kwargs: Any) -> Any:
        """Leave denied durable jobs for their owner recovery poll instead of executing them."""
        factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
        from modules.backup.public import BackupAdmissionDenied
        from modules.settings.public import finish_activity, register_activity

        work_id = str(args[0]) if args and isinstance(args[0], (str, int)) else None
        try:
            async with factory() as session:
                receipt = await register_activity(session, function.__name__, work_id)
                await session.commit()
        except BackupAdmissionDenied:
            # Owner durable rows remain the source of truth; dispatch/recovery polls will retry.
            return None

        try:
            result = await function(ctx, *args, **kwargs)
        except BaseException:
            async with factory() as session:
                # Handler exit only closes this admission lease. Any unresolved external effect
                # must remain visible in its owner's durable journal and block snapshot there.
                await finish_activity(session, receipt)
                await session.commit()
            raise
        async with factory() as session:
            await finish_activity(session, receipt)
            await session.commit()
        return result

    return guarded


def cron(function: Callable[..., Any], *args: Any, **kwargs: Any) -> object:
    """Attach both fresh owner-module and global backup admission checks to worker schedules."""
    return _cron(_gate_backup_job(_gate_module_job(function)), *args, **kwargs)

HEAVY_SLOT_JOBS = frozenset({
    "process_uploaded_file", "process_entity_extraction_work", "process_timeline_extraction_work",
    "process_graph_operation",
})


def _arq_options(name: str) -> dict[str, Any]:
    """Per-function arq options: outbox consumers drop their result, heavy-slot jobs get a long retry budget."""
    options: dict[str, Any] = {}
    if name in set(WORKER_BY_EVENT.values()):
        options["keep_result"] = 0
    if name in HEAVY_SLOT_JOBS:
        options["max_tries"] = HEAVY_JOB_MAX_TRIES
    return options


async def startup(ctx: dict[str, object]) -> None:
    """Load the bounded database pool and compose the worker-owned native/MCP agent registry."""
    settings = Settings()
    ctx["settings"] = settings
    install_log_redaction()
    set_process_role("worker")
    engine, factory = make_session_factory(
        settings.database_url,
        pool_size=WorkerSettings.max_jobs + 1,
        max_overflow=10,
        statement_timeout_ms=0,
        idle_tx_timeout_ms=settings.db_idle_tx_timeout_ms,
    )
    ctx["session_factory"] = factory
    ctx["db_engine"] = engine
    try:
        registry, admission, runtime = await compose_agent_registry(
            settings, cast(async_sessionmaker[AsyncSession], ctx["session_factory"]),
            cast(Redis, ctx["redis"]),
        )
        ctx["agent_tool_registry"] = registry
        ctx["agent_mcp_admission"] = admission
        ctx["agent_mcp_runtime"] = runtime
        await cast(Redis, ctx["redis"]).set(ARQ_WORKER_GENERATION_KEY, uuid4().hex)
    except Exception:
        await engine.dispose()
        raise


async def chat_startup(ctx: dict[str, object]) -> None:
    """Minimal chat-worker startup: bounded DB pool only (no agent registry, no worker-generation key)."""
    settings = Settings()
    ctx["settings"] = settings
    install_log_redaction()
    set_process_role("chat-worker")
    engine, factory = make_session_factory(
        settings.database_url,
        pool_size=10,
        max_overflow=10,
        statement_timeout_ms=60000,
        idle_tx_timeout_ms=settings.db_idle_tx_timeout_ms,
    )
    ctx["session_factory"] = factory
    ctx["db_engine"] = engine


async def shutdown(ctx: dict[str, object]) -> None:
    """Stop new worker MCP admissions and dispose the database engine when present."""
    admission = ctx.get("agent_mcp_admission")
    if admission is not None:
        cast(Any, admission).stop()
    engine = ctx.get("db_engine")
    if engine is not None:
        await cast(AsyncEngine, engine).dispose()


async def purge_expired_sessions(ctx: dict[str, object]) -> int:
    """Delete at most 1000 expired auth sessions per run and commit the cleanup transaction."""
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    async with factory() as session:
        expired = (
            select(AuthSession.token_hash)
            .where(AuthSession.expires_at <= func.now())
            .order_by(AuthSession.expires_at)
            .limit(1000)
        )
        result = cast(
            CursorResult[Any],
            await session.execute(delete(AuthSession).where(AuthSession.token_hash.in_(expired))),
        )
        await session.commit()
        return result.rowcount or 0


class WorkerSettings:
    """ARQ worker registration, recurring schedules, retry/concurrency bounds, and lifecycle hooks."""
    # Queue delay / duration / outcome telemetry wraps only event-driven jobs (labelled by function
    # name); cron polls stay unwrapped so no-op polls record and log nothing.
    functions: ClassVar[list[Any]] = [
        purge_expired_sessions, instrument_job(process_ingestion_event, success_return_outcome="returned"),
        instrument_job(process_normalize_event, success_return_outcome="returned"),
        instrument_job(process_uploaded_file, success_return_outcome="returned"), process_source_purge,
        process_source_memory_coverage, process_document_cleanup,
        reconcile_connectors, instrument_job(process_document_ready, success_return_outcome="returned"),
        instrument_job(process_entity_extraction_work),
        instrument_job(process_timeline_extraction_work), instrument_job(process_graph_operation),
        instrument_job(process_news_document_ready), purge_expired_chat_runs,
        instrument_job(process_agent_run, run_id_kind="agent_run_id"), instrument_job(process_automation_run),
    ]
    functions = [_gate_module_job(function) for function in functions]
    functions = [_gate_backup_job(function) for function in functions]
    # Outbox consumers are re-enqueued under the fixed id `ingestion:{event.id}` per continuation; a
    # kept arq result (default 3600 s) would turn those enqueues into no-ops. Nothing reads arq
    # results, and the in-progress job key still dedupes concurrently queued jobs.
    # Heavy-slot jobs retry on contention; with several job slots the default max_tries would be
    # exhausted (and the job id blocked for keep_result_s) before the slot holder finishes.
    functions = [_arq_func(function, **_arq_options(function.__name__)) if _arq_options(function.__name__)
                 else function for function in functions]
    cron_jobs: ClassVar[list[object]] = [
        cron(purge_expired_sessions, minute=0),
        cron(run_retention_maintenance, minute=0),
        cron(cleanup_storage_orphans, minute=set(range(0, 60, 5))),
        cron(dispatch_pending_work, second=set(range(0, 60, 5)), run_at_startup=True),
        cron(index_pending_chunks, minute=set(range(0, 60, 1))),
        cron(reconcile_connectors, second=set(range(0, 60, 5)), run_at_startup=True),
        cron(reconcile_document_memory_cleanup, second=set(range(0, 60, 5)), run_at_startup=True),
        cron(reconcile_document_agent_cleanup, second=set(range(0, 60, 5)), run_at_startup=True),
        cron(reconcile_document_copied_stage_cleanup, second=set(range(0, 60, 5)), run_at_startup=True),
        cron(reconcile_source_coverage, second={0, 30}, run_at_startup=True),
        cron(recover_entity_extraction_work, minute=set(range(0, 60, 1))),
        cron(recover_timeline_extraction_work, minute=set(range(0, 60, 1))),
        cron(recover_graph_work, second=set(range(0, 60, 5)), run_at_startup=True),
        cron(recover_news_work, minute=set(range(0, 60, 1)), run_at_startup=True),
        cron(run_scheduled_brief, minute=set(range(0, 60, 1)), run_at_startup=True),
        cron(run_scheduled_highlights, minute=set(range(0, 60, 1)), run_at_startup=True),
        cron(purge_expired_chat_runs, minute=set(range(0, 60, 15))),
        cron(reconcile_agent_dispatch, second=set(range(0, 60, 5)), run_at_startup=True),
        cron(reconcile_automation_runs, second=set(range(0, 60, 5)), run_at_startup=True),
    ]
    redis_settings = RedisSettings.from_dsn(Settings().redis_url)

    # Heavy local work (parsing, embedding, extraction, graph, browser reads) is serialized across
    # processes by `core.heavy_work.heavy_job_slot` and retries on contention, so extra slots only
    # let light jobs (cron polls, cleanup stages, chat/agent/model calls bounded by ModelGateway's
    # own slots) run beside a long job instead of queueing behind it. Heavy jobs that lose the slot
    # retry under HEAVY_JOB_MAX_TRIES. Six keeps the DB pool bounded (pool_size = max_jobs + 1).
    max_jobs = 6
    max_tries = 5
    # The graph owner budget is 150 seconds; ARQ must leave time for durable
    # uncertainty publication and cancellation before the 180-second lease ends.
    job_timeout = 180
    health_check_key = ARQ_WORKER_HEALTH_KEY
    health_check_interval = 15
    on_startup = startup
    on_shutdown = shutdown


class ChatWorkerSettings:
    """Dedicated arq worker for chat generation, isolated from the main worker's cron and heavy jobs."""
    # keep_result=0 (worker-level too: arq finish_failed_job ignores the per-function value): neither a
    # gated return nor an interrupted/failed job may block the fixed job id, or recovery could not re-enqueue.
    keep_result = 0
    functions: ClassVar[list[Any]] = [
        _arq_func(_gate_backup_job(_gate_module_job(instrument_job(process_chat_response))), keep_result=0),
    ]
    cron_jobs: ClassVar[list[object]] = [cron(recover_chat_runs, second={0, 15, 30, 45})]
    redis_settings = RedisSettings.from_dsn(Settings().redis_url)
    queue_name = CHAT_QUEUE
    max_jobs = 15  # keeps 5 of the shared 10+10 DB pool free for cron/rerank sessions (pool_timeout 5 s)
    job_timeout = CHAT_JOB_TIMEOUT
    max_tries = 1
    health_check_key = "arq:chat:health-check"
    health_check_interval = 15
    on_startup = chat_startup
    on_shutdown = shutdown
