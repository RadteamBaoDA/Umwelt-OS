from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.storage import storage_path
from core.realtime import commit_with_replay, make_knowledge_change, make_source_change
from modules.ingestion import public as ingestion
from modules.knowledge.documents import public as documents
from modules.sources.models import Source, SourcePurgeOperation


async def process_source_purge(ctx: dict[str, object], event_id: str) -> None:
    """Purge source database records and then delete their raw files.

    The source generation fences stale events. Database deletion and realtime
    tombstones commit before filesystem unlink; an unlink failure marks the
    operation ``file_cleanup_failed``, returns its event to pending with a
    30-second retry time, and re-raises the filesystem error.
    Database records remain deleted while raw files that failed cleanup may
    still be present.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    settings = cast(Settings, ctx["settings"])
    identifier = UUID(event_id)
    async with factory() as session:
        event = await ingestion.get_event_delivery(session, identifier)
        if event is None or event.status == "delivered":
            return
        operation_id = UUID(str(event.payload["operation_id"]))
        hint = await session.get(SourcePurgeOperation, operation_id)
        if hint is None:
            await ingestion.set_event_delivery(session, identifier, "failed")
            await session.commit()
            return
        source = await session.scalar(select(Source).where(Source.id == hint.source_id).with_for_update())
        operation = await session.scalar(
            select(SourcePurgeOperation).where(SourcePurgeOperation.id == operation_id).with_for_update()
        )
        if operation is None or source is None or source.generation != operation.generation:
            await ingestion.set_event_delivery(session, identifier, "failed")
            if operation is not None:
                operation.status = "failed"
                operation.error_code = "source_generation_changed"
            drafts = [make_source_change(source.id, source.generation, source.status, operation_id=operation_id)] if source is not None else []
            await commit_with_replay(session, drafts)
            return
        if operation.status == "succeeded":
            await ingestion.set_event_delivery(session, identifier, "delivered")
            await session.commit()
            return
        operation.status = "running"
        operation.error_code = None
        raw_uris = list(operation.raw_uris)
        timeline_drafts = await documents.delete_source_documents(session, source.id)
        await ingestion.cancel_and_purge_source_ingestion(session, source.id)
        await commit_with_replay(session, [
            make_source_change(source.id, source.generation, source.status, operation_id=operation_id),
            make_knowledge_change(source.id, deleted=True),
            *timeline_drafts,
        ])

    # Persist tombstones first so a retry can finish file cleanup after a crash.
    try:
        for raw_uri in raw_uris:
            storage_path(settings.data_dir, raw_uri).unlink(missing_ok=True)
    except (OSError, ValueError):
        async with factory() as session:
            operation = await session.get(SourcePurgeOperation, operation_id, with_for_update=True)
            if operation is not None:
                operation.status = "failed"
                operation.error_code = "file_cleanup_failed"
            await ingestion.set_event_delivery(
                session, identifier, "pending", next_attempt_at=datetime.now(UTC) + timedelta(seconds=30)
            )
            source = await session.get(Source, operation.source_id) if operation is not None else None
            drafts = [make_source_change(source.id, source.generation, source.status, operation_id=operation_id)] if source is not None else []
            await commit_with_replay(session, drafts)
        raise

    async with factory() as session:
        operation = await session.get(SourcePurgeOperation, operation_id, with_for_update=True)
        if operation is not None:
            operation.status = "succeeded"
            operation.error_code = None
        await ingestion.set_event_delivery(session, identifier, "delivered")
        source = await session.get(Source, operation.source_id) if operation is not None else None
        drafts = [make_source_change(source.id, source.generation, source.status, operation_id=operation_id)] if source is not None else []
        await commit_with_replay(session, drafts)
