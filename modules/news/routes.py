"""Owner-authenticated story and trend query routes with private cache policy."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.public import authenticated_session_ref
from core.database import get_session
from core.publication import require_publication_gate
from core.workspaces import public as workspaces
from core.workspaces.dependencies import require_workspace_read
from core.workspaces.schemas import GrantRef, PublicationFence, WorkspaceContext
from modules.connectors import public as connector_public
from modules.news.public import build_correlations
from modules.news.schemas import (
    CiiUnavailableRead,
    CorrelationQuery,
    CorrelationResult,
    StoryDetail,
    StoryFilter,
    StoryPage,
    StoryRead,
    TrendFilter,
    TrendPage,
)
from modules.news.stories import get_story, list_stories
from modules.news.trends import list_trends
from modules.settings.public import module_dependency

router = APIRouter(prefix="/api/v1", tags=["news"], dependencies=[Depends(module_dependency("news"))])
Session = Annotated[AsyncSession, Depends(get_session)]
WorkspaceRead = Annotated[WorkspaceContext, Depends(require_workspace_read)]


def _no_store(response: Response) -> None:
    """Prevent shared caches from retaining owner-specific current story evidence."""
    response.headers["Cache-Control"] = "private, no-store"


def _require_owner(scope: WorkspaceContext) -> None:
    """Trends, correlations and CII stay owner only; members see story projections alone."""
    if scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")


async def _gate_member_read(
    session: AsyncSession, request: Request, scope: WorkspaceContext, stories: list[StoryRead],
) -> None:
    """Bind a member's shared-content response to its grants and access fence for the publication gate."""
    if scope.role == "owner":
        return
    ids = sorted({item.document_id for story in stories for item in story.evidence}, key=str)
    grants: list[GrantRef] = []
    for start in range(0, len(ids), 500):
        grants.extend(await workspaces.read_resource_grants(
            session, scope=scope, kind="document", resource_ids=tuple(ids[start:start + 500])))
    fence = await workspaces.read_access_fence(
        session, scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    require_publication_gate(request, PublicationFence(
        scope=scope, access_fence=fence, auth_session=authenticated_session_ref(request), grants=tuple(grants)))


@router.get("/stories", response_model=StoryPage)
async def list_stories_route(
    session: Session, request: Request, scope: WorkspaceRead, response: Response,
    source_ids: Annotated[list[UUID], Query(max_length=32)] = [],  # noqa: B006  # never mutated; FastAPI/DTO copies the default
    topic_id: UUID | None = None, entity_id: UUID | None = None,
    date_from: datetime | None = None, date_to: datetime | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[str | None, Query(max_length=4096)] = None,
) -> StoryPage:
    """Return a bounded source-scoped story page for the authenticated owner."""
    _no_store(response)
    try:
        filters = StoryFilter(
            source_ids=source_ids, topic_id=topic_id, entity_id=entity_id,
            date_from=date_from, date_to=date_to, q=q, limit=limit, cursor=cursor,
        )
        page = await list_stories(session, filters, scope=scope,
            multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid story filter") from exc
    await _gate_member_read(session, request, scope, page.items)
    return page


@router.get("/stories/{story_id}", response_model=StoryDetail)
async def get_story_route(
    story_id: UUID, session: Session, request: Request, scope: WorkspaceRead, response: Response,
    source_ids: Annotated[list[UUID], Query(max_length=32)] = [],  # noqa: B006  # never mutated; FastAPI/DTO copies the default
    evidence_cursor: Annotated[str | None, Query(max_length=4096)] = None,
    evidence_limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> StoryDetail:
    """Return one current story only while an authorized supporting revision remains."""
    _no_store(response)
    StoryFilter(source_ids=source_ids)
    value = await get_story(
        session, story_id, tuple(source_ids), evidence_limit=evidence_limit,
        evidence_cursor=evidence_cursor, scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
    )
    if value is None:
        raise HTTPException(status_code=404, detail="Story not found")
    await _gate_member_read(session, request, scope, [value.story] if value.story else [])
    return value


@router.get("/trends", response_model=TrendPage)
async def list_trends_route(
    session: Session, request: Request, scope: WorkspaceRead, response: Response,
    source_ids: Annotated[list[UUID], Query(max_length=32)] = [],  # noqa: B006  # never mutated; FastAPI/DTO copies the default
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> TrendPage:
    """Return current source-breadth trend candidates with explicit baseline flags."""
    _require_owner(scope)
    _no_store(response)
    return await list_trends(session, TrendFilter(source_ids=source_ids, limit=limit), scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)


@router.get("/intelligence/correlations", response_model=CorrelationResult)
async def read_correlations(
    session: Session, request: Request, scope: WorkspaceRead, response: Response,
    regions: Annotated[list[str], Query(min_length=1, max_length=32)],
    from_at: datetime, to_at: datetime,
    source_ids: Annotated[list[UUID], Query(max_length=32)] = [],  # noqa: B006  # never mutated; FastAPI/DTO copies the default
    limit_per_domain: Annotated[int, Query(ge=1, le=100)] = 100,
) -> CorrelationResult:
    """Return bounded evidence-only temporal co-occurrence for the authenticated owner."""
    _require_owner(scope)
    _no_store(response)
    try:
        query = CorrelationQuery(
            regions=regions, from_at=from_at, to_at=to_at, source_ids=source_ids,
            limit_per_domain=limit_per_domain,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="Correlation query is invalid") from exc
    try:
        return await build_correlations(session, query, scope=scope,
            multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Correlation query is invalid") from exc


@router.get("/intelligence/cii", response_model=CiiUnavailableRead)
async def read_cii_availability(
    scope: WorkspaceRead, response: Response,
    countries: Annotated[list[str], Query(max_length=31)] = [],  # noqa: B006  # never mutated; FastAPI/DTO copies the default
) -> CiiUnavailableRead:
    """Expose a consumed CII v8 unavailable state without fabricating scores or country coverage."""
    _require_owner(scope)
    _no_store(response)
    try:
        projection = connector_public.cii_v8_availability(countries)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="CII country scope is invalid") from exc
    return CiiUnavailableRead(method_version=projection.method_version, requested_countries=list(projection.requested_countries), score=projection.score, band=projection.band, movement_24h=projection.movement_24h, as_of=projection.as_of, availability=projection.availability, reason=projection.reason)
