"""Documents-owned bounded cleanup consumer for raw files and Chat evidence copies."""

from datetime import UTC, datetime, timedelta
import logging
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.storage import storage_path
from modules.ingestion import public as ingestion
from modules.knowledge.documents import public as documents
from modules.knowledge.documents.models import DocumentCleanupOperation

logger = logging.getLogger(__name__)
_RETRY_DELAY = timedelta(seconds=30)
_CONTINUATION_DELAY = timedelta(seconds=1)


async def process_document_cleanup(ctx: dict[str, object], event_id: str) -> None:
    """Advance one bounded raw and Chat copy-cleanup page after canonical deletion commits.

    The event payload contains only a Documents receipt ID. Documents locks the exact raw URI
    before the receipt, checks surviving references, and unlinks only a contained unshared path;
    it then passes one bounded detached evidence-reference page to Chat. Terminal receipts are
    rechecked after locking, and raw/Chat cursor state plus deterministic Source progress events
    commit atomically. Incomplete work reuses its child event with a bounded retry; copied status
    remains running until every owner stage is integrated. Failure handlers compare the locked
    receipt's raw/Chat cursor progress with this attempt's starting snapshot before changing it.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    settings = cast(Settings, ctx["settings"])
    identifier = UUID(event_id)
    attempt_progress: tuple[str, str, str, str, object] | None = None

    try:
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

            operation_hint = await session.scalar(select(DocumentCleanupOperation).where(
                DocumentCleanupOperation.id == operation_id,
            ))
            if operation_hint is None:
                await ingestion.set_event_delivery(session, identifier, "failed")
                await session.commit()
                return
            if operation_hint.raw_uri and operation_hint.raw_status not in {
                "not_present", "retained_shared", "succeeded",
            }:
                # Raw URI publication/deletion keeps the existing URI-before-receipt lock order.
                await documents.lock_raw_uri_identity(session, operation_hint.raw_uri)
            operation = await session.scalar(select(DocumentCleanupOperation).where(
                DocumentCleanupOperation.id == operation_id,
            ).with_for_update().execution_options(populate_existing=True))
            if operation is None:
                await ingestion.set_event_delivery(session, identifier, "failed")
                await session.commit()
                return

            cursor_snapshot = (
                dict(operation.copied_cursor)
                if isinstance(operation.copied_cursor, dict)
                else operation.copied_cursor
            )
            attempt_progress = (
                operation.raw_status,
                operation.evidence_scope_status,
                operation.chat_status,
                operation.copied_status,
                cursor_snapshot,
            )

            if operation.raw_status in {"not_present", "retained_shared", "succeeded"} and operation.chat_status == "succeeded":
                # Duplicate deliveries must not reopen a receipt whose required active stages finished.
                await ingestion.set_event_delivery(session, identifier, "delivered")
                await session.commit()
                return

            operation.status = "running"
            operation.error_code = None
            operation.copied_status = "running"
            next_attempt_at: datetime | None = None
            terminal_scope_failure = False

            if operation.raw_status not in {"not_present", "retained_shared", "succeeded"}:
                try:
                    if operation.raw_uri is None:
                        operation.raw_status = "not_present"
                    elif await documents.raw_uri_is_referenced(session, operation.raw_uri):
                        operation.raw_status = "retained_shared"
                    else:
                        # Resolve persisted URIs under data_dir immediately before the idempotent unlink.
                        storage_path(settings.data_dir, operation.raw_uri).unlink(missing_ok=True)
                        operation.raw_status = "succeeded"
                except (OSError, ValueError):
                    operation.raw_status = "failed"
                    operation.error_code = "file_cleanup_failed"
                    next_attempt_at = datetime.now(UTC) + _RETRY_DELAY

            if operation.evidence_scope_status != "captured":
                operation.chat_status = "failed"
                operation.chat_error_code = "evidence_identity_unavailable"
                operation.copied_status = "failed"
                operation.copied_error_code = "evidence_identity_unavailable"
                operation.error_code = "evidence_identity_unavailable"
                terminal_scope_failure = True
            elif operation.chat_status != "succeeded":
                operation.chat_status = "running"
                operation.chat_error_code = None
                cursor_state = operation.copied_cursor or {}
                if not isinstance(cursor_state, dict):
                    cursor_state = {}
                reference_after = UUID(str(cursor_state["reference_after"])) if cursor_state.get("reference_after") else None
                chat_cursor = cursor_state.get("chat_cursor")
                if chat_cursor is not None and not isinstance(chat_cursor, str):
                    raise ValueError("Stored Chat cleanup cursor is malformed")
                scope = await documents.list_document_cleanup_evidence_scope(
                    session, operation.id, after=reference_after, limit=100,
                )
                if scope is None:
                    raise ValueError("Document cleanup evidence scope is unavailable")
                from modules.chat import public as chat

                progress = await chat.purge_document_copied_evidence_page(
                    session, scope, cursor=chat_cursor, limit=100,
                )
                if progress.complete:
                    if scope.next_cursor is None:
                        operation.chat_status = "succeeded"
                        operation.chat_error_code = None
                        operation.copied_cursor = None
                    else:
                        operation.copied_cursor = {
                            "reference_after": str(scope.next_cursor),
                            "chat_cursor": None,
                        }
                        next_attempt_at = datetime.now(UTC) + _CONTINUATION_DELAY
                else:
                    if progress.next_cursor is None:
                        raise ValueError("Chat cleanup page is incomplete without a continuation cursor")
                    operation.copied_cursor = {
                        "reference_after": str(reference_after) if reference_after else None,
                        "chat_cursor": progress.next_cursor,
                    }
                    next_attempt_at = datetime.now(UTC) + _CONTINUATION_DELAY

            if operation.raw_status == "failed":
                operation.error_code = operation.error_code or "file_cleanup_failed"
                next_attempt_at = next_attempt_at or datetime.now(UTC) + _RETRY_DELAY
            if operation.chat_status == "failed" and not terminal_scope_failure:
                operation.copied_status = "failed"
                operation.copied_error_code = operation.chat_error_code or "chat_cleanup_failed"
                operation.error_code = operation.copied_error_code
                next_attempt_at = next_attempt_at or datetime.now(UTC) + _RETRY_DELAY

            if operation.chat_status in {"succeeded", "failed"}:
                await documents.publish_source_cleanup_wakeup(
                    session, operation,
                    progress_key=f"raw={operation.raw_status};chat={operation.chat_status}",
                )

            if operation.raw_status == "failed" or operation.chat_status == "failed":
                operation.status = "failed"
            else:
                # This task cleans Chat only; keep aggregate deletion visibly pending for other owners.
                operation.status = "running"
                operation.copied_status = "running"

            if next_attempt_at is not None:
                await ingestion.set_event_delivery(
                    session, identifier, "pending", next_attempt_at=next_attempt_at,
                )
            else:
                # The event is complete for Documents and Chat even while other copy owners remain pending.
                await ingestion.set_event_delivery(session, identifier, "delivered")
            await session.commit()
    except ValueError:
        # A malformed local continuation restarts idempotently from the first exact identity page.
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
            operation_hint = await session.scalar(select(DocumentCleanupOperation).where(
                DocumentCleanupOperation.id == operation_id,
            ))
            if operation_hint is None:
                await ingestion.set_event_delivery(session, identifier, "failed")
                await session.commit()
                return
            if operation_hint.raw_uri and operation_hint.raw_status not in {
                "not_present", "retained_shared", "succeeded",
            }:
                await documents.lock_raw_uri_identity(session, operation_hint.raw_uri)
            recovery_operation = await session.scalar(select(DocumentCleanupOperation).where(
                DocumentCleanupOperation.id == operation_id,
            ).with_for_update().execution_options(populate_existing=True))
            progress_unchanged = (
                recovery_operation is not None
                and attempt_progress is not None
                and recovery_operation.raw_status == attempt_progress[0]
                and recovery_operation.evidence_scope_status == attempt_progress[1]
                and recovery_operation.chat_status == attempt_progress[2]
                and recovery_operation.copied_status == attempt_progress[3]
                and recovery_operation.copied_cursor == attempt_progress[4]
            )
            if recovery_operation is not None and progress_unchanged and recovery_operation.chat_status != "succeeded":
                recovery_operation.copied_cursor = None
                recovery_operation.chat_status = "failed"
                recovery_operation.chat_error_code = "chat_cursor_reset"
                recovery_operation.copied_status = "failed"
                recovery_operation.copied_error_code = "chat_cursor_reset"
                recovery_operation.status = "failed"
                recovery_operation.error_code = "chat_cursor_reset"
                await documents.publish_source_cleanup_wakeup(
                    session, recovery_operation,
                    progress_key=f"raw={recovery_operation.raw_status};chat={recovery_operation.chat_status}",
                )
                await ingestion.set_event_delivery(
                    session, identifier, "pending", next_attempt_at=datetime.now(UTC) + _RETRY_DELAY,
                )
            elif progress_unchanged or recovery_operation is None or attempt_progress is None:
                await ingestion.set_event_delivery(
                    session, identifier, "pending", next_attempt_at=datetime.now(UTC) + _RETRY_DELAY,
                )
            await session.commit()
    except Exception as exc:
        logger.warning("Document copied-evidence cleanup deferred (%s)", type(exc).__name__)
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
            operation_hint = await session.scalar(select(DocumentCleanupOperation).where(
                DocumentCleanupOperation.id == operation_id,
            ))
            if operation_hint is None:
                await ingestion.set_event_delivery(session, identifier, "failed")
                await session.commit()
                return
            if operation_hint.raw_uri and operation_hint.raw_status not in {
                "not_present", "retained_shared", "succeeded",
            }:
                await documents.lock_raw_uri_identity(session, operation_hint.raw_uri)
            recovery_operation = await session.scalar(select(DocumentCleanupOperation).where(
                DocumentCleanupOperation.id == operation_id,
            ).with_for_update().execution_options(populate_existing=True))
            progress_unchanged = (
                recovery_operation is not None
                and attempt_progress is not None
                and recovery_operation.raw_status == attempt_progress[0]
                and recovery_operation.evidence_scope_status == attempt_progress[1]
                and recovery_operation.chat_status == attempt_progress[2]
                and recovery_operation.copied_status == attempt_progress[3]
                and recovery_operation.copied_cursor == attempt_progress[4]
            )
            if recovery_operation is not None and progress_unchanged and recovery_operation.chat_status != "succeeded":
                recovery_operation.chat_status = "failed"
                recovery_operation.chat_error_code = "chat_cleanup_failed"
                recovery_operation.copied_status = "failed"
                recovery_operation.copied_error_code = "chat_cleanup_failed"
                recovery_operation.status = "failed"
                recovery_operation.error_code = "chat_cleanup_failed"
                await documents.publish_source_cleanup_wakeup(
                    session, recovery_operation,
                    progress_key=f"raw={recovery_operation.raw_status};chat={recovery_operation.chat_status}",
                )
                await ingestion.set_event_delivery(
                    session, identifier, "pending", next_attempt_at=datetime.now(UTC) + _RETRY_DELAY,
                )
            elif progress_unchanged or recovery_operation is None or attempt_progress is None:
                await ingestion.set_event_delivery(
                    session, identifier, "pending", next_attempt_at=datetime.now(UTC) + _RETRY_DELAY,
                )
            await session.commit()
