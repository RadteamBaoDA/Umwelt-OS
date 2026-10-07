"""Owner-protected observability routes under the existing /api/v1/system prefix."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_owner
from core.auth.models import AuthSession
from core.auth.routes import get_auth_redis
from core.database import get_session
from modules.observability import public
from modules.observability.schemas import MetricsRead, RunKind, RunsRead
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
    return await public.list_runs(session, limit=limit, kind=kind)


@router.get("/maintenance", response_model=MaintenanceSummaryRead)
async def read_maintenance_summary(
    session: Annotated[AsyncSession, Depends(get_session)],
    _owner: Annotated[AuthSession, Depends(require_owner)],
) -> MaintenanceSummaryRead:
    """Expose the latest durable maintenance result to the authenticated owner."""
    return await public.get_maintenance_summary(session)
