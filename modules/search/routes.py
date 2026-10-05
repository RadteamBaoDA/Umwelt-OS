from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_owner, require_owner_write
from core.auth.models import AuthSession
from core.config import Settings
from core.database import get_session
from modules.goals.schemas import GoalFilter
from modules.search import indexing, public
from modules.search.schemas import (
    GlobalSearchResponse,
    ReindexResponse,
    SearchFilters,
    SearchIndexStatus,
    SearchRequest,
    SearchResponse,
)
from modules.tasks.schemas import TaskFilter

router = APIRouter(prefix="/api/v1/search", tags=["search"])
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[AuthSession, Depends(require_owner)]
OwnerWrite = Annotated[AuthSession, Depends(require_owner_write)]


@router.get("/global", response_model=GlobalSearchResponse)
async def global_search(
    request: Request,
    session: Session,
    owner: OwnerRead,
    q: Annotated[str, Query(min_length=1, max_length=300)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    document_cursor: Annotated[str | None, Query(max_length=256)] = None,
    task_cursor: Annotated[str | None, Query(max_length=512)] = None,
    goal_cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> GlobalSearchResponse:
    """Search owner documents, tasks, and goals using separate bounded page cursors.

    The authenticated owner ID is passed only to owner list APIs. Document search
    keeps its existing lexical result/citation path and document cursor. Task and
    goal ordering comes from their own list APIs, with no relevance score; source,
    date, and content-type filters do not apply to those records. FastAPI rejects
    query and cursor length errors with 422, and whitespace-only queries are
    rejected before document search. Owner cursor decoders return controlled
    422 responses for malformed tokens. API middleware applies the private,
    no-store response policy.
    """
    if not q.strip():
        # Empty owner queries mean an unfiltered collection, so reject before any search path.
        raise HTTPException(status_code=422, detail="Search query must contain non-whitespace characters")

    doc_request = SearchRequest(
        query=q,
        mode="lexical",
        limit=limit,
        cursor=document_cursor,
        filters=SearchFilters(),
    )
    doc_response = await public.search(session, request.app.state.redis, request.app.state.settings, doc_request)
    try:
        task_filter = TaskFilter(q=q, limit=limit, cursor=task_cursor)
        goal_filter = GoalFilter(q=q, limit=limit, cursor=goal_cursor)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="Invalid internal search filter or cursor") from exc

    task_page, goal_page = await public.search_tasks_and_goals(
        session, owner.owner_id, task_filter, goal_filter
    )
    total = len(doc_response.items) + len(task_page.items) + len(goal_page.items)
    return GlobalSearchResponse(
        documents=doc_response.items,
        tasks=task_page.items,
        goals=goal_page.items,
        document_next_cursor=doc_response.next_cursor,
        task_next_cursor=task_page.next_cursor,
        goal_next_cursor=goal_page.next_cursor,
        total=total,
    )


@router.post("", response_model=SearchResponse)
async def search(payload: SearchRequest, request: Request, session: Session, _owner: OwnerRead) -> SearchResponse:
    """Run owner-authenticated search using the request-scoped Redis and settings."""
    return await public.search(session, request.app.state.redis, request.app.state.settings, payload)


@router.get("/index", response_model=SearchIndexStatus)
async def index_status(session: Session, _owner: OwnerRead) -> SearchIndexStatus:
    """Return owner-only status for the active or latest index generation."""
    return await public.index_status(session)


@router.post("/reindex", response_model=ReindexResponse, status_code=202)
async def reindex(request: Request, session: Session, _owner: OwnerWrite) -> ReindexResponse:
    """Queue reindexing only when a permitted remote embedding mapping is configured."""
    redis: Redis = request.app.state.redis
    settings: Settings = request.app.state.settings
    config, mapping, policy = await indexing.configured_embedding(session, settings, redis)
    if mapping is None or not mapping.model.strip() or mapping.destination != "remote" or not policy.embeddings_allowed:
        raise HTTPException(status_code=409, detail="Configure and permit a remote embedding model first")
    try:
        generation = await indexing.create_generation(session, mapping, config.gateway_identity)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="An index generation for another gateway is still running") from exc
    return ReindexResponse(run_id=generation.id)
