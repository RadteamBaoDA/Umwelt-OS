from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.public import authenticated_session_ref
from core.config import Settings
from core.database import get_session
from core.publication import require_publication_gate
from core.workspaces import public as workspaces
from core.workspaces.dependencies import require_workspace_read, require_workspace_write
from core.workspaces.schemas import AccessFence, PublicationFence, WorkspaceContext
from modules.goals.schemas import GoalFilter
from modules.search import indexing, public
from modules.search.schemas import (
    GlobalSearchResponse,
    ReindexResponse,
    SearchFilters,
    SearchHit,
    SearchIndexStatus,
    SearchRequest,
    SearchResponse,
)
from modules.settings.public import module_dependency
from modules.tasks.schemas import TaskFilter

router = APIRouter(prefix="/api/v1/search", tags=["search"], dependencies=[Depends(module_dependency("search"))])
Session = Annotated[AsyncSession, Depends(get_session)]
WorkspaceRead = Annotated[WorkspaceContext, Depends(require_workspace_read)]
WorkspaceWrite = Annotated[WorkspaceContext, Depends(require_workspace_write)]


async def _gate_member_hits(
    request: Request, session: AsyncSession, workspace: WorkspaceContext, fence: AccessFence,
    hits: list[SearchHit],
) -> None:
    """Bind a member's response to the exact document grants it was built from (409 if any moved)."""
    ids = tuple(dict.fromkeys(hit.document_id for hit in hits))
    grants = await workspaces.read_resource_grants(session, scope=workspace, kind="document", resource_ids=ids)
    if {grant.resource_id for grant in grants} != set(ids):
        raise HTTPException(status_code=409, detail="Search results changed; retry")
    require_publication_gate(request, PublicationFence(
        scope=workspace, access_fence=fence, auth_session=authenticated_session_ref(request), grants=grants,
    ))


async def _member_fence(request: Request, session: AsyncSession, workspace: WorkspaceContext) -> AccessFence | None:
    """Admission fence for member reads (None for owners, who need no publication gate)."""
    if not public.is_member_scope(workspace):
        return None
    return await workspaces.read_access_fence(
        session, scope=workspace, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
    )


@router.get("/global", response_model=GlobalSearchResponse)
async def global_search(
    request: Request,
    session: Session,
    workspace: WorkspaceRead,
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
    member_fence = await _member_fence(request, session, workspace)
    doc_response = await public.search(
        session, request.app.state.redis, request.app.state.settings, doc_request,
        scope=workspace, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
    )
    if member_fence is not None:
        # Members get documents only: no task/goal rows and no cross-type counts.
        await _gate_member_hits(request, session, workspace, member_fence, doc_response.items)
        return GlobalSearchResponse(
            documents=doc_response.items, document_next_cursor=doc_response.next_cursor,
            total=len(doc_response.items),
        )
    try:
        task_filter = TaskFilter(q=q, limit=limit, cursor=task_cursor)
        goal_filter = GoalFilter(q=q, limit=limit, cursor=goal_cursor)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="Invalid internal search filter or cursor") from exc

    task_page, goal_page = await public.search_tasks_and_goals(
        session, task_filter, goal_filter, scope=workspace,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
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
async def search(
    payload: SearchRequest, request: Request, session: Session, workspace: WorkspaceRead,
) -> SearchResponse:
    """Search the selected workspace: owners see everything, members only explicitly shared documents."""
    member_fence = await _member_fence(request, session, workspace)
    response = await public.search(
        session, request.app.state.redis, request.app.state.settings, payload,
        scope=workspace, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
        release_during_embed=member_fence is None,
    )
    if member_fence is not None:
        await _gate_member_hits(request, session, workspace, member_fence, response.items)
    return response


@router.get("/index", response_model=SearchIndexStatus)
async def index_status(
    request: Request, session: Session, workspace: WorkspaceRead,
) -> SearchIndexStatus:
    """Return this admitted workspace's generation and truthful automatic-index readiness state."""
    return await public.index_status(
        session, scope=workspace,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
        settings=request.app.state.settings, redis=request.app.state.redis,
    )


@router.post("/reindex", response_model=ReindexResponse, status_code=202)
async def reindex(
    request: Request, session: Session, workspace: WorkspaceWrite,
) -> ReindexResponse:
    """Queue reindexing only when a permitted remote embedding mapping is configured."""
    redis: Redis = request.app.state.redis
    settings: Settings = request.app.state.settings
    # Admission precedes the config read; the original fence and config are compared again
    # under the workspace generation mutex, so a change in between aborts with 409.
    authority = await indexing.capture_authority(session, settings, redis, scope=workspace)
    if not authority.permitted():
        raise HTTPException(status_code=409, detail="Configure and permit a remote embedding model first")
    try:
        generation = await indexing.create_generation_for_authority(
            session, authority, settings, redis, scope=workspace,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="An index generation for another gateway is still running") from exc
    if generation is None:
        raise HTTPException(status_code=409, detail="Index generation is not available")
    return ReindexResponse(run_id=generation.id)
