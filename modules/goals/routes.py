"""Protected REST routes for owner goal management and plan proposal acceptance."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_owner, require_owner_write
from core.auth.models import AuthSession
from core.database import get_session
from modules.goals import public
from modules.goals.public import GoalConflict, GoalMissing
from modules.goals.schemas import (
    GoalCreate,
    GoalFilter,
    GoalPage,
    GoalRead,
    GoalStatus,
    GoalUpdate,
    PlanAcceptanceResult,
    PlanProposal,
)

router = APIRouter(prefix="/api/v1/goals", tags=["goals"])
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[AuthSession, Depends(require_owner)]
OwnerWrite = Annotated[AuthSession, Depends(require_owner_write)]


def _no_store(response: Response) -> None:
    """Set standard no-store cache controls for private owner goals."""
    response.headers["Cache-Control"] = "private, no-store"


async def _call(operation):
    """Execute a goal operation and translate domain exceptions to HTTP statuses."""
    try:
        return await operation
    except GoalMissing as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "goal_not_found", "message": "Goal not found", "details": {}},
        ) from exc
    except GoalConflict as exc:
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
            detail={"code": "invalid_goal_request", "message": str(exc), "details": {}},
        ) from exc


@router.get("", response_model=GoalPage)
async def list_goals(
    session: Session,
    owner: OwnerRead,
    response: Response,
    goal_status: Annotated[GoalStatus | None, Query(alias="status")] = None,
    q: Annotated[str | None, Query(max_length=300)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> GoalPage:
    """List owner goals with optional status filtering, search, and cursor pagination."""
    _no_store(response)
    goal_filter = GoalFilter(status=goal_status, q=q, limit=limit, cursor=cursor)
    return await _call(public.list_goals(session, owner.owner_id, goal_filter))


@router.post("", status_code=status.HTTP_201_CREATED, response_model=GoalRead)
async def create_goal(
    payload: GoalCreate, session: Session, owner: OwnerWrite, response: Response
) -> GoalRead:
    """Create a new strategic goal for the authenticated owner."""
    _no_store(response)
    return await _call(public.create_goal(session, owner.owner_id, payload))


@router.get("/{goal_id}", response_model=GoalRead)
async def get_goal(
    goal_id: UUID, session: Session, owner: OwnerRead, response: Response
) -> GoalRead:
    """Retrieve an existing goal by identifier."""
    _no_store(response)
    return await _call(public.get_goal(session, owner.owner_id, goal_id))


@router.patch("/{goal_id}", response_model=GoalRead)
async def update_goal(
    goal_id: UUID,
    payload: GoalUpdate,
    session: Session,
    owner: OwnerWrite,
    response: Response,
) -> GoalRead:
    """Update goal fields under optimistic revision control."""
    _no_store(response)
    return await _call(public.update_goal(session, owner.owner_id, goal_id, payload))


@router.delete("/{goal_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_goal(
    goal_id: UUID, session: Session, owner: OwnerWrite, response: Response,
    expected_revision: Annotated[int, Query(ge=1, le=9_007_199_254_740_991)],
) -> None:
    """Delete a goal only when the caller supplies its current revision."""
    _no_store(response)
    await _call(public.delete_goal(session, owner.owner_id, goal_id, expected_revision))


@router.post("/{goal_id}/accept-plan", response_model=PlanAcceptanceResult)
async def accept_plan(
    goal_id: UUID,
    payload: PlanProposal,
    session: Session,
    owner: OwnerWrite,
    response: Response,
) -> PlanAcceptanceResult:
    """Atomically materialize an owner-accepted plan proposal into tasks and milestones once."""
    _no_store(response)
    return await _call(public.accept_plan(session, owner.owner_id, goal_id, payload))
