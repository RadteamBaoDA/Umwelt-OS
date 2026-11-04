"""Owner-protected observability routes under the existing /api/v1/system prefix."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_owner, require_owner_write
from core.auth.models import AuthSession
from core.auth.routes import get_auth_redis
from core.database import get_session
from modules.observability import database_stats, history, public
from modules.observability.schemas import (
    DatabaseStatsRead,
    HistoryRange,
    HistoryRead,
    HistorySettingsRead,
    HistorySettingsUpdate,
    MetricsRead,
    RunKind,
    RunsRead,
)
from modules.settings.public import module_dependency
from modules.settings.schemas import MaintenanceSummaryRead

router = APIRouter(prefix="/api/v1/system", tags=["system"], dependencies=[Depends(module_dependency("observability"))])


@router.get("/metrics", response_model=MetricsRead)
async def read_metrics(
    _owner: Annotated[AuthSession, Depends(require_owner)],
    redis: Annotated[Any, Depends(get_auth_redis)],
) -> MetricsRead:
    """Return merged bounded metrics from live API and worker processes."""
    return await public.read_metrics(redis)


@router.get("/runs", response_model=RunsRead)
async def read_runs(
    session: Annotated[AsyncSession, Depends(get_session)],
    _owner: Annotated[AuthSession, Depends(require_owner)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    kind: RunKind | None = None,
) -> RunsRead:
    """Return the newest runs across ingestion, agents, automations and chat."""
    return await public.list_runs(session, limit=limit, kind=kind, instance_operator=True)


@router.get("/maintenance", response_model=MaintenanceSummaryRead)
async def read_maintenance_summary(
    session: Annotated[AsyncSession, Depends(get_session)],
    _owner: Annotated[AuthSession, Depends(require_owner)],
) -> MaintenanceSummaryRead:
    """Expose the latest durable maintenance result to the authenticated owner."""
    return await public.get_maintenance_summary(session)


@router.get("/history", response_model=HistoryRead)
async def read_history(
    session: Annotated[AsyncSession, Depends(get_session)],
    _owner: Annotated[AuthSession, Depends(require_owner)],
    range: HistoryRange = "24h",
) -> HistoryRead:
    """Return SQL-bucketed system health history for the requested range (422 for unknown ranges)."""
    return await history.read_history(session, range)


@router.get("/history/settings", response_model=HistorySettingsRead)
async def read_history_settings(
    session: Annotated[AsyncSession, Depends(get_session)],
    _owner: Annotated[AuthSession, Depends(require_owner)],
) -> HistorySettingsRead:
    """Return the instance-wide health history retention policy."""
    return await history.read_history_settings(session)


@router.patch("/history/settings", response_model=HistorySettingsRead)
async def patch_history_settings(
    value: HistorySettingsUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    _owner: Annotated[AuthSession, Depends(require_owner_write)],
) -> HistorySettingsRead:
    """Save retention and/or collection interval under origin/CSRF checks; 409 when the revision is stale."""
    result = await history.save_history_settings(
        session, expected_revision=value.expected_revision, retention_days=value.retention_days,
        sample_interval_minutes=value.sample_interval_minutes)
    await session.commit()
    return result


@router.get("/database", response_model=DatabaseStatsRead)
async def read_database_stats(
    session: Annotated[AsyncSession, Depends(get_session)],
    _owner: Annotated[AuthSession, Depends(require_owner)],
    redis: Annotated[Any, Depends(get_auth_redis)],
) -> DatabaseStatsRead:
    """Return read-only Postgres/Redis aggregates (no query text, users, addresses or key names); failed probes are null."""
    return DatabaseStatsRead.model_validate(await database_stats.read_database_stats(session, redis))
