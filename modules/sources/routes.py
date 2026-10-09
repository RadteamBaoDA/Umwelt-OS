"""Workspace-owner Source HTTP boundary; preserve scoped pagination and aggregate purge DTOs."""

from dataclasses import asdict
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_session
from core.workspaces.dependencies import require_workspace_read, require_workspace_write
from core.workspaces.schemas import WorkspaceContext
from modules.settings.public import module_dependency
from modules.sources import public
from modules.sources.schemas import (
    OperationRead,
    SourceCreate,
    SourceImpactRead,
    SourceList,
    SourcePatch,
    SourceRead,
)


async def _with_timing(session: AsyncSession, scope: WorkspaceContext, reads: list[SourceRead]) -> None:
    """Fill next_due_at/retry_at from connector schedule state, scoped to the workspace."""
    from modules.connectors import public as connectors

    timing = await connectors.collection_timing(session, scope, [r.id for r in reads if r.type != "manual"])
    for read in reads:
        read.next_due_at, read.retry_at = timing.get(read.id, (None, None))


router = APIRouter(
    prefix="/api/v1/sources",
    tags=["sources"],
    dependencies=[Depends(module_dependency("sources"))],
)
Session = Annotated[AsyncSession, Depends(get_session)]
WorkspaceRead = Annotated[WorkspaceContext, Depends(require_workspace_read)]
WorkspaceWrite = Annotated[WorkspaceContext, Depends(require_workspace_write)]


def _require_source_owner(scope: WorkspaceContext) -> None:
    """Reject invited members before Source configuration, identities or counts are read."""
    if scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")


@router.get("", response_model=SourceList)
async def list_sources(
    session: Session,
    request: Request,
    scope: WorkspaceRead,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> SourceList:
    """Return <=100 owned-workspace Sources through the principal-bound pagination seam."""
    _require_source_owner(scope)
    items, next_cursor = await public.list_sources(
        session, limit, cursor, scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
    )
    reads = [SourceRead.model_validate(item, from_attributes=True) for item in items]
    await _with_timing(session, scope, reads)
    return SourceList(
        items=reads,
        next_cursor=next_cursor,
    )


@router.post("", response_model=SourceRead, status_code=201)
async def create_source(
    payload: SourceCreate, session: Session, request: Request, scope: WorkspaceWrite,
) -> SourceRead:
    """Create in the selected owner workspace after CSRF/backup/module write admission."""
    _require_source_owner(scope)
    source = await public.create_source(
        session, payload, scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
    )
    return SourceRead.model_validate(source, from_attributes=True)


@router.get("/{source_id}", response_model=SourceRead)
async def get_source(
    source_id: UUID, session: Session, request: Request, scope: WorkspaceRead,
) -> SourceRead:
    """Read one owned-workspace Source; absent/foreign UUIDs preserve the same 404."""
    _require_source_owner(scope)
    source = await public.get_source(
        session, source_id, scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
    )
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    read = SourceRead.model_validate(source, from_attributes=True)
    await _with_timing(session, scope, [read])
    return read


@router.get("/{source_id}/impact", response_model=SourceImpactRead)
async def get_source_impact(
    source_id: UUID, session: Session, request: Request, scope: WorkspaceRead,
) -> SourceImpactRead:
    """Return owner-only dependent counts; 409 while a data purge is pending."""
    _require_source_owner(scope)
    impact = await public.get_source_impact(
        session, source_id, scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
    )
    if impact is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return SourceImpactRead(**asdict(impact))


@router.patch("/{source_id}", response_model=SourceRead)
async def update_source(
    source_id: UUID, payload: SourcePatch, session: Session, request: Request, scope: WorkspaceWrite,
) -> SourceRead:
    """Patch non-null fields in the selected owner workspace, retaining lifecycle conflicts."""
    _require_source_owner(scope)
    source = await public.get_source(
        session, source_id, scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
    )
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if not payload.model_fields_set or any(
        getattr(payload, key) is None for key in payload.model_fields_set
    ):
        raise HTTPException(status_code=422, detail="At least one non-null field is required")
    source = await public.update_source(
        session, source, payload, scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
    )
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return SourceRead.model_validate(source, from_attributes=True)


@router.delete("/{source_id}")
async def delete_source(
    source_id: UUID,
    session: Session,
    request: Request,
    scope: WorkspaceWrite,
    with_data: bool = False,
) -> Response:
    """Archive or queue retained owner-workspace purge; expose only allowlisted aggregate progress."""
    _require_source_owner(scope)
    if with_data:
        operation = await public.start_source_purge(
            session, source_id, scope=scope,
            multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
        )
        if operation is None:
            raise HTTPException(status_code=404, detail="Source not found")
        return JSONResponse(
            status_code=202,
            content=OperationRead(
                operation_id=operation.id,
                workspace_id=operation.workspace_id,
                source_id=operation.source_id,
                status=operation.status,
                error_code=operation.error_code,
                documents_status=operation.documents_status,
                pending_child_count=operation.pending_child_count,
                failed_child_count=operation.failed_child_count,
                pending_owner_codes=operation.pending_owner_codes,
                created_at=operation.created_at,
                updated_at=operation.updated_at,
            ).model_dump(mode="json"),
        )
    source = await public.archive_source(
        session, source_id, scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
    )
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return Response(status_code=204)
