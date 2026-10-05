"""Owner-authenticated story and trend query routes with private cache policy."""

from typing import Annotated
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_owner
from core.auth.models import AuthSession
from core.database import get_session
from modules.news.schemas import StoryDetail, StoryFilter, StoryPage, TrendFilter, TrendPage
from modules.news.stories import get_story, list_stories
from modules.news.trends import list_trends
from modules.settings.public import module_dependency

router = APIRouter(prefix="/api/v1", tags=["news"], dependencies=[Depends(module_dependency("news"))])
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[AuthSession, Depends(require_owner)]


def _no_store(response: Response) -> None:
    """Prevent shared caches from retaining owner-specific current story evidence."""
    response.headers["Cache-Control"] = "private, no-store"


@router.get("/stories", response_model=StoryPage)
async def list_stories_route(
    session: Session, owner: OwnerRead, response: Response,
    source_ids: Annotated[list[UUID], Query(max_length=32)] = [],
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
        return await list_stories(session, owner.owner_id, filters)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid story filter") from exc


@router.get("/stories/{story_id}", response_model=StoryDetail)
async def get_story_route(
    story_id: UUID, session: Session, owner: OwnerRead, response: Response,
    source_ids: Annotated[list[UUID], Query(max_length=32)] = [],
    evidence_cursor: Annotated[str | None, Query(max_length=4096)] = None,
    evidence_limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> StoryDetail:
    """Return one current story only while an authorized supporting revision remains."""
    _no_store(response)
    StoryFilter(source_ids=source_ids)
    value = await get_story(
        session, owner.owner_id, story_id, tuple(source_ids),
        evidence_limit=evidence_limit, evidence_cursor=evidence_cursor,
    )
    if value is None:
        raise HTTPException(status_code=404, detail="Story not found")
    return value


@router.get("/trends", response_model=TrendPage)
async def list_trends_route(
    session: Session, owner: OwnerRead, response: Response,
    source_ids: Annotated[list[UUID], Query(max_length=32)] = [],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> TrendPage:
    """Return current source-breadth trend candidates with explicit baseline flags."""
    _no_store(response)
    return await list_trends(session, owner.owner_id, TrendFilter(source_ids=source_ids, limit=limit))
