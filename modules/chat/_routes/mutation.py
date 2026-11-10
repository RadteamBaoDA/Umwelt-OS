"""Message-mutation replay, original-load and branch-staging helpers behind ``modules.chat.routes``."""

import hashlib
import hmac
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from modules.chat import conversation_service
from modules.chat._routes.guards import (
    _message_mutation_digest,
    _message_reasoning,
    _message_suggestions,
    _run_context,
)
from modules.chat.models import (
    Conversation,
    Message,
    MessageMutationReceipt,
    ResponseRun,
)
from modules.chat.schemas import (
    MessageMutationRequest,
    MessageRead,
    SendMessageResponse,
    WebSearchOutcomeRead,
    parse_generative_ui_parts,
)


def _message_reads(
    messages_rows: list[Message],
    current_citations: list[Any],
    response_ids_by_user_message: dict[UUID, UUID],
    web_search_by_assistant: dict[UUID, WebSearchOutcomeRead],
) -> list[MessageRead]:
    """Translate stored messages into API DTOs using already-filtered current citations.

    Pure mapping: ``current_citations`` aligns one-to-one with ``messages_rows`` (strict zip).
    The first generative-UI part is mirrored into ``generative_ui`` for older clients.
    """
    return [
        MessageRead(
            id=m.id,
            conversation_id=m.conversation_id,
            role=m.role,
            content=m.content,
            client_request_id=m.client_request_id,
            model_identity=m.model_identity,
            citations=citations,
            response_id=m.response_id or response_ids_by_user_message.get(m.id),
            revision_of_message_id=m.revision_of_message_id,
            created_at=m.created_at,
            web_search=web_search_by_assistant.get(m.id),
            generative_ui=parts[0] if parts else None,  # first part, for older clients
            generative_ui_parts=parts,
            reasoning=_message_reasoning(getattr(m, "metadata_json", None)),
            suggestions=_message_suggestions(getattr(m, "metadata_json", None)),
        )
        for m, citations, parts in zip(
            messages_rows,
            current_citations,
            (parse_generative_ui_parts(getattr(m, "metadata_json", None)) for m in messages_rows),
            strict=True,
        )
    ]


def _require_history_storage(privacy: Any, conversation: Conversation) -> None:
    """Refuse (409) a persistent turn while Memory consent disables conversation history."""
    if not privacy.store_conversation_history and not conversation.ephemeral:
        raise HTTPException(
            status_code=409,
            detail="History storage is disabled. Start a new temporary conversation to continue.",
        )


def _mutation_digest_for(
    payload: MessageMutationRequest,
    message_id: UUID,
    normalized_content: str | None,
) -> str:
    """Compute the idempotency digest binding a mutation request to its exact inputs."""
    return _message_mutation_digest(
        action=payload.action,
        target_message_id=message_id,
        base_content_hash=payload.base_content_hash,
        content=normalized_content,
        web_search=payload.web_search,
        model_override=(
            {"provider_id": payload.provider_id, "model": payload.model}
            if payload.provider_id and payload.model
            else None
        ),
    )


async def _replay_mutation_or_reject_collision(
    session: AsyncSession,
    conversation_id: UUID,
    payload: MessageMutationRequest,
    digest: str,
) -> SendMessageResponse | None:
    """Return the original response for an identical retry, or ``None`` for a fresh request key.

    Raises:
        HTTPException: 409 when the key was used with different content, the stored receipt's
            run is gone, or the key already belongs to a normal send.
    """
    receipt = await conversation_service.find_mutation_receipt(
        session,
        conversation_id,
        payload.client_request_id,
    )
    if receipt is not None:
        if not hmac.compare_digest(receipt.request_digest, digest):
            raise HTTPException(
                status_code=409,
                detail="Mutation request ID was already used with different content",
            )
        replayed_run = await conversation_service.find_run_by_id(session, receipt.response_id)
        if replayed_run is None:
            raise HTTPException(status_code=409, detail="Mutation receipt is no longer available")
        return SendMessageResponse(
            message_id=receipt.result_user_message_id,
            response_id=replayed_run.id,
            status=replayed_run.status,
        )

    # A request key belongs to one creation path; reject a collision with a normal send.
    if await conversation_service.request_id_in_use_by_run(
        session, conversation_id, payload.client_request_id
    ):
        raise HTTPException(status_code=409, detail="Mutation request ID is already in use")
    return None


async def _load_mutation_originals(
    session: AsyncSession,
    conversation_id: UUID,
    message_id: UUID,
    payload: MessageMutationRequest,
) -> tuple[Message, ResponseRun, Message]:
    """Lock the mutation target and load the original run and prompt it derives from.

    Order: target row lock, role check, base-content hash check, original run, original prompt.

    Raises:
        HTTPException: 404 missing target; 422 action/role mismatch; 409 stale content or missing
            original context/prompt.
    """
    target = await conversation_service.lock_mutation_target(session, conversation_id, message_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Message not found")
    expected_role = "user" if payload.action == "edit" else "assistant"
    if target.role != expected_role:
        raise HTTPException(
            status_code=422, detail="Mutation action does not match the message role"
        )
    actual_hash = hashlib.sha256(target.content.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(actual_hash, payload.base_content_hash):
        raise HTTPException(
            status_code=409, detail="Message changed; reload the conversation before retrying"
        )

    original_run = await conversation_service.find_original_run(
        session, conversation_id, target, payload.action
    )
    if original_run is None:
        raise HTTPException(status_code=409, detail="The original response context is unavailable")
    original_prompt = await conversation_service.find_original_prompt(
        session, conversation_id, original_run
    )
    if original_prompt is None:
        raise HTTPException(status_code=409, detail="The original prompt is unavailable")
    return target, original_run, original_prompt


async def _stage_mutation_branch(
    session: AsyncSession,
    conversation: Conversation,
    payload: MessageMutationRequest,
    *,
    target: Message,
    original_run: ResponseRun,
    original_prompt: Message,
    normalized_content: str | None,
    privacy: Any,
    model_override: dict[str, str] | None,
    digest: str,
) -> tuple[Message, ResponseRun]:
    """Stage the revised prompt, its pending run and the idempotency receipt (flush, no commit).

    The prompt is flushed first for its id, then the run, then the receipt referencing both; the
    caller commits all three atomically and bumps nothing else. ``conversation.updated_at`` is
    set here so the commit publishes it with the branch.
    """
    conversation_id = conversation.id
    user_message = Message(
        conversation_id=conversation_id,
        role="user",
        content=normalized_content if payload.action == "edit" else original_prompt.content,
        client_request_id=payload.client_request_id,
        revision_of_message_id=target.id,
    )
    session.add(user_message)
    await session.flush()
    response_run = ResponseRun(
        workspace_id=conversation.workspace_id,
        actor_user_id=conversation.actor_user_id,
        conversation_id=conversation_id,
        user_message_id=user_message.id,
        client_request_id=payload.client_request_id,
        status="pending",
        # Retain the original client-visible context; every server-private `_` key (privacy fence,
        # web search opt-in) is rewritten by `_run_context`. The worker rechecks its source/version
        # fences before retrieval and before remote send.
        retrieval_context=_run_context(
            original_run.retrieval_context,
            privacy,
            payload.web_search,
            model_override,
            db_query=payload.db_query,
        ),
        ephemeral=conversation.ephemeral,
        expires_at=conversation.expires_at,
    )
    session.add(response_run)
    await session.flush()
    session.add(
        MessageMutationReceipt(
            conversation_id=conversation_id,
            client_request_id=payload.client_request_id,
            request_digest=digest,
            action=payload.action,
            target_message_id=target.id,
            result_user_message_id=user_message.id,
            response_id=response_run.id,
        )
    )
    conversation.updated_at = func.now()
    return user_message, response_run
