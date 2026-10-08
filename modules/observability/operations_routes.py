"""Owner-only bounded operational summaries used by Settings screens."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_owner
from core.auth.models import AuthSession
from core.database import get_session
from modules.observability import public as observability
from modules.observability.operations import quality_summary, queue_summary
from modules.observability.schemas import RunKind, RunRead
from modules.settings.public import module_dependency

router = APIRouter(prefix="/api/v1/system", tags=["system"], dependencies=[Depends(module_dependency("observability"))])


@router.get("/quality")
async def read_quality(
    session: Annotated[AsyncSession, Depends(get_session)],
    _owner: Annotated[AuthSession, Depends(require_owner)],
) -> dict[str, object]:
    """Return aggregate, content-free data quality counts."""
    return await quality_summary(session, instance_operator=True)


@router.get("/queue")
async def read_queue(
    session: Annotated[AsyncSession, Depends(get_session)],
    _owner: Annotated[AuthSession, Depends(require_owner)],
) -> dict[str, object]:
    """Return durable state counts only; event payloads and job arguments never leave PostgreSQL."""
    return await queue_summary(session, instance_operator=True)


@router.get("/runs/{kind}/{run_id}", response_model=RunRead)
async def read_run_detail(
    kind: RunKind,
    run_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _owner: Annotated[AuthSession, Depends(require_owner)],
) -> RunRead:
    """Return one safe run projection by exact owner-module ID lookup."""
    result = await observability.get_run_by_id(session, kind, run_id, instance_operator=True)
    if result is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return result
