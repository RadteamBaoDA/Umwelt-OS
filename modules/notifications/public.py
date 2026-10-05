"""Public notification service used by dashboard, tasks and later automation (P10)."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from modules.notifications.models import Notification
from modules.notifications.schemas import NotificationEmit, NotificationPage, NotificationRead


__all__ = ["NotificationEmit", "NotificationMissing", "NotificationPage", "emit", "list_notifications", "set_read"]


class NotificationMissing(Exception):
    """Raised when a notification is absent or owned by another owner."""


async def emit(session: AsyncSession, owner_id: int, payload: NotificationEmit) -> bool:
    """Insert a notification unless ``dedupe_key`` already exists; return whether a row was created.

    The caller owns the transaction; nothing is committed here, so emission commits atomically
    with the change that justified it.
    """
    result = await session.execute(
        insert(Notification)
        .values(owner_id=owner_id, **payload.model_dump())
        .on_conflict_do_nothing(constraint="uq_notifications_dedupe")
        .returning(Notification.id)
    )
    return result.scalar_one_or_none() is not None


async def list_notifications(
    session: AsyncSession, owner_id: int, *, unread_only: bool = False, limit: int = 50
) -> NotificationPage:
    """Return the newest bounded notifications and the owner's full unread count."""
    statement = select(Notification).where(Notification.owner_id == owner_id)
    if unread_only:
        statement = statement.where(Notification.read_at.is_(None))
    rows = (await session.scalars(
        statement.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(min(max(limit, 1), 100))
    )).all()
    unread = await session.scalar(
        select(func.count()).select_from(Notification).where(
            Notification.owner_id == owner_id, Notification.read_at.is_(None)
        )
    )
    return NotificationPage(items=[NotificationRead.model_validate(row) for row in rows], unread_count=unread or 0)


async def set_read(session: AsyncSession, owner_id: int, notification_id: UUID, read: bool) -> NotificationRead:
    """Mark one owned notification read or unread and return its projection."""
    row = await session.scalar(
        select(Notification).where(Notification.id == notification_id, Notification.owner_id == owner_id).with_for_update()
    )
    if row is None:
        raise NotificationMissing
    row.read_at = datetime.now(UTC) if read else None
    await session.commit()
    return NotificationRead.model_validate(row)
