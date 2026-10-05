from typing import Any, ClassVar, cast

from arq.connections import RedisSettings
from arq.cron import cron
from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from redis.asyncio import Redis

from core.auth.models import AuthSession
from core.config import Settings
from core.system.health import ARQ_WORKER_HEALTH_KEY
from modules.ingestion.dispatcher import dispatch_pending_work
from modules.ingestion.worker import (
    cleanup_storage_orphans,
    process_normalize_event,
    process_ingestion_event,
    process_uploaded_file,
)
from modules.sources.worker import process_source_purge
from modules.search.indexing import index_pending_chunks
from modules.connectors.worker import reconcile_connectors
from modules.knowledge.entities.worker import (
    process_document_ready,
    process_entity_extraction_work,
    recover_entity_extraction_work,
)
from modules.timeline.worker import process_timeline_extraction_work, recover_timeline_extraction_work
from modules.knowledge.temporal.worker import process_graph_operation, recover_graph_work
from modules.news.worker import process_news_document_ready, recover_news_work
from modules.dashboard.worker import run_scheduled_brief
from modules.chat.worker import process_chat_response, purge_expired_chat_runs
from modules.agents.worker import compose_agent_registry, process_agent_run, reconcile_agent_dispatch
from modules.automations.worker import process_automation_run, reconcile_automation_runs

async def startup(ctx: dict[str, object]) -> None:
    """Load the bounded database pool and compose the worker-owned native/MCP agent registry."""
    settings = Settings()
    ctx["settings"] = settings
    engine = create_async_engine(settings.database_url, pool_pre_ping=True, pool_size=2)
    ctx["session_factory"] = async_sessionmaker(engine, expire_on_commit=False)
    ctx["db_engine"] = engine
    try:
        registry, admission, runtime = await compose_agent_registry(
            settings, cast(async_sessionmaker[AsyncSession], ctx["session_factory"]),
            cast(Redis, ctx["redis"]),
        )
        ctx["agent_tool_registry"] = registry
        ctx["agent_mcp_admission"] = admission
        ctx["agent_mcp_runtime"] = runtime
    except Exception:
        await engine.dispose()
        raise


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
    functions: ClassVar[list[object]] = [
        purge_expired_sessions, process_ingestion_event, process_normalize_event, process_uploaded_file, process_source_purge,
        reconcile_connectors, process_document_ready, process_entity_extraction_work, process_timeline_extraction_work,
        process_graph_operation, process_news_document_ready, process_chat_response, purge_expired_chat_runs,
        process_agent_run, process_automation_run,
    ]
    cron_jobs: ClassVar[list[object]] = [
        cron(purge_expired_sessions, minute=0),
        cron(cleanup_storage_orphans, minute=set(range(0, 60, 5))),
        cron(dispatch_pending_work, second=set(range(0, 60, 5)), run_at_start=True),
        cron(index_pending_chunks, minute=set(range(0, 60, 1))),
        cron(reconcile_connectors, second=set(range(0, 60, 5)), run_at_start=True),
        cron(recover_entity_extraction_work, minute=set(range(0, 60, 1))),
        cron(recover_timeline_extraction_work, minute=set(range(0, 60, 1))),
        cron(recover_graph_work, second=set(range(0, 60, 5)), run_at_start=True),
        cron(recover_news_work, minute=set(range(0, 60, 1)), run_at_start=True),
        cron(run_scheduled_brief, minute=set(range(0, 60, 1)), run_at_start=True),
        cron(purge_expired_chat_runs, minute=set(range(0, 60, 15))),
        cron(reconcile_agent_dispatch, second=set(range(0, 60, 5)), run_at_start=True),
        cron(reconcile_automation_runs, second=set(range(0, 60, 5)), run_at_start=True),
    ]
    redis_settings = RedisSettings.from_dsn(Settings().redis_url)

    max_jobs = 1
    max_tries = 5
    # The graph owner budget is 150 seconds; ARQ must leave time for durable
    # uncertainty publication and cancellation before the 180-second lease ends.
    job_timeout = 180
    health_check_key = ARQ_WORKER_HEALTH_KEY
    health_check_interval = 15
    on_startup = startup
    on_shutdown = shutdown
