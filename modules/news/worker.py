"""Process durable News readiness receipts under source and document fences."""

from typing import cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modules.ingestion import public as ingestion
from modules.knowledge.documents import public as documents
from modules.news.models import NewsRecoveryCheckpoint
from modules.news.stories import cluster_observation
from modules.sources import public as sources


def _factory(ctx: dict[str, object]) -> async_sessionmaker[AsyncSession]:
    """Read the worker-owned async database session factory from ARQ context."""
    return cast(async_sessionmaker[AsyncSession], ctx["session_factory"])


async def process_news_document_ready(ctx: dict[str, object], event_id: str) -> None:
    """Cluster one immutable ready version and ACK its outbox event atomically.

    The handler validates event shape, locks source before document, confirms
    current version and generation, writes only bounded database state, then
    acknowledges within the same session transaction. Duplicate events return
    the existing version membership; invalid or stale evidence is safely ACKed
    without exposing its content or recreating a stale story.
    """
    try:
        identifier = UUID(event_id)
    except ValueError:
        return
    async with _factory(ctx)() as session:
        event = await ingestion.lock_news_document_ready_event(session, identifier)
        if event is None or event.status in ("delivered", "failed"):
            return
        if event.version != 1 or not event.valid_payload:
            await ingestion.fail_news_document_ready_event(session, identifier)
            await session.commit()
            return
        try:
            source_id = UUID(str(event.payload["source_id"]))
            document_id = UUID(str(event.payload["document_id"]))
            version_id = UUID(str(event.payload["document_version_id"]))
            generation = int(event.payload["source_generation"])
            version_number = int(event.payload["version_number"])
        except (KeyError, TypeError, ValueError):
            await ingestion.fail_news_document_ready_event(session, identifier)
            await session.commit()
            return
        source = await sources.lock_source(session, source_id)
        if source is None or source.status != "active" or source.generation != generation:
            await ingestion.mark_news_document_ready_event_delivered(session, identifier)
            await session.commit()
            return
        if not await documents.lock_document_for_extraction(session, document_id, source_id):
            await ingestion.mark_news_document_ready_event_delivered(session, identifier)
            await session.commit()
            return
        projection = await documents.get_news_document_projection(
            session, document_id, expected_source_generation=generation,
        )
        if (
            projection is None or projection.document_version_id != version_id
            or projection.version_number != version_number or projection.source_id != source_id
        ):
            await ingestion.mark_news_document_ready_event_delivered(session, identifier)
            await session.commit()
            return
        await cluster_observation(
            session, document_id=document_id, expected_source_generation=generation,
        )
        await ingestion.mark_news_document_ready_event_delivered(session, identifier)
        await session.commit()


async def recover_news_work(ctx: dict[str, object]) -> int:
    """Catch up one finite page of preexisting ready documents with durable keysets.

    PostgreSQL checkpoint and observation writes commit together. The source
    facade exposes only detached active identities; each page then locks sorted
    sources before sorted documents and revalidates current generation/version
    through Documents before clustering. Repeated cron runs advance both cursors
    and wrap only after the last active source page, so old imported documents
    are eventually discovered without a source polling timer or ARQ-only state.
    """
    async with _factory(ctx)() as session:
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('news:legacy-catchup', 0))"))
        checkpoint = await session.get(NewsRecoveryCheckpoint, 1, with_for_update=True)
        if checkpoint is None:
            checkpoint = NewsRecoveryCheckpoint(id=1)
            session.add(checkpoint)
            await session.flush()
        source_page = await sources.list_active_gadget_sources(
            session, limit=32, cursor=checkpoint.source_cursor,
        )
        source_ids = tuple(item.id for item in source_page.items)
        if not source_ids:
            checkpoint.source_cursor = None
            checkpoint.document_cursor = None
            await session.commit()
            return 0
        projections, document_cursor = await documents.list_news_document_projections(
            session, source_ids=source_ids, limit=10, cursor=checkpoint.document_cursor,
        )
        projections.sort(key=lambda item: (str(item.source_id), str(item.document_id)))
        fences = {}
        for source_id in sorted(source_ids, key=str):
            fences[source_id] = await sources.lock_source(session, source_id)
        locked_documents = set()
        for projection in projections:
            if projection.document_id not in locked_documents:  # noqa: SIM102  # style-only rewrite skipped to avoid touching control flow
                if await documents.lock_document_for_extraction(session, projection.document_id, projection.source_id):
                    locked_documents.add(projection.document_id)
        processed = 0
        for projection in projections:
            source = fences.get(projection.source_id)
            if (
                source is None or source.status != "active"
                or source.generation != projection.current_source_generation
                or projection.document_id not in locked_documents
            ):
                continue
            current = await documents.get_news_document_projection(
                session, projection.document_id, expected_source_generation=source.generation,
            )
            if current is None or current.document_version_id != projection.document_version_id:
                continue
            await cluster_observation(
                session, document_id=current.document_id,
                expected_source_generation=current.current_source_generation,
            )
            processed += 1
        if document_cursor is None:
            checkpoint.source_cursor = source_page.next_cursor
            checkpoint.document_cursor = None
        else:
            checkpoint.document_cursor = document_cursor
        await session.commit()
        return processed
