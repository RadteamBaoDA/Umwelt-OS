"""Public module contracts and API boundary for the chat capability."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from modules.chat.citations import (
    INSUFFICIENT_EVIDENCE_MESSAGE,
    ensure_grounded_answer,
    validate_answer_citations,
    validate_citations,
)
from modules.chat.models import (
    AgentActivityLink,
    Conversation,
    Message,
    ResponseRun,
    StreamEvent,
)
from modules.chat.retrieval import (
    build_context,
    format_grounded_context,
    revalidate_context_fence,
)
from modules.chat.schemas import (
    AgentActivityRead,
    AnswerContext,
    AnswerContextRequest,
    CancelResponse,
    Citation,
    CitationValidationResult,
    ConversationCreate,
    ConversationDetailRead,
    ConversationPatch,
    ConversationRead,
    EntityContextItem,
    EvidenceItem,
    MessageRead,
    ResponseRunRead,
    SelectedEvidenceRef,
    SendMessageRequest,
    SendMessageResponse,
    TemporalContextItem,
    ValidatedAnswer,
)
from modules.chat.stream import (
    StreamBuffer,
    format_sse_event,
    make_event_id,
    parse_event_id,
)
from modules.chat.worker import (
    is_history_storage_enabled,
    process_chat_response,
    purge_expired_chat_runs,
    run_response_generation,
)

__all__ = [
    "AgentActivityLink", "AgentActivityRead",
    "get_agent_activity", "link_agent_run", "publish_agent_activity",
    "list_agent_run_ids_for_owner",
    "has_live_agent_run_link", "live_agent_conversation_id", "list_agent_run_ids_for_delete",
    "filter_live_agent_run_ids", "authorize_agent_run_access",
    "AnswerContext",
    "AnswerContextRequest",
    "CancelResponse",
    "Citation",
    "CitationValidationResult",
    "Conversation",
    "ConversationCreate",
    "ConversationDetailRead",
    "ConversationPatch",
    "ConversationRead",
    "EntityContextItem",
    "EvidenceItem",
    "INSUFFICIENT_EVIDENCE_MESSAGE",
    "Message",
    "MessageRead",
    "ResponseRun",
    "ResponseRunRead",
    "SelectedEvidenceRef",
    "SendMessageRequest",
    "SendMessageResponse",
    "StreamBuffer",
    "StreamEvent",
    "TemporalContextItem",
    "ValidatedAnswer",
    "build_context",
    "ensure_grounded_answer",
    "format_grounded_context",
    "format_sse_event",
    "is_history_storage_enabled",
    "make_event_id",
    "parse_event_id",
    "process_chat_response",
    "purge_expired_chat_runs",
    "revalidate_context_fence",
    "run_response_generation",
    "validate_answer_citations",
    "validate_citations",
]


async def link_agent_run(
    session: AsyncSession,
    conversation_id: UUID,
    agent_run_id: UUID,
    owner_id: int,
    auth_session_hash: str,
) -> None:
    """Create a live conversation-owned activity link after owner-session and expiry checks.

    Ephemeral links inherit the conversation deadline or receive a 24-hour retention deadline;
    already expired conversations cannot acquire new activity rows.
    """
    from datetime import UTC, datetime, timedelta
    from fastapi import HTTPException
    from sqlalchemy import select
    from core.auth.public import revalidate_owner_session

    conversation = await session.scalar(select(Conversation).where(Conversation.id == conversation_id))
    if conversation is None or owner_id != 1 or not await revalidate_owner_session(session, auth_session_hash, owner_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    now = datetime.now(UTC)
    if conversation.expires_at is not None and conversation.expires_at <= now:
        raise HTTPException(status_code=404, detail="Conversation not found")
    ephemeral = conversation.ephemeral or not await is_history_storage_enabled(session)
    expires_at = conversation.expires_at or (now + timedelta(hours=24) if ephemeral else None)
    session.add(AgentActivityLink(
        conversation_id=conversation_id, agent_run_id=agent_run_id, owner_id=owner_id,
        auth_session_hash=auth_session_hash, ephemeral=ephemeral,
        expires_at=expires_at,
        activities=[{"kind": "status", "status": "queued", "created_at": datetime.now(UTC).isoformat()}],
    ))


async def publish_agent_activity(
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    *,
    run_id: UUID,
    owner_id: int,
    auth_session_hash: str,
    status: str,
    tool_name: str | None = None,
) -> None:
    """Append bounded identifiers only while the linked conversation and owner session are live.

    A current durable run status is replayable by the worker reconciler; duplicate consecutive
    status publications are ignored to keep that bounded retry path from filling activity history.
    The conversation is locked before the refreshed link row; expiry is checked with fresh time
    after both lifecycle locks and owner-session revalidation, immediately before append.
    """
    from datetime import UTC, datetime
    from sqlalchemy import select
    from core.auth.public import revalidate_owner_session

    if status not in {"queued", "running", "waiting_approval", "succeeded", "failed", "cancelled", "started", "denied"}:
        return
    async with session_factory() as session:
        candidate_conversation_id = await session.scalar(
            select(AgentActivityLink.conversation_id).where(AgentActivityLink.agent_run_id == run_id)
        )
        if candidate_conversation_id is None:
            return
        # Match conversation deletion's parent-before-child lock order to avoid cascade deadlocks.
        conversation = await session.scalar(
            select(Conversation).where(Conversation.id == candidate_conversation_id).with_for_update()
        )
        link = await session.scalar(
            select(AgentActivityLink).where(AgentActivityLink.agent_run_id == run_id).with_for_update()
            .execution_options(populate_existing=True)
        )
        if (link is None or link.owner_id != owner_id or link.auth_session_hash != auth_session_hash
                or conversation is None or link.conversation_id != conversation.id
                or not await revalidate_owner_session(session, auth_session_hash, owner_id)):
            return
        # Take fresh time after waiting on lifecycle locks and revalidating the owner session.
        now = datetime.now(UTC)
        if (link.expires_at is not None and link.expires_at <= now
                or conversation.expires_at is not None and conversation.expires_at <= now):
            return
        latest_status = next((
            item.get("status") for item in reversed(link.activities)
            if isinstance(item, dict) and item.get("kind") == "status"
        ), None)
        if tool_name is None and latest_status == status:
            return
        event = {"kind": "tool" if tool_name else "status", "status": status}
        if tool_name:
            event["tool_name"] = tool_name[:160]
        event["created_at"] = now.isoformat()
        link.activities = [*link.activities[-63:], event]
        await session.commit()


async def get_agent_activity(
    session: AsyncSession, conversation_id: UUID, run_id: UUID, owner_id: int,
    auth_session_hash: str,
) -> AgentActivityRead:
    """Read safe activity only through the requested live conversation and original owner session."""
    from fastapi import HTTPException
    from sqlalchemy import select

    link = await session.scalar(select(AgentActivityLink).where(
        AgentActivityLink.conversation_id == conversation_id,
        AgentActivityLink.agent_run_id == run_id,
        AgentActivityLink.owner_id == owner_id,
    ))
    from datetime import UTC, datetime
    from core.auth.public import revalidate_owner_session

    conversation = await session.scalar(select(Conversation).where(Conversation.id == conversation_id))
    if (link is None or link.auth_session_hash != auth_session_hash
            or link.expires_at is not None and link.expires_at <= datetime.now(UTC)
            or conversation is None
            or conversation.expires_at is not None and conversation.expires_at <= datetime.now(UTC)
            or not await revalidate_owner_session(session, auth_session_hash, owner_id)):
        raise HTTPException(status_code=404, detail="Agent activity not found")
    return AgentActivityRead(
        conversation_id=link.conversation_id, agent_run_id=link.agent_run_id,
        activities=link.activities, updated_at=link.updated_at,
    )


async def list_agent_run_ids_for_owner(
    session: AsyncSession, conversation_id: UUID, owner_id: int, auth_session_hash: str,
) -> list[UUID]:
    """Return at most 50 live run links from the authenticated conversation without exposing chat storage."""
    from datetime import UTC, datetime
    from fastapi import HTTPException
    from sqlalchemy import select
    from core.auth.public import revalidate_owner_session

    conversation = await session.scalar(select(Conversation).where(Conversation.id == conversation_id))
    if (conversation is None or owner_id != 1
            or conversation.expires_at is not None and conversation.expires_at <= datetime.now(UTC)
            or not await revalidate_owner_session(session, auth_session_hash, owner_id)):
        raise HTTPException(status_code=404, detail="Conversation not found")
    statement = select(AgentActivityLink.agent_run_id).where(
        AgentActivityLink.conversation_id == conversation_id,
        AgentActivityLink.owner_id == owner_id,
        AgentActivityLink.auth_session_hash == auth_session_hash,
        (AgentActivityLink.expires_at.is_(None) | (AgentActivityLink.expires_at > datetime.now(UTC))),
    ).order_by(AgentActivityLink.updated_at.desc(), AgentActivityLink.agent_run_id).limit(50)
    return list((await session.scalars(statement)).all())


async def filter_live_agent_run_ids(
    session: AsyncSession,
    run_ids: list[UUID],
    owner_id: int,
    auth_session_hash: str,
    *,
    conversation_id: UUID | None = None,
) -> frozenset[UUID]:
    """Return bounded run links visible to the current owner within Chat's live retention window.

    This query stays inside Chat because it owns conversations and activity links. Callers may
    pass candidate run IDs only; Chat revalidates the current owner session and retention window.
    Ephemeral links remain bound to their creating session, while retained persistent history is
    available to the authenticated owner after a session rotation.
    """
    from datetime import UTC, datetime
    from core.auth.public import revalidate_owner_session
    from sqlalchemy import select

    if not run_ids or len(run_ids) > 100 or len(set(run_ids)) != len(run_ids):
        return frozenset()
    if owner_id != 1 or not await revalidate_owner_session(session, auth_session_hash, owner_id):
        return frozenset()
    now = datetime.now(UTC)
    statement = select(AgentActivityLink.agent_run_id).join(
        Conversation, Conversation.id == AgentActivityLink.conversation_id,
    ).where(
        AgentActivityLink.agent_run_id.in_(run_ids),
        AgentActivityLink.owner_id == owner_id,
        (~AgentActivityLink.ephemeral | (AgentActivityLink.auth_session_hash == auth_session_hash)),
        (AgentActivityLink.expires_at.is_(None) | (AgentActivityLink.expires_at > now)),
        (Conversation.expires_at.is_(None) | (Conversation.expires_at > now)),
    )
    if conversation_id is not None:
        statement = statement.where(AgentActivityLink.conversation_id == conversation_id)
    return frozenset((await session.scalars(statement)).all())


async def authorize_agent_run_access(
    session: AsyncSession,
    run_id: UUID,
    owner_id: int,
    auth_session_hash: str,
    *,
    require_original_session: bool = False,
    lock_conversation: bool = False,
) -> bool:
    """Authorize a linked run read or action through its live Chat conversation.

    Current owner sessions may read retained persistent history. Ephemeral reads and every
    action require the link's original session digest. Cancellation may lock the Conversation
    before the agent run row so it follows Chat deletion's parent-before-child lock order.
    Deleted, expired, unlinked, or unauthorized conversations fail closed.
    """
    from datetime import UTC, datetime
    from core.auth.public import revalidate_owner_session
    from sqlalchemy import select

    if owner_id != 1 or not await revalidate_owner_session(session, auth_session_hash, owner_id):
        return False
    conversation_id = await session.scalar(select(AgentActivityLink.conversation_id).where(
        AgentActivityLink.agent_run_id == run_id,
        AgentActivityLink.owner_id == owner_id,
    ))
    if conversation_id is None:
        return False

    conversation = None
    if lock_conversation:
        # Chat deletion locks the parent conversation before linked agent state; keep that order.
        conversation = await session.scalar(select(Conversation).where(
            Conversation.id == conversation_id,
        ).with_for_update().execution_options(populate_existing=True))
        if conversation is None:
            return False

    link = await session.scalar(select(AgentActivityLink).where(
        AgentActivityLink.agent_run_id == run_id,
        AgentActivityLink.conversation_id == conversation_id,
        AgentActivityLink.owner_id == owner_id,
    ).execution_options(populate_existing=True))
    if link is None:
        return False

    if conversation is None:
        conversation = await session.scalar(select(Conversation).where(
            Conversation.id == conversation_id,
        ).execution_options(populate_existing=True))
    if conversation is None or not await revalidate_owner_session(session, auth_session_hash, owner_id):
        return False
    now = datetime.now(UTC)
    if ((link.expires_at is not None and link.expires_at <= now)
            or (conversation.expires_at is not None and conversation.expires_at <= now)):
        return False
    if (require_original_session or link.ephemeral) and link.auth_session_hash != auth_session_hash:
        return False
    return True


async def has_live_agent_run_link(
    session: AsyncSession, run_id: UUID, owner_id: int, auth_session_hash: str,
) -> bool:
    """Authorize one action through a live link bound to the original owner session digest."""
    return await live_agent_conversation_id(session, run_id, owner_id, auth_session_hash) is not None


async def live_agent_conversation_id(
    session: AsyncSession, run_id: UUID, owner_id: int, auth_session_hash: str,
) -> UUID | None:
    """Return the linked conversation only while its owner and retention fences remain live."""
    from datetime import UTC, datetime
    from sqlalchemy import select

    now = datetime.now(UTC)
    if owner_id != 1:
        return None
    statement = select(AgentActivityLink.conversation_id).join(
        Conversation, Conversation.id == AgentActivityLink.conversation_id,
    ).where(
        AgentActivityLink.agent_run_id == run_id,
        AgentActivityLink.owner_id == owner_id,
        AgentActivityLink.auth_session_hash == auth_session_hash,
        (AgentActivityLink.expires_at.is_(None) | (AgentActivityLink.expires_at > now)),
        (Conversation.expires_at.is_(None) | (Conversation.expires_at > now)),
    )
    return await session.scalar(statement)


async def list_agent_run_ids_for_delete(
    session: AsyncSession, conversation_id: UUID, owner_id: int, after: UUID | None = None,
) -> list[UUID]:
    """Return one bounded ordered page of linked runs for Chat's deletion-time privacy cleanup contract."""
    from sqlalchemy import select

    statement = select(AgentActivityLink.agent_run_id).where(
        AgentActivityLink.conversation_id == conversation_id,
        AgentActivityLink.owner_id == owner_id,
    )
    if after is not None:
        statement = statement.where(AgentActivityLink.agent_run_id > after)
    return list((await session.scalars(statement.order_by(AgentActivityLink.agent_run_id).limit(50))).all())

