"""Owner-only bounded operational summaries used by Settings screens."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_owner
from core.auth.models import AuthSession
from core.database import get_session
from modules.observability import public as observability
from modules.observability.operations import quality_summary, queue_summary
from modules.observability.schemas import RunKind, RunRead
from modules.settings.public import module_dependency
from modules.sources import public as sources
from modules.sources.schemas import OperationRead

router = APIRouter(prefix="/api/v1/system", tags=["system"], dependencies=[Depends(module_dependency("observability"))])


@router.get("/operations/{operation_id}", response_model=OperationRead)
async def read_source_operation(
    operation_id: UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    _owner: Annotated[AuthSession, Depends(require_owner)],
) -> OperationRead:
    """Return only the Sources public projection for a polled purge operation ID.

    This route exists for the source-list progress poller. It delegates the exact lookup to
    the Sources owner and does not expose generic outbox payloads or other operation models.
    The retained job's durable scope is resolved first; an unknown or invalid lineage is 404.
    """
    enabled = request.app.state.settings.multi_workspace_enabled
    scope = await sources.resolve_source_purge_job_scope(session, operation_id, multi_workspace_enabled=enabled)
    if scope is None:
        raise HTTPException(status_code=404, detail="Operation not found")
    operation = await sources.read_source_purge_operation(
        session, operation_id, scope=scope, multi_workspace_enabled=enabled,
    )
    if operation is None:
        raise HTTPException(status_code=404, detail="Operation not found")
    return operation


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
