"""Source-owned orchestration for generation-fenced source purge receipts."""

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.realtime import commit_with_replay, make_knowledge_change, make_source_change
from modules.ingestion import public as ingestion
from modules.knowledge.documents import public as documents
from modules.sources.models import Source, SourcePurgeOperation

_CONTINUATION_DELAY = timedelta(seconds=5)


async def _store_source_cleanup_progress(
    session: AsyncSession,
    operation: SourcePurgeOperation,
) -> tuple[bool, bool]:
    """Store Documents' detached aggregate and return active work and full completion.

    Source supplies the durable capture status; Documents performs a read-only aggregate over
    its indexed child linkage and returns counts/codes, never child IDs or owner-private rows.
    A nonempty scope remains incomplete while any not-yet-integrated copy owner is pending.
    """
    progress = await documents.source_cleanup_progress(
        session,
        operation.id,
        capture_recorded=operation.documents_status == "deleted",
    )
    operation.pending_child_count = progress.pending_count
    operation.failed_child_count = progress.failed_count
    operation.pending_owner_codes = list(progress.pending_owner_codes)
    active_copy_work = any(code in {"raw", "chat"} for code in progress.pending_owner_codes)
    return active_copy_work, progress.all_required_complete


async def process_source_purge(ctx: dict[str, object], event_id: str) -> None:
    """Capture and delete a Source once, then publish Documents-owned aggregate progress.

    Source is locked before its purge receipt whenever both are needed. The Documents call
    captures bounded per-document receipts and exact immutable identities in this transaction;
    its raw and Chat consumers do filesystem work under URI-before-receipt locks and wake this
    worker through operation-only outbox events. Source never snapshots or unlinks raw files.
    Active raw/Chat work receives a five-second continuation; Documents retries ordinary stage
    failures after thirty seconds and emits a fresh wakeup when that stage changes. Unintegrated
    copy owners remain visible without a Source polling loop.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    identifier = UUID(event_id)
    async with factory() as session:
        event = await ingestion.get_event_delivery(session, identifier)
        if event is None or event.status == "delivered":
            return
        try:
            operation_id = UUID(str(event.payload["operation_id"]))
        except (KeyError, TypeError, ValueError):
            await ingestion.set_event_delivery(session, identifier, "failed")
            await session.commit()
            return

        hint = await session.get(SourcePurgeOperation, operation_id)
        if hint is None:
            await ingestion.set_event_delivery(session, identifier, "failed")
            await session.commit()
            return
        # Source-before-operation is the common lifecycle lock order for mutation and replay.
        source = await session.scalar(
            select(Source).where(Source.id == hint.source_id).with_for_update()
        )
        operation = await session.scalar(
            select(SourcePurgeOperation).where(SourcePurgeOperation.id == operation_id)
            .with_for_update().execution_options(populate_existing=True)
        )
        if operation is None:
            await ingestion.set_event_delivery(session, identifier, "failed")
            await session.commit()
            return
        if operation.status == "succeeded":
            await ingestion.set_event_delivery(session, identifier, "delivered")
            await session.commit()
            return
        if source is None or source.generation != operation.generation:
            operation.status = "failed"
            if operation.documents_status != "unavailable":
                operation.documents_status = "failed"
                operation.error_code = "source_generation_changed"
                operation.pending_owner_codes = ["documents"]
            await ingestion.set_event_delivery(session, identifier, "delivered")
            drafts = [make_source_change(
                source.id, source.generation, source.status, operation_id=operation_id,
            )] if source is not None else []
            await commit_with_replay(session, drafts)
            return

        if operation.documents_status in {"failed", "unavailable"}:
            # Migration marked already-cascaded legacy scopes truthfully unavailable.
            operation.status = "failed"
            operation.error_code = operation.error_code or "evidence_identity_unavailable"
            await ingestion.set_event_delivery(session, identifier, "delivered")
            await commit_with_replay(session, [make_source_change(
                source.id, source.generation, source.status, operation_id=operation_id,
            )])
            return

        drafts = []
        if operation.documents_status == "queued":
            try:
                timeline_drafts = await documents.delete_source_documents(
                    session,
                    source.id,
                    source_purge_operation_id=operation.id,
                )
            except ValueError as exc:
                if str(exc) != "Source graph cleanup exceeds its atomic document limit":
                    raise
                operation.documents_status = "failed"
                operation.status = "failed"
                operation.error_code = "source_document_limit_exceeded"
                operation.pending_owner_codes = ["documents"]
                await ingestion.set_event_delivery(session, identifier, "delivered")
                await commit_with_replay(session, [make_source_change(
                    source.id, source.generation, source.status, operation_id=operation_id,
                )])
                return
            operation.documents_status = "deleted"
            await ingestion.cancel_and_purge_source_ingestion(session, source.id)
            drafts = [
                make_source_change(source.id, source.generation, source.status, operation_id=operation_id),
                make_knowledge_change(source.id, deleted=True),
                *timeline_drafts,
            ]

        active_copy_work, all_required_complete = await _store_source_cleanup_progress(session, operation)
        if operation.failed_child_count:
            operation.status = "failed"
            operation.error_code = "document_cleanup_failed"
            # Child receipts own the 30-second retry; a changed terminal stage creates a new wakeup.
            await ingestion.set_event_delivery(session, identifier, "delivered")
        elif not all_required_complete:
            operation.status = "running"
            operation.error_code = None
            if active_copy_work:
                await ingestion.set_event_delivery(
                    session, identifier, "pending",
                    next_attempt_at=datetime.now(UTC) + _CONTINUATION_DELAY,
                )
            else:
                # Future owner stages requeue deterministic child progress events when they finish.
                await ingestion.set_event_delivery(session, identifier, "delivered")
        else:
            # Empty source is complete only after this transaction recorded Documents deletion.
            operation.status = "succeeded"
            operation.error_code = None
            await ingestion.set_event_delivery(session, identifier, "delivered")

        if not drafts:
            drafts = [make_source_change(
                source.id, source.generation, source.status, operation_id=operation_id,
            )]
        await commit_with_replay(session, drafts)
