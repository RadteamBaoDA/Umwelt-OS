"""Owner-authenticated event and timeline HTTP routes."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_session
from core.workspaces.dependencies import require_workspace_read, require_workspace_write
from core.workspaces.schemas import WorkspaceContext
from modules.settings.public import module_dependency
from modules.timeline import public
from modules.timeline.schemas import (
    EventCreate,
    EventPage,
    EventPatch,
    EventRead,
    TimelinePage,
    TimelineQuery,
)

router = APIRouter(tags=["timeline"], dependencies=[Depends(module_dependency("knowledge.timeline"))])
Session = Annotated[AsyncSession, Depends(get_session)]
WorkspaceRead = Annotated[WorkspaceContext, Depends(require_workspace_read)]
WorkspaceWrite = Annotated[WorkspaceContext, Depends(require_workspace_write)]


@router.get("/api/v1/events", response_model=EventPage)
async def list_events(session: Session, scope: WorkspaceRead, request: Request, response: Response,
                      limit: Annotated[int, Query(ge=1, le=100)] = 50,
                      cursor: Annotated[str | None, Query(max_length=1024)] = None,
                      source_id: UUID | None = None,
                      q: Annotated[str | None, Query(min_length=1, max_length=200)] = None) -> EventPage:
    """List owner events in bounded stable pages and prevent HTTP caching."""
    if response is not None:
        response.headers["Cache-Control"] = "no-store"
    try:
        return await public.list_events(session, limit=limit, cursor=cursor, source_id=source_id, q=q,
                                        scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/api/v1/events/{event_id}", response_model=EventRead)
async def get_event(event_id: UUID, session: Session, scope: WorkspaceRead,
                    request: Request, response: Response) -> EventRead:
    """Return one owner event and mark its potentially sensitive provenance response no-store."""
    response.headers["Cache-Control"] = "no-store"
    event = await public.get_event(session, event_id, scope=scope,
                                   multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


@router.get("/api/v1/events/{event_id}/evidence")
async def list_evidence(event_id: UUID, session: Session, scope: WorkspaceRead,
                        request: Request, response: Response) -> dict[str, object]:
    """Return exact evidence references for one visible event without caching source metadata."""
    response.headers["Cache-Control"] = "no-store"
    evidence = await public.list_event_evidence(session, event_id, scope=scope,
                                                multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    if evidence is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return {"items": evidence}


@router.get("/api/v1/timeline", response_model=TimelinePage)
async def list_timeline(session: Session, scope: WorkspaceRead, request: Request, response: Response,
                        date_from: str | None = None, date_to: str | None = None,
                        timezone: str = "Asia/Ho_Chi_Minh", source_id: UUID | None = None,
                        entity_id: UUID | None = None,
                        type_filter: Annotated[str | None, Query(alias="type", min_length=1, max_length=64)] = None,
                        precision: Annotated[str, Query(pattern="^(all|timed|date|unknown)$")] = "all",
                        q: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
                        limit: Annotated[int, Query(ge=1, le=100)] = 50,
                        cursor: Annotated[str | None, Query(max_length=1024)] = None) -> TimelinePage:
    """Return filtered timeline partitions with no-store caching and cursor-bound filters."""
    response.headers["Cache-Control"] = "no-store"
    try:
        query = TimelineQuery.model_validate({
            "date_from": date_from, "date_to": date_to, "timezone": timezone,
            "source_id": source_id, "entity_id": entity_id, "type": type_filter,
            "precision": precision,
            "q": q,
        })
        return await public.list_timeline(session, query, limit=limit, cursor=cursor, scope=scope,
                                          multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/v1/events", response_model=EventRead, status_code=201)
async def create_event(payload: EventCreate, session: Session, scope: WorkspaceWrite,
                       request: Request, response: Response) -> EventRead:
    """Create a manual event under the session-bound owner write and CSRF contract."""
    response.headers["Cache-Control"] = "no-store"
    try:
        return await public.create_event(session, payload, scope=scope,
                                         multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.patch("/api/v1/events/{event_id}", response_model=EventRead)
async def update_event(event_id: UUID, payload: EventPatch, session: Session,
                       scope: WorkspaceWrite, request: Request, response: Response) -> EventRead:
    """Apply a revision-fenced event correction and map stale revisions to conflict."""
    response.headers["Cache-Control"] = "no-store"
    try:
        result = await public.update_event(session, event_id, payload, scope=scope,
                                           multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    except ValueError as exc:
        status = 409 if "stale" in str(exc) else 422
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return result


@router.delete("/api/v1/events/{event_id}", status_code=204)
async def delete_event(event_id: UUID, session: Session, scope: WorkspaceWrite,
                       request: Request, response: Response,
                       expected_revision: Annotated[int, Query(ge=1)],
                       reason: Annotated[str, Query(min_length=1, max_length=300)] = "owner_delete") -> Response:
    """Tombstone a revision-fenced owner event and retain exact derived suppression identity."""
    response.headers["Cache-Control"] = "no-store"
    try:
        deleted = await public.delete_event(session, event_id, expected_revision=expected_revision,
                                            reason=reason, scope=scope,
                                            multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    except ValueError as exc:
        status = 409 if "stale" in str(exc) else 422
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Event not found")
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
