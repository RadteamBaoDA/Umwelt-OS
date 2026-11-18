"""Chat commands that let scheduled tasks keep a per-automation thread of assistant posts.

All functions only flush; the caller owns the transaction and must not expect a commit here.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
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
from modules.chat.schemas import MAX_SOURCE_SCOPE, SelectedEvidenceRef

# Retrieval drops selected_refs longer than MAX_SELECTED_REFS (retrieval.py), so brief refs get a
# smaller fixed share; this stops the brief from pushing the request over that limit.
MAX_BRIEF_SELECTED_REFS = 40

# Conversation metadata keys only the scheduler may write; client create/patch payloads cannot set them.
RESERVED_METADATA_KEYS = frozenset({"scheduled_task", "task_source_scope", "task_kind"})

# Chat has no pre-existing erased-content constant; this is the single placeholder for scrubbed briefs.
SCRUBBED_CONTENT = "[This message was removed because its sources are no longer available.]"

# Prompt budget for the quoted brief, and the inline fact markers stripped so they cannot be
# mistaken for evidence numbers.
TASK_BRIEF_CONTEXT_CHARS = 4000
_BRIEF_MARKER = re.compile(r"\[\d{1,2}\]")


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
    only the first time it is marked (a legacy row converted to a task, or a new row), so a later
    owner rename survives every subsequent post; with ``None`` it matches the legacy
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
        meta = conv.metadata_json or {}
        if "scheduled_task" not in meta:
            # Title only when the thread first becomes a task thread; later posts keep owner renames.
            conv.title = title[:255]
        conv.metadata_json = {**meta, "scheduled_task": {"kind": task_kind}}
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


def task_brief_key(metadata: Any) -> str | None:
    """Return the brief key a stored message's metadata is withheld by, or ``None`` for a non-task message.

    ``None`` when ``metadata`` is not a mapping or its ``origin`` is not ``"scheduled_task"``.
    Otherwise ``str(metadata.get("brief_id") or "")``: a missing, malformed or extra-key-laden
    task message still yields a key (possibly ``""``) that never matches a live brief, so it is
    withheld instead of shown in full (fail closed). Pure; unlike ``parse_task_message_meta`` it
    never validates the rest of the payload.
    """
    if not isinstance(metadata, dict) or metadata.get("origin") != "scheduled_task":
        return None
    return str(metadata.get("brief_id") or "")


async def withheld_brief_ids(
    session: AsyncSession,
    metadatas: Sequence[Any],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> frozenset[str]:
    """Return the brief key of every scheduled-task message among ``metadatas`` that must not be shown.

    ``metadatas`` are the messages' stored ``metadata_json`` values (``None`` or non-task ones are
    ignored; see :func:`task_brief_key`).

    A brief is withheld when Dashboard no longer reports it live (a captured source was deleted,
    paused or made local-only, or its support changed) or when its stored id is missing or
    unreadable, so the check fails closed. Writes nothing and takes no row locks (the Dashboard
    lookup runs with ``lock=False``), so it is safe on a long export session and in any lock order:
    withholding only decides what to display. One batched lookup for the whole transcript, scoped
    to ``scope`` and the caller's ``multi_workspace_enabled`` flag. Only the newest 500 briefs are
    checked (``metadatas`` oldest-first); older ones are withheld.
    """
    from modules.dashboard import public as dashboard  # local import: dashboard imports chat

    wanted: dict[str, UUID | None] = {}
    for raw in metadatas:
        key = task_brief_key(raw)
        if key is None:
            continue
        try:
            wanted[key] = UUID(key)
        except ValueError:
            wanted[key] = None
    ids = [u for u in wanted.values() if u is not None]
    live = (
        {
            str(i)
            for i in await dashboard.live_brief_ids(
                session,
                ids,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
                lock=False,
            )
        }
        if ids
        else set()
    )
    return frozenset(key for key in wanted if key not in live)


@dataclass(frozen=True)
class TaskBriefContext:
    """The latest live brief of a task thread as model context: marker-free text plus in-scope evidence refs."""

    text: str
    refs: list[SelectedEvidenceRef]


async def latest_task_brief(
    session: AsyncSession,
    conversation_id: UUID,
    cutoff: datetime,
    pinned_scope: Sequence[str],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> TaskBriefContext | None:
    """Return the newest scheduled-task brief posted before ``cutoff`` in this workspace-scoped thread, or None.

    None when there is no brief, when it is withheld (``withheld_brief_ids``: deleted, paused or
    local-only source, stale support, malformed id) or when its text is the scrub placeholder. Refs
    are the brief's document support restricted to ``pinned_scope`` and capped at
    ``MAX_BRIEF_SELECTED_REFS``. Read-only and lock-free, so it is safe in the turn's short transaction.
    """
    from modules.dashboard import public as dashboard  # local import: dashboard imports chat

    row = (
        await session.execute(
            select(Message.content, Message.metadata_json)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.conversation_id == conversation_id,
                Conversation.workspace_id == scope.workspace_id,
                Message.role == "assistant",
                Message.metadata_json["origin"].as_string() == "scheduled_task",
                Message.created_at < cutoff,
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
        )
    ).first()
    if row is None or row[0] == SCRUBBED_CONTENT:
        return None
    content, metadata = row[0], row[1]
    key = task_brief_key(metadata) or ""
    withheld = await withheld_brief_ids(
        session, [metadata], scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if key in withheld:
        return None
    allowed = set(pinned_scope)
    refs: dict[tuple[UUID, UUID], SelectedEvidenceRef] = {}
    for ref in await dashboard.brief_document_refs(
        session, UUID(key), scope=scope, multi_workspace_enabled=multi_workspace_enabled
    ):
        if str(ref.source_id) not in allowed:  # defence in depth: never outside the pinned scope
            continue
        refs.setdefault(
            (ref.document_version_id, ref.chunk_id),
            SelectedEvidenceRef(
                document_version_id=ref.document_version_id,
                chunk_id=ref.chunk_id,
                document_id=ref.document_id,
                source_id=ref.source_id,
            ),
        )
    text = _BRIEF_MARKER.sub("", content).strip()[:TASK_BRIEF_CONTEXT_CHARS]
    return TaskBriefContext(text=text, refs=list(refs.values())[:MAX_BRIEF_SELECTED_REFS])
