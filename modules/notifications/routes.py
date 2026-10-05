"""Protected notification REST routes."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_owner, require_owner_write
from core.auth.models import AuthSession
from core.database import get_session
from modules.notifications import public
from modules.notifications.schemas import NotificationPage, NotificationPatch, NotificationRead

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[AuthSession, Depends(require_owner)]
OwnerWrite = Annotated[AuthSession, Depends(require_owner_write)]


@router.get("", response_model=NotificationPage)
async def list_notifications(
    session: Session, owner: OwnerRead, response: Response,
    unread_only: bool = False, limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> NotificationPage:
    """List the owner's newest notifications with the unread count; never cacheable."""
    response.headers["Cache-Control"] = "private, no-store"
    return await public.list_notifications(session, owner.owner_id, unread_only=unread_only, limit=limit)


@router.patch("/{notification_id}", response_model=NotificationRead)
async def patch_notification(
    notification_id: UUID, payload: NotificationPatch, session: Session, owner: OwnerWrite, response: Response,
) -> NotificationRead:
    """Toggle read state of one owned notification after CSRF-protected write checks."""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return await public.set_read(session, owner.owner_id, notification_id, payload.read)
    except public.NotificationMissing as exc:
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "Notification not found", "details": {}}
        ) from exc
