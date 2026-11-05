"""Chat worker retention purges: expired conversations, activity links, runs and terminal message deltas."""

from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.worker_context import worker_session_factory
from modules.chat._worker_base import (
    DELTA_PURGE_LIMIT,
    DELTA_RETENTION,
)
from modules.chat.models import (
    AgentActivityLink,
    Conversation,
    Message,
    MessageMutationReceipt,
    ResponseRun,
    StreamEvent,
)


async def _purge_expired_conversations(session: AsyncSession, page_size: int) -> int:
    """Delete expired ephemeral conversations (with their transcript/runs/links) through Chat's public contract.

    Each candidate is its own short transaction: the previous unit is committed, then Memory privacy and the
    Chat parent are locked and expiry is rechecked before mutation. Returns the number of rows removed
    including children; the caller commits the final unit.
    """
    removed = 0
    from modules.chat.public import delete_conversation as delete_chat_conversation
    from modules.memory.public import lock_export_privacy

    # Parent expiry owns the entire transcript and its links. Delete through Chat's public
    # contract so activity payloads are purged before the FK cascade removes their links.
    candidate_conversations = list((await session.execute(
        select(Conversation.id, Conversation.workspace_id).where(
            Conversation.ephemeral.is_(True),
            Conversation.expires_at.is_not(None),
            Conversation.expires_at <= datetime.now(UTC),
        ).order_by(Conversation.id).limit(page_size)
    )).all())
    for conversation_id, workspace_id in candidate_conversations:
        await session.commit()  # end the previous item's unit: releases the workspace privacy lock between items
        # Eraser (writer): exclusive so no consent-gated publication races the purge.
        await lock_export_privacy(session, workspace_id)
        conversation = await session.scalar(
            select(Conversation).where(Conversation.id == conversation_id)
            .with_for_update().execution_options(populate_existing=True)
        )
        now = datetime.now(UTC)
        if (conversation is None or not conversation.ephemeral or conversation.expires_at is None
                or conversation.expires_at > now):
            continue
        # Count every row in Chat's child tables covered by this cascade. The locked parent
        # prevents supported send/stream/link writers from adding more rows during the count.
        run_count = await session.scalar(select(func.count()).select_from(ResponseRun).where(
            ResponseRun.conversation_id == conversation_id,
        )) or 0
        message_count = await session.scalar(select(func.count()).select_from(Message).where(
            Message.conversation_id == conversation_id,
        )) or 0
        receipt_count = await session.scalar(select(func.count()).select_from(MessageMutationReceipt).where(
            MessageMutationReceipt.conversation_id == conversation_id,
        )) or 0
        stream_count = await session.scalar(select(func.count()).select_from(StreamEvent).join(
            ResponseRun, StreamEvent.response_id == ResponseRun.id,
        ).where(ResponseRun.conversation_id == conversation_id)) or 0
        link_count = await session.scalar(select(func.count()).select_from(AgentActivityLink).where(
            AgentActivityLink.conversation_id == conversation_id,
        )) or 0
        # The conversation's own actor owns the delete; the public helper resolves that actor's scope.
        if await delete_chat_conversation(session, conversation_id, owner_id=conversation.actor_user_id):
            removed += 1 + run_count + message_count + receipt_count + stream_count + link_count
    return removed


async def _purge_expired_activity_links(session: AsyncSession, page_size: int) -> int:
    """Remove expired ephemeral Agent activity links of retained conversations via the Agent public API.

    Lock order: Memory privacy, Chat parent, then Agent-owned state; link identity and expiry are rechecked
    after the Agent locks. Each candidate is its own short transaction. Returns the number of links removed.
    """
    removed = 0
    from modules.memory.public import lock_export_privacy

    # Expired activity links can belong to an otherwise retained conversation. Retain the
    # Agent run identity and effect rows while removing only payloads through the Agent API.
    candidate_links = list((await session.execute(
        select(AgentActivityLink.id, AgentActivityLink.conversation_id, Conversation.workspace_id)
        .join(Conversation, Conversation.id == AgentActivityLink.conversation_id)
        .where(
            AgentActivityLink.ephemeral.is_(True),
            AgentActivityLink.expires_at.is_not(None),
            AgentActivityLink.expires_at <= datetime.now(UTC),
        ).order_by(AgentActivityLink.id).limit(page_size)
    )).all())
    for link_id, conversation_id, workspace_id in candidate_links:
        await session.commit()  # end the previous item's unit: releases the workspace privacy lock between items
        await lock_export_privacy(session, workspace_id)  # eraser (writer): exclusive
        conversation = await session.scalar(
            select(Conversation).where(Conversation.id == conversation_id)
            .with_for_update().execution_options(populate_existing=True)
        )
        if conversation is None:
            continue
        link = await session.scalar(select(AgentActivityLink).where(
            AgentActivityLink.id == link_id,
            AgentActivityLink.conversation_id == conversation_id,
        ).execution_options(populate_existing=True))
        now = datetime.now(UTC)
        if (link is None or (conversation.ephemeral and conversation.expires_at is not None
                             and conversation.expires_at <= now)
                or not link.ephemeral or link.expires_at is None or link.expires_at > now):
            continue

        from core.workspaces.public import resolve_workspace_owner_context
        from core.workspaces.schemas import InternalJobScope
        from modules.agents.public import purge_agent_runs
        from modules.chat.scope import multi_workspace_enabled

        flag = multi_workspace_enabled()
        owner_ctx = await resolve_workspace_owner_context(
            session, link.workspace_id, multi_workspace_enabled=flag,
        )
        if owner_ctx is None:
            continue
        await purge_agent_runs(
            session, [link.agent_run_id],
            scope=InternalJobScope(
                workspace_id=link.workspace_id, actor_user_id=link.owner_id,
                membership_revision=owner_ctx.membership_revision,
            ),
            multi_workspace_enabled=flag,
        )
        # The parent lock serializes supported Chat link writers; refresh and lock only after
        # Agent locks, matching conversation deletion's parent-before-Agent lock ordering.
        current_link = await session.scalar(select(AgentActivityLink).where(
            AgentActivityLink.id == link_id,
            AgentActivityLink.conversation_id == conversation_id,
        ).with_for_update().execution_options(populate_existing=True))
        now = datetime.now(UTC)
        if (current_link is not None and current_link.ephemeral
                and current_link.expires_at is not None and current_link.expires_at <= now
                and not (conversation.ephemeral and conversation.expires_at is not None
                         and conversation.expires_at <= now)):
            await session.delete(current_link)
            removed += 1
    return removed


async def _purge_expired_runs(session: AsyncSession, page_size: int) -> int:
    """Remove expired ephemeral runs (plus stream events and idempotency receipts) of retained conversations.

    The persistent parent and its transcript messages remain. Each candidate is its own short transaction
    under Memory privacy -> parent -> run locks with expiry rechecked. Returns rows removed.
    """
    removed = 0
    from modules.memory.public import lock_export_privacy

    # A response can expire before its persistent conversation. Remove that run, its stream
    # events, and its idempotency receipt only; the parent and transcript messages remain.
    candidate_runs = list((await session.execute(
        select(ResponseRun.id, ResponseRun.conversation_id, ResponseRun.workspace_id)
        .join(Conversation, Conversation.id == ResponseRun.conversation_id)
        .where(
            ResponseRun.ephemeral.is_(True),
            ResponseRun.expires_at.is_not(None),
            ResponseRun.expires_at <= datetime.now(UTC),
            ~(
                Conversation.ephemeral.is_(True)
                & Conversation.expires_at.is_not(None)
                & (Conversation.expires_at <= datetime.now(UTC))
            ),
        ).order_by(ResponseRun.id).limit(page_size)
    )).all())
    for run_id, conversation_id, workspace_id in candidate_runs:
        await session.commit()  # end the previous item's unit: releases the workspace privacy lock between items
        await lock_export_privacy(session, workspace_id)  # eraser (writer): exclusive
        conversation = await session.scalar(
            select(Conversation).where(Conversation.id == conversation_id)
            .with_for_update().execution_options(populate_existing=True)
        )
        run = await session.scalar(select(ResponseRun).where(
            ResponseRun.id == run_id,
            ResponseRun.conversation_id == conversation_id,
        ).with_for_update().execution_options(populate_existing=True))
        now = datetime.now(UTC)
        if (conversation is None or run is None or not run.ephemeral or run.expires_at is None
                or run.expires_at > now
                or conversation.ephemeral and conversation.expires_at is not None
                and conversation.expires_at <= now):
            continue
        stream_count = await session.scalar(select(func.count()).select_from(StreamEvent).where(
            StreamEvent.response_id == run_id,
        )) or 0
        receipt_count = await session.scalar(select(func.count()).select_from(
            MessageMutationReceipt,
        ).where(MessageMutationReceipt.response_id == run_id)) or 0
        await session.delete(run)
        removed += 1 + stream_count + receipt_count
    return removed


async def purge_expired_chat_runs(ctx: dict[str, object]) -> int:
    """Reconcile one bounded slice of expired Chat rows under the deletion lock order.

    Args:
        ctx: ARQ worker context containing the database session factory.

    Returns:
        Number of Chat-owned rows removed in this slice, including transcripts and stream events.

        Side effects:
        At most 100 candidates of each row kind are examined per invocation. Each path serializes
        Memory privacy before the Chat parent and locks Agent-owned state only after those fences;
        it rechecks current expiry and link identity before mutation. Pinned ephemeral conversations
        still expire; pinning only excludes a conversation from ordinary unpinned-history cleanup.
        Run-only expiry never
        deletes a persistent parent. Agent cleanup uses its public owner contract, which preserves
        effect identities and uncertainty tombstones and never makes an external effect replayable.
        Each candidate is its own short transaction; the privacy lock is never held across items.
    """
    factory = worker_session_factory(ctx)
    page_size = 100
    removed = 0
    async with factory() as session:
        removed += await _purge_expired_conversations(session, page_size)
        removed += await _purge_expired_activity_links(session, page_size)
        removed += await _purge_expired_runs(session, page_size)
        await session.commit()
    await purge_terminal_message_deltas(factory)
    return removed


async def purge_terminal_message_deltas(
    factory: async_sessionmaker[AsyncSession], *, now: datetime | None = None,
) -> int:
    """Delete one bounded slice of ``message.delta`` and ``message.ui.delta`` events of old terminal runs; returns rows deleted.

    Replay reads ``seq > current_seq`` and tolerates gaps, and ``message.done`` (completed runs) already
    carries the full answer, so deltas of runs that finished more than ``DELTA_RETENTION`` ago only
    bloat ``chat_stream_events``. At most ``DELTA_PURGE_LIMIT`` rows go per call via an ``id IN
    (subselect)`` so each delete stays short; the 15-minute cron drains a backlog. Runs in its own
    transaction after the privacy-ordered purge commits: terminal events are immutable apart from
    privacy redaction, which only rewrites deltas, so no parent/run locks are needed.
    """
    cutoff = (now or datetime.now(UTC)) - DELTA_RETENTION
    ids = (
        select(StreamEvent.id)
        .join(ResponseRun, ResponseRun.id == StreamEvent.response_id)
        .where(
            # message.ui.delta partial widget snapshots are as disposable as text deltas: message.ui.done
            # keeps the final widget.
            StreamEvent.event_type.in_(("message.delta", "message.reasoning.delta", "message.ui.delta", "message.ui.discard")),
            ResponseRun.status.in_(("completed", "cancelled", "failed")),
            ResponseRun.completed_at.is_not(None),
            ResponseRun.completed_at < cutoff,
        )
        .limit(DELTA_PURGE_LIMIT)
    )
    async with factory() as session:
        result = await session.execute(delete(StreamEvent).where(StreamEvent.id.in_(ids)))
        await session.commit()
    return int(getattr(result, "rowcount", 0) or 0)
