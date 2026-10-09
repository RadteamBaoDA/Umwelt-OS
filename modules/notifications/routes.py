"""Protected notification REST routes."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_account, require_account_write
from core.auth.models import AuthSession
from core.database import get_session
from core.workspaces.dependencies import require_workspace_read, require_workspace_write
from core.workspaces.schemas import WorkspaceContext
from modules.notifications import public
from modules.notifications.schemas import NotificationPage, NotificationPatch, NotificationRead
from modules.settings.public import module_dependency

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"], dependencies=[Depends(module_dependency("notifications"))])
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[AuthSession, Depends(require_account)]
OwnerWrite = Annotated[AuthSession, Depends(require_account_write)]
WorkspaceRead = Annotated[WorkspaceContext, Depends(require_workspace_read)]
WorkspaceWrite = Annotated[WorkspaceContext, Depends(require_workspace_write)]


@router.get("", response_model=NotificationPage)
async def list_notifications(
    session: Session, owner: OwnerRead, scope: WorkspaceRead, request: Request, response: Response,
    unread_only: bool = False, limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> NotificationPage:
    """List the owner's newest notifications with the unread count; never cacheable."""
    response.headers["Cache-Control"] = "private, no-store"
    return await public.list_notifications(
        session, unread_only=unread_only, limit=limit, scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
    )


@router.patch("/{notification_id}", response_model=NotificationRead)
async def patch_notification(
    notification_id: UUID, payload: NotificationPatch, session: Session, owner: OwnerWrite, scope: WorkspaceWrite, request: Request,
    response: Response,
) -> NotificationRead:
    """Toggle read state of one owned notification after CSRF-protected write checks."""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return await public.set_read(
            session, notification_id, payload.read, scope=scope,
            multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
        )
    except public.NotificationMissing as exc:
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "Notification not found", "details": {}}
        ) from exc
