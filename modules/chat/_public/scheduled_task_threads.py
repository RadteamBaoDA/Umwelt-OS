"""Chat commands that let scheduled tasks keep a per-automation thread of assistant posts.

All functions only flush; the caller owns the transaction and must not expect a commit here.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import NotFoundError
from core.workspaces.admission import actor_id
from core.workspaces.schemas import Scope
from modules.chat._public.conversations import delete_conversation_in_scope
from modules.chat.models import Conversation, Message
from modules.chat.schemas import MAX_SOURCE_SCOPE

# Conversation metadata keys only the scheduler may write; client create/patch payloads cannot set them.
RESERVED_METADATA_KEYS = frozenset({"scheduled_task", "task_source_scope", "task_kind"})

# Chat has no pre-existing erased-content constant; this is the single placeholder for scrubbed briefs.
SCRUBBED_CONTENT = "[This message was removed because its sources are no longer available.]"


async def delete_automation_conversation(
    session: AsyncSession, automation_id: UUID, *, scope: Scope
) -> int:
    """Delete every Chat conversation of one automation in ``scope.workspace_id``; return the count.

    Includes archived conversations. Reuses the normal conversation deletion (agent actions, saved
    templates, cascading messages and runs) so nothing is orphaned. Flushes only; the caller owns
    the transaction. Conversations of other workspaces are never touched.
    """
    ids = (
        await session.scalars(
            select(Conversation.id).where(
                Conversation.workspace_id == scope.workspace_id,
                Conversation.context_kind == "automation",
                Conversation.context_resource_id == automation_id,
            )
        )
    ).all()
    deleted = 0
    for conversation_id in ids:
        if await delete_conversation_in_scope(session, conversation_id, scope):
            deleted += 1
    return deleted


async def ensure_automation_conversation(
    session: AsyncSession,
    automation_id: UUID,
    title: str,
    *,
    task_kind: Literal["daily_brief"] | None,
    scope: Scope,
) -> UUID:
    """Return the automation's live Chat conversation id, creating it on first use.

    Scoped to ``scope.workspace_id``; archived conversations are ignored. With ``task_kind`` set the
    row is marked ``metadata["scheduled_task"] = {"kind": task_kind}`` and titled ``title[:255]``
    (also applied to an existing row); with ``None`` it matches the legacy
    ``"Automation: <title>"`` shape and empty metadata. Flushes only.
    """
    conv = await session.scalar(
        select(Conversation)
        .where(
            Conversation.workspace_id == scope.workspace_id,
            Conversation.context_kind == "automation",
            Conversation.context_resource_id == automation_id,
            Conversation.archived.is_(False),
        )
        .limit(1)
    )
    if conv is None:
        conv = Conversation(
            workspace_id=scope.workspace_id,
            actor_user_id=actor_id(scope),
            title=f"Automation: {title}"[:255],
            context_kind="automation",
            context_resource_id=automation_id,
            metadata_json={},
        )
        session.add(conv)
    if task_kind is not None:
        conv.title = title[:255]
        conv.metadata_json = {**(conv.metadata_json or {}), "scheduled_task": {"kind": task_kind}}
    await session.flush()
    return conv.id


async def append_task_message(
    session: AsyncSession,
    conversation_id: UUID,
    *,
    content: str,
    citations: list[dict[str, Any]],
    metadata: dict[str, Any],
    source_ids: Sequence[UUID],
    dedupe_key: str,
    scope: Scope,
) -> tuple[UUID, bool]:
    """Append one assistant message idempotently on ``dedupe_key``; return ``(message_id, created)``.

    Row-locks the workspace-scoped conversation first so concurrent retries serialize; raises
    ``NotFoundError`` if it is absent. A message already stored under ``task:<dedupe_key>`` is
    returned unchanged with ``created=False``. On insert, ``source_ids`` are merged newest-first
    into ``metadata["task_source_scope"]`` (string UUIDs, de-duplicated, capped at
    ``MAX_SOURCE_SCOPE`` by dropping the oldest) and ``updated_at`` is bumped. Flushes only.
    """
    conv = await session.scalar(
        select(Conversation)
        .where(Conversation.id == conversation_id, Conversation.workspace_id == scope.workspace_id)
        .with_for_update()
    )
    if conv is None:
        raise NotFoundError("Conversation not found")
    request_id = f"task:{dedupe_key}"
    existing = await session.scalar(
        select(Message.id).where(
            Message.conversation_id == conversation_id, Message.client_request_id == request_id
        )
    )
    if existing is not None:
        return existing, False
    message = Message(
        conversation_id=conversation_id,
        role="assistant",
        content=content,
        client_request_id=request_id,
        citations=citations,
        metadata_json={"origin": "scheduled_task", **metadata},
    )
    session.add(message)
    meta = dict(conv.metadata_json or {})
    merged = [str(i) for i in source_ids] + list(meta.get("task_source_scope", []))
    meta["task_source_scope"] = list(dict.fromkeys(merged))[:MAX_SOURCE_SCOPE]
    conv.metadata_json = meta
    conv.updated_at = datetime.now(UTC)
    await session.flush()
    return message.id, True


async def scrub_task_messages(
    session: AsyncSession, brief_ids: Sequence[UUID], *, scope: Scope
) -> int:
    """Blank the chat copies of the given briefs and return how many messages changed.

    Matches workspace-scoped messages whose ``metadata["brief_id"]`` is in ``brief_ids``; each gets
    ``SCRUBBED_CONTENT``, empty citations and no ``brief_citations``. Flushes only.
    """
    if not brief_ids:
        return 0
    rows = await session.scalars(
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Conversation.workspace_id == scope.workspace_id,
            Message.metadata_json["brief_id"].as_string().in_([str(i) for i in brief_ids]),
        )
        .with_for_update(of=Message)
    )
    count = 0
    for message in rows:
        message.content = SCRUBBED_CONTENT
        message.citations = []
        message.metadata_json = {
            k: v for k, v in message.metadata_json.items() if k != "brief_citations"
        }
        count += 1
    await session.flush()
    return count
