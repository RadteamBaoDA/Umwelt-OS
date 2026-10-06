from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.storage import storage_path
from modules.ingestion import public as ingestion
from modules.knowledge.documents import public as documents
from modules.knowledge.documents.models import DocumentCleanupOperation


async def process_document_cleanup(ctx: dict[str, object], event_id: str) -> None:
    """Retry a durable document's raw-file cleanup after its DB and graph tombstones commit.

    The event payload contains only an operation ID. Documents locks the raw URI while
    checking shared references, rejecting future publication of a deleted URI, and
    unlinking. Unsafe paths and filesystem failures preserve tombstones, expose a bounded
    error code, and return the durable event to pending with a delay; retrying unlink is
    idempotent.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    settings = cast(Settings, ctx["settings"])
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
        operation = await session.scalar(
            select(DocumentCleanupOperation)
            .where(DocumentCleanupOperation.id == operation_id)
            .with_for_update()
        )
        if operation is None:
            await ingestion.set_event_delivery(session, identifier, "failed")
            await session.commit()
            return
        if operation.raw_status in {"not_present", "retained_shared", "succeeded"}:
            operation.status = "succeeded"
            operation.error_code = None
            await ingestion.set_event_delivery(session, identifier, "delivered")
            await session.commit()
            return
        raw_uri = operation.raw_uri
        operation.status = "running"
        await session.commit()

    try:
        async with factory() as session:
            if raw_uri is not None:
                await documents.lock_raw_uri_identity(session, raw_uri)
            operation = await session.scalar(
                select(DocumentCleanupOperation)
                .where(DocumentCleanupOperation.id == operation_id)
                .with_for_update()
            )
            if operation is None:
                await ingestion.set_event_delivery(session, identifier, "failed")
                await session.commit()
                return
            if operation.raw_status in {"not_present", "retained_shared", "succeeded"}:
                # A duplicate delivery that waited for the URI lock must not regress a
                # cleanup another worker already completed.
                operation.status = "succeeded"
                operation.error_code = None
                await ingestion.set_event_delivery(session, identifier, "delivered")
                await session.commit()
                return
            if raw_uri is not None:
                shared = await documents.raw_uri_is_referenced(session, raw_uri)
                if shared:
                    raw_status = "retained_shared"
                else:
                    # Resolve under data_dir at the last responsible boundary; persisted URIs
                    # are still untrusted input when read back from durable state.
                    storage_path(settings.data_dir, raw_uri).unlink(missing_ok=True)
                    raw_status = "succeeded"
            else:
                raw_status = "not_present"
            operation.raw_status = raw_status
            operation.status = "succeeded"
            operation.error_code = None
            await ingestion.set_event_delivery(session, identifier, "delivered")
            # Keep the identity lock until cleanup status and delivery commit atomically.
            await session.commit()
    except (OSError, ValueError):
        async with factory() as session:
            if raw_uri is not None:
                await documents.lock_raw_uri_identity(session, raw_uri)
            operation = await session.scalar(
                select(DocumentCleanupOperation)
                .where(DocumentCleanupOperation.id == operation_id)
                .with_for_update()
            )
            if operation is not None and operation.raw_status in {"not_present", "retained_shared", "succeeded"}:
                # A concurrent delivery may have completed between the failed unlink
                # transaction and this receipt update; preserve that terminal state.
                operation.status = "succeeded"
                operation.error_code = None
                await ingestion.set_event_delivery(session, identifier, "delivered")
            else:
                if operation is not None:
                    operation.raw_status = "failed"
                    operation.status = "failed"
                    operation.error_code = "file_cleanup_failed"
                await ingestion.set_event_delivery(
                    session,
                    identifier,
                    "pending",
                    next_attempt_at=datetime.now(UTC) + timedelta(seconds=30),
                )
            await session.commit()
        raise
