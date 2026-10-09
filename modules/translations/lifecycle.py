"""Translation cache cleanup: immediate purge on revoke, expiry pages and orphan removal (derived data only)."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.translations.models import ContentTranslation, TranslationBatch

PAGE = 100


async def purge_resource_translations(
    session: AsyncSession, workspace_id: UUID, resource_type: str, resource_id: UUID,
) -> int:
    """Delete every actor's cache rows for one resource (flush-only; batch items null their reference).

    A running worker's publish then updates 0 rows, so nothing is resurrected.
    """
    result = await session.execute(delete(ContentTranslation).where(
        ContentTranslation.workspace_id == workspace_id, ContentTranslation.resource_type == resource_type,
        ContentTranslation.resource_id == resource_id,
    ).execution_options(synchronize_session=False))
    return int(getattr(result, "rowcount", 0) or 0)


async def expire_page(session: AsyncSession, *, now: datetime | None = None, limit: int = PAGE) -> int:
    """Delete one page of expired rows (any status except a live-leased running row) plus expired batches."""
    now = now or datetime.now(UTC)
    running = and_(ContentTranslation.lease_token.is_not(None), ContentTranslation.lease_expires_at > now)
    ids = select(ContentTranslation.id).where(
        ContentTranslation.expires_at <= now, ~running,
    ).order_by(ContentTranslation.expires_at).limit(limit).with_for_update(skip_locked=True)
    rows = await session.execute(
        delete(ContentTranslation).where(ContentTranslation.id.in_(ids)).execution_options(synchronize_session=False))
    batch_ids = select(TranslationBatch.id).where(TranslationBatch.expires_at <= now).limit(limit)
    batches = await session.execute(
        delete(TranslationBatch).where(TranslationBatch.id.in_(batch_ids)).execution_options(synchronize_session=False))
    return int(getattr(rows, "rowcount", 0) or 0) + int(getattr(batches, "rowcount", 0) or 0)


async def orphan_page(session: AsyncSession, after: UUID | None, limit: int = PAGE) -> list[ContentTranslation]:
    """Keyset page of rows without a live lease, for the orphan sweeper."""
    stmt = select(ContentTranslation).where(
        or_(ContentTranslation.lease_token.is_(None), ContentTranslation.lease_expires_at <= datetime.now(UTC)),
    ).order_by(ContentTranslation.id).limit(limit)
    if after is not None:
        stmt = stmt.where(ContentTranslation.id > after)
    return list((await session.scalars(stmt)).all())
