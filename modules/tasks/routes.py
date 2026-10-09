"""Protected REST routes for owner task management."""

from collections.abc import Awaitable
from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_session
from core.workspaces.dependencies import require_workspace_read, require_workspace_write
from core.workspaces.schemas import WorkspaceContext
from modules.settings.public import module_dependency
from modules.tasks import public
from modules.tasks.public import TaskConflict, TaskMissing
from modules.tasks.schemas import (
    TaskCreate,
    TaskFilter,
    TaskPage,
    TaskRead,
    TaskStatus,
    TaskUpdate,
    TaskView,
)

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"], dependencies=[Depends(module_dependency("tasks"))])
Session = Annotated[AsyncSession, Depends(get_session)]
WorkspaceRead = Annotated[WorkspaceContext, Depends(require_workspace_read)]
WorkspaceWrite = Annotated[WorkspaceContext, Depends(require_workspace_write)]


def _no_store(response: Response) -> None:
    """Set standard no-store cache controls for private owner tasks."""
    response.headers["Cache-Control"] = "private, no-store"


async def _call[T](operation: Awaitable[T]) -> T:
    """Execute a task operation and translate domain exceptions to HTTP statuses."""
    try:
        return await operation
    except TaskMissing as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "task_not_found", "message": "Task not found", "details": {}},
        ) from exc
    except TaskConflict as exc:
        details = (
            {"current_revision": exc.current_revision}
            if exc.current_revision is not None
            else {}
        )
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc), "details": details},
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_task_request", "message": str(exc), "details": {}},
        ) from exc


@router.get("", response_model=TaskPage)
async def list_tasks(
    session: Session,
    scope: WorkspaceRead,
    request: Request,
    response: Response,
    view: Annotated[TaskView | None, Query()] = None,
    task_status: Annotated[TaskStatus | None, Query(alias="status")] = None,
    goal_id: Annotated[UUID | None, Query()] = None,
    entity_id: Annotated[UUID | None, Query()] = None,
    due_date_from: Annotated[date | None, Query()] = None,
    due_date_to: Annotated[date | None, Query()] = None,
    due_at_from: Annotated[datetime | None, Query()] = None,
    due_at_to: Annotated[datetime | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=300)] = None,
    timezone: Annotated[str, Query(max_length=64)] = "Asia/Ho_Chi_Minh",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> TaskPage:
    """List tasks under status, view, date range, or text search predicates."""
    _no_store(response)
    try:
        task_filter = TaskFilter(
            view=view,
            status=task_status,
            goal_id=goal_id,
            entity_id=entity_id,
            due_date_from=due_date_from,
            due_date_to=due_date_to,
            due_at_from=due_at_from,
            due_at_to=due_at_to,
            q=q,
            timezone=timezone,
            limit=limit,
            cursor=cursor,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_task_request", "message": "Invalid task filters", "details": {}},
        ) from exc
    return await _call(public.list_tasks(
        session, task_filter, scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
    ))


@router.post("", status_code=status.HTTP_201_CREATED, response_model=TaskRead)
async def create_task(
    payload: TaskCreate, session: Session, scope: WorkspaceWrite,
    request: Request, response: Response,
) -> TaskRead:
    """Create a new task under the authenticated owner account."""
    _no_store(response)
    return await _call(public.create_task(
        session, payload, scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
    ))


@router.get("/{task_id}", response_model=TaskRead)
async def get_task(
    task_id: UUID, session: Session, scope: WorkspaceRead,
    request: Request, response: Response
) -> TaskRead:
    """Retrieve an existing task by its identifier."""
    _no_store(response)
    return await _call(public.get_task(
        session, task_id, scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
    ))


@router.patch("/{task_id}", response_model=TaskRead)
async def update_task(
    task_id: UUID,
    payload: TaskUpdate,
    session: Session,
    scope: WorkspaceWrite,
    request: Request,
    response: Response,
) -> TaskRead:
    """Update fields of an existing task under optimistic revision control."""
    _no_store(response)
    return await _call(public.update_task(
        session, task_id, payload, scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
    ))


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    task_id: UUID, session: Session, scope: WorkspaceWrite,
    request: Request, response: Response,
    expected_revision: Annotated[int, Query(ge=1, le=9_007_199_254_740_991)],
) -> None:
    """Soft-delete a task only when the caller supplies its current revision."""
    _no_store(response)
    await _call(public.delete_task(
        session, task_id, expected_revision, scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
    ))
