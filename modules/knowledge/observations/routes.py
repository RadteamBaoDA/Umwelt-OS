"""Owner-protected bounded structured observation reads."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_session
from core.workspaces.dependencies import require_workspace_read
from core.workspaces.schemas import WorkspaceContext
from modules.knowledge.observations import public
from modules.knowledge.observations.schemas import (
    GeospatialObservationPage,
    ObservationPage,
    ObservationQuery,
    ObservationRead,
)
from modules.settings.public import module_dependency

router = APIRouter(
    prefix="/api/v1/observations", tags=["observations"],
    dependencies=[Depends(module_dependency("knowledge.observations"))],
)
Session = Annotated[AsyncSession, Depends(get_session)]
WorkspaceRead = Annotated[WorkspaceContext, Depends(require_workspace_read)]


@router.get("/geospatial", response_model=GeospatialObservationPage)
async def read_geospatial_observations(
    session: Session, workspace: WorkspaceRead, request: Request, response: Response,
    source_ids: Annotated[list[UUID], Query(min_length=1, max_length=32)],
    from_at: datetime, to_at: datetime,
    regions: Annotated[list[str], Query(max_length=32)] = [],  # noqa: B006  # never mutated; FastAPI/DTO copies the default
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
) -> GeospatialObservationPage:
    """Return current, evidence-backed point observations under one owner-gated cursor scope."""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        query = ObservationQuery(
            source_ids=source_ids, regions=regions, from_at=from_at, to_at=to_at,
            limit=limit, geospatial_only=True,
        )
    except ValidationError as exc:
        from fastapi import HTTPException

        raise HTTPException(status_code=422, detail="Geospatial observation query is invalid") from exc
    try:
        return await public.list_geospatial_observations(
            session, query, cursor, scope=workspace,
            multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
        )
    except ValueError as exc:
        from fastapi import HTTPException

        raise HTTPException(status_code=422, detail="Geospatial observation query is invalid") from exc


@router.get("", response_model=ObservationPage)
async def read_observations(
    session: Session, workspace: WorkspaceRead, request: Request, response: Response,
    source_ids: Annotated[list[UUID], Query(min_length=1, max_length=32)],
    from_at: datetime, to_at: datetime,
    metrics: Annotated[list[str], Query(max_length=32)] = [],  # noqa: B006  # never mutated; FastAPI/DTO copies the default
    symbols: Annotated[list[str], Query(max_length=32)] = [],  # noqa: B006  # never mutated; FastAPI/DTO copies the default
    regions: Annotated[list[str], Query(max_length=32)] = [],  # noqa: B006  # never mutated; FastAPI/DTO copies the default
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    geospatial_only: bool = False,
) -> ObservationPage:
    """Return an owner-authorized half-open time series page with raw values omitted."""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        query = ObservationQuery(
            source_ids=source_ids, metrics=metrics, symbols=symbols, regions=regions,
            from_at=from_at, to_at=to_at, limit=limit, geospatial_only=geospatial_only,
        )
    except ValidationError as exc:
        from fastapi import HTTPException

        raise HTTPException(status_code=422, detail="Observation query is invalid") from exc
    items, next_cursor, truncated, _ = await public.list_observations(
        session, query, cursor, scope=workspace,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
    )
    return ObservationPage(
        items=[ObservationRead.model_validate(row) for row in items],
        next_cursor=next_cursor, truncated=truncated,
    )
