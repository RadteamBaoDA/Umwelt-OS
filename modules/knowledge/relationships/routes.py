from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_owner, require_owner_write, require_workspace_read, require_workspace_write
from core.auth.models import AuthSession
from core.database import get_session
from core.workspaces.schemas import WorkspaceContext
from modules.knowledge.entities.public import RedirectedEntityConflict
from modules.knowledge.relationships import public
from modules.knowledge.relationships.schemas import (
    EvidencePage,
    RelationshipCreate,
    RelationshipPage,
    RelationshipRead,
)
from modules.settings.public import module_dependency

router = APIRouter(prefix="/api/v1/relationships", tags=["knowledge"], dependencies=[Depends(module_dependency("knowledge.relationships"))])
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[AuthSession, Depends(require_owner)]
OwnerWrite = Annotated[AuthSession, Depends(require_owner_write)]
WorkspaceRead = Annotated[WorkspaceContext, Depends(require_workspace_read)]
WorkspaceWrite = Annotated[WorkspaceContext, Depends(require_workspace_write)]


@router.get("", response_model=RelationshipPage)
async def list_relationships(
    session: Session,
    _owner: OwnerRead,
    scope: WorkspaceRead,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
    entity_id: UUID | None = None,
    valid_at: datetime | None = None,
    include_unknown_validity: bool = True,
    knowledge_as_of: datetime | None = None,
) -> RelationshipPage:
    """List owner facts with separate validity/observation controls and bound cursor.

    Historical canonical values remain explicitly unavailable where never saved;
    current permissions and retained exact evidence are required for all rows.
    """
    try:
        return await public.list_relationships(
            session, limit, cursor, entity_id, valid_at=valid_at,
            include_unknown_validity=include_unknown_validity, knowledge_as_of=knowledge_as_of,
            scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("", response_model=RelationshipRead, status_code=201)
async def create_relationship(payload: RelationshipCreate, session: Session, owner: OwnerWrite,
                              scope: WorkspaceWrite, request: Request) -> RelationshipRead:
    """Create an authorized relationship using the requested owner or derived origin.

    Public validation checks endpoint/evidence membership and derives confidence
    from evidence for derived facts; redirected/missing endpoints map to HTTP
    errors. Route authorization does not rewrite the supplied origin.
    """
    try:
        return await public.create_relationship(session, payload, actor_id=owner.owner_id, scope=scope,
                                                multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    except RedirectedEntityConflict as exc:
        raise HTTPException(status_code=409, detail={"code": "ENTITY_REDIRECTED", "message": str(exc), "details": {}}) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/{relationship_id}", status_code=204)
async def delete_relationship(
    relationship_id: UUID, session: Session, owner: OwnerWrite, scope: WorkspaceWrite, request: Request,
    reason: Annotated[str, Query(min_length=1, max_length=300)] = "owner_relationship_delete",
) -> None:
    """Delete a relationship using the authenticated owner and bounded audit reason."""
    try:
        if not await public.remove_relationship(session, relationship_id, actor_id=owner.owner_id, reason=reason,
                                                scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled):
            raise HTTPException(status_code=404, detail="Relationship not found")
    except RedirectedEntityConflict as exc:
        raise HTTPException(status_code=409, detail={"code": "ENTITY_REDIRECTED", "message": str(exc), "details": {}}) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{relationship_id}/evidence", response_model=EvidencePage)
async def list_evidence(
    relationship_id: UUID,
    session: Session,
    _owner: OwnerRead,
    scope: WorkspaceRead,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    cursor: str | None = Query(default=None, max_length=512),
    knowledge_as_of: datetime | None = None,
) -> EvidencePage:
    """Return permitted retained observations, separately from historical fact values."""
    try:
        rows, next_cursor = await public.list_relationship_evidence(
            session, relationship_id, limit, cursor, knowledge_as_of=knowledge_as_of,
            scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if rows is None:
        raise HTTPException(status_code=404, detail="Relationship not found")
    return EvidencePage(items=rows, next_cursor=next_cursor)
