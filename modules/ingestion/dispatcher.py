from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from arq.connections import ArqRedis
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modules.ingestion.models import EventOutbox

DISPATCH_STALE_AFTER = timedelta(seconds=30)
WORKER_BY_EVENT = {
    "document.file.uploaded": "process_uploaded_file",
    "document.cleanup.requested": "process_document_cleanup",
    "source.purge.requested": "process_source_purge",
    "source.purge.progressed": "process_source_purge",
    "source.purge.coverage": "process_source_memory_coverage",
    "ingestion.stage.requested": "process_ingestion_event",
    "connector.crawl.requested": "process_ingestion_event",
    "ingestion.normalize.requested": "process_normalize_event",
    "document.version.ready": "process_document_ready",
    "news.document.ready": "process_news_document_ready",
}


async def dispatch_pending_work(ctx: dict[str, object]) -> int:
    """Enqueue due supported outbox events under row locks and return the count."""
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    redis = cast(ArqRedis, ctx["redis"])
    now = datetime.now(UTC)
    async with factory() as session:
        events = list(
            (
                await session.scalars(
                    select(EventOutbox)
                    .where(
                        # Keep unsupported durable events out of the bounded dispatcher window.
                        EventOutbox.type.in_(WORKER_BY_EVENT),
                        or_(
                            and_(EventOutbox.status == "pending", EventOutbox.next_attempt_at <= now),
                            and_(
                                EventOutbox.status == "queued",
                                EventOutbox.dispatched_at < now - DISPATCH_STALE_AFTER,
                            ),
                        )
                    )
                    .order_by(EventOutbox.created_at)
                    .limit(100)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        enqueued = 0
        for event in events:
            job = WORKER_BY_EVENT[event.type]
            await redis.enqueue_job(
                job,
                str(event.id),
                _job_id=f"ingestion:{event.id}",
                _defer_until=now,
            )
            event.status = "queued"
            event.dispatched_at = now
            enqueued += 1
        if events:
            await session.commit()
        return enqueued


async def mark_event_delivered(session: AsyncSession, event_id: UUID) -> None:
    """Mark an existing outbox event delivered while holding its row lock."""
    event = await session.get(EventOutbox, event_id, with_for_update=True)
    if event is not None:
        event.status = "delivered"
        await session.commit()
