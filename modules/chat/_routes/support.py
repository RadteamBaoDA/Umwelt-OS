"""Request-flow helpers split out of ``modules.chat.routes`` to keep that module within its size ratchet."""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.chat import conversation_service
from modules.chat.models import Conversation, ResponseRun
from modules.chat.schemas import SendMessageRequest, SendMessageResponse
from modules.chat.worker import _privacy_cancel_locked
from modules.memory.public import lock_export_privacy


async def _replayed_send_response(
    session: AsyncSession, conversation_id: UUID, payload: SendMessageRequest
) -> SendMessageResponse | None:
    """Return the original response for a repeated ``client_request_id``, or ``None`` for a new request.

    A replay returns the original run and ignores every changed field (content and web_search alike): no new
    run is created, so a flipped flag cannot cause egress. Mutations hash the flag into their digest and
    answer 409 instead, because they have no other identity check. Read-only; runs under the caller's
    conversation lock.
    """
    if not payload.client_request_id:
        return None
    existing_run = await conversation_service.find_run_by_client_request(
        session,
        conversation_id,
        payload.client_request_id,
    )
    if existing_run is None:
        return None
    return SendMessageResponse(
        message_id=existing_run.user_message_id,
        response_id=existing_run.id,
        status=existing_run.status,
    )


async def _redact_cancelled_run_in_fresh_session(
    session_factory: Any, workspace_id: UUID, response_id: UUID
) -> None:
    """Durably redact a run whose privacy-fence check failed, in a brand-new Chat transaction.

    Lock order matches worker publications: the shared Memory export-privacy lock, then the Conversation
    parent, then the run row. The run and conversation are re-read under their locks, and the redaction is
    skipped silently when the run vanished. Commits the fresh session before returning.
    """
    async with session_factory() as redaction_session:
        await lock_export_privacy(redaction_session, workspace_id, shared=True)
        current_hint = await redaction_session.scalar(
            select(ResponseRun).where(
                ResponseRun.id == response_id,
            )
        )
        if current_hint is not None:
            await redaction_session.scalar(
                select(Conversation)
                .where(
                    Conversation.id == current_hint.conversation_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            current_run = await redaction_session.scalar(
                select(ResponseRun)
                .where(
                    ResponseRun.id == response_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if current_run is not None:
                await _privacy_cancel_locked(redaction_session, current_run)
        await redaction_session.commit()
