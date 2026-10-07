"""Owner-authenticated temporal status/history and durable scoped reconciliation endpoints."""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_owner, require_owner_write
from core.auth.models import AuthSession
from core.config import Settings
from core.database import get_session
from modules.knowledge.entities.schemas import EntityHistoryPage
from modules.knowledge.service import KnowledgeService
from modules.knowledge.temporal import public
from modules.knowledge.temporal.schemas import (
    ChangePage,
    GraphStatus,
    ReconcileRequest,
    ReconcileStatus,
)
from modules.timeline.schemas import TimelineQuery

router = APIRouter(tags=["temporal-knowledge"])
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[AuthSession, Depends(require_owner)]
OwnerWrite = Annotated[AuthSession, Depends(require_owner_write)]


@router.get("/api/v1/system/graph/status", response_model=list[GraphStatus])
async def graph_status(session: Session, _owner: OwnerRead, response: Response,
                        document_version_ids: Annotated[list[UUID], Query(max_length=100)]) -> list[GraphStatus]:
    """Return a bounded authorized graph-status batch alongside usable canonical records; prevent caching."""
    response.headers["Cache-Control"] = "no-store"
    return await public.mapping_statuses(session, document_version_ids, graph_enabled=Settings().graph_enabled)


@router.post("/api/v1/system/graph/reconcile", status_code=202)
async def reconcile(payload: ReconcileRequest, session: Session, _owner: OwnerWrite, response: Response) -> dict[str, UUID]:
    """Commit one strictly selected durable run under owner Origin/session-CSRF authorization; never rebuild in HTTP."""
    response.headers["Cache-Control"] = "no-store"
    try:
        run_id = await public.request_reconcile(session, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await session.commit()
    return {"run_id": run_id}


@router.get("/api/v1/system/graph/reconcile/{run_id}", response_model=ReconcileStatus)
async def reconcile_run(run_id: UUID, session: Session, _owner: OwnerRead, response: Response) -> ReconcileStatus:
    """Read actual selected-run continuation and counts; queued/pending cleanup is never reported as success."""
    response.headers["Cache-Control"] = "no-store"
    result = await public.reconcile_status(session, run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Reconciliation run not found")
    return result


@router.get("/api/v1/entities/{entity_id}/history", response_model=EntityHistoryPage)
async def entity_history(entity_id: UUID, session: Session, _owner: OwnerRead, response: Response,
                          limit: Annotated[int, Query(ge=1, le=100)] = 50,
                          cursor: Annotated[str | None, Query(max_length=1024)] = None,
                          membership_cursor: Annotated[str | None, Query(max_length=1024)] = None) -> EntityHistoryPage:
    """Page owner audit and retained membership observations separately without deleted text or fabricated values."""
    response.headers["Cache-Control"] = "no-store"
    try:
        result = await KnowledgeService(session).entity_history(entity_id, limit=limit, cursor=cursor,
                                                                membership_cursor=membership_cursor)
    except ValueError as exc:
        # Cursor validation belongs to the public owner; malformed client input is not a server failure.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    return result


@router.get("/api/v1/entities/{entity_id}/timeline")
async def entity_timeline(entity_id: UUID, session: Session, _owner: OwnerRead, response: Response,
                           date_from: str | None = None, date_to: str | None = None,
                           timezone: str = "Asia/Ho_Chi_Minh",
                           type_filter: Annotated[str | None, Query(alias="type", max_length=64)] = None,
                           limit: Annotated[int, Query(ge=1, le=100)] = 50,
                           cursor: Annotated[str | None, Query(max_length=1024)] = None) -> Any:
    """Return canonical event occurrence/citations and graph status under the configured runtime enablement."""
    response.headers["Cache-Control"] = "no-store"
    try:
        query = TimelineQuery(date_from=date_from, date_to=date_to, timezone=timezone, type=type_filter)
        return await KnowledgeService(session).get_entity_timeline(entity_id, query,
            graph_enabled=Settings().graph_enabled, limit=limit, cursor=cursor)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Entity not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/api/v1/knowledge/changes", response_model=ChangePage)
async def changes(session: Session, _owner: OwnerRead, response: Response,
                    kind: Annotated[str | None, Query(pattern="^(entity|relationship|event)$")] = None,
                    canonical_id: UUID | None = None, observed_from: datetime | None = None,
                    observed_to: datetime | None = None,
                    limit: Annotated[int, Query(ge=1, le=100)] = 50,
                    cursor: Annotated[str | None, Query(max_length=1024)] = None) -> ChangePage:
    """Page real recorded canonical changes with current provenance permission; observation bounds are not occurrence time."""
    response.headers["Cache-Control"] = "no-store"
    try:
        return await KnowledgeService(session).find_changes(kind=kind, canonical_id=canonical_id,
            observed_from=observed_from, observed_to=observed_to, limit=limit, cursor=cursor)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
