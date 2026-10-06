from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_owner, require_owner_write
from core.auth.models import AuthSession
from core.database import get_session
from modules.sources import public
from modules.sources.schemas import OperationRead, SourceCreate, SourceList, SourcePatch, SourceRead
from modules.settings.public import module_dependency

router = APIRouter(
    prefix="/api/v1/sources",
    tags=["sources"],
    dependencies=[Depends(module_dependency("sources"))],
)
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[AuthSession, Depends(require_owner)]
OwnerWrite = Annotated[AuthSession, Depends(require_owner_write)]


@router.get("", response_model=SourceList)
async def list_sources(
    session: Session,
    _owner: OwnerRead,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> SourceList:
    """Return a bounded owner-only source page with its continuation cursor."""
    items, next_cursor = await public.list_sources(session, limit, cursor)
    return SourceList(
        items=[SourceRead.model_validate(item, from_attributes=True) for item in items],
        next_cursor=next_cursor,
    )


@router.post("", response_model=SourceRead, status_code=201)
async def create_source(payload: SourceCreate, session: Session, _owner: OwnerWrite) -> SourceRead:
    """Create a source under owner write authorization."""
    source = await public.create_source(session, payload)
    return SourceRead.model_validate(source, from_attributes=True)


@router.get("/{source_id}", response_model=SourceRead)
async def get_source(source_id: UUID, session: Session, _owner: OwnerRead) -> SourceRead:
    """Return one owner-only source or 404 when absent."""
    source = await public.get_source(session, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return SourceRead.model_validate(source, from_attributes=True)


@router.patch("/{source_id}", response_model=SourceRead)
async def update_source(
    source_id: UUID, payload: SourcePatch, session: Session, _owner: OwnerWrite
) -> SourceRead:
    """Update non-null source fields through the lifecycle owner contract."""
    source = await public.get_source(session, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if not payload.model_fields_set or any(
        getattr(payload, key) is None for key in payload.model_fields_set
    ):
        raise HTTPException(status_code=422, detail="At least one non-null field is required")
    source = await public.update_source(session, source, payload)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return SourceRead.model_validate(source, from_attributes=True)


@router.delete("/{source_id}")
async def delete_source(
    source_id: UUID,
    session: Session,
    _owner: OwnerWrite,
    with_data: bool = False,
) -> Response:
    """Archive a source or queue a bounded identity-safe purge with its current owner progress."""
    if with_data:
        operation = await public.start_source_purge(session, source_id)
        if operation is None:
            raise HTTPException(status_code=404, detail="Source not found")
        return JSONResponse(
            status_code=202,
            content=OperationRead(
                operation_id=operation.id,
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
    source = await public.archive_source(session, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return Response(status_code=204)
