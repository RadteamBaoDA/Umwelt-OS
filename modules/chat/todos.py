"""Conversation-scoped to-do list shared by the model tools and the owner's panel.

Ported from the CopilotKit OpenIntelligentUI ``manage_todos``/``get_todos`` agent tools (MIT), with the
agent-state list replaced by the ``chat_todos`` table.

Invariants:
    * Every call is scoped by the caller's workspace and resolves the parent conversation first; a
      conversation outside the workspace is indistinguishable from a missing one (``not_found``).
    * Todos hold only model- or owner-typed task text, never document or evidence content. They are
      treated as user data: the ``chat_todos`` composite FK cascades from ``chat_conversations``, so
      conversation deletion and retention purges remove them with the transcript.
    * Service functions never commit; the route or tool runner owns the unit of work.
"""

import json
import logging
from collections.abc import Sequence
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.schemas import WorkspaceContext
from modules.chat.models import ChatTodo, Conversation

logger = logging.getLogger(__name__)

MAX_TODOS = 50
TodoStatus = Literal["pending", "completed"]


class TodoError(Exception):
    """Domain failure with a stable machine code (``not_found`` or ``invalid``)."""

    def __init__(self, code: str, message: str = "") -> None:
        """Store the code; ``message`` defaults to it."""
        super().__init__(message or code)
        self.code = code


class TodoItem(BaseModel):
    """One todo as supplied by the model or the panel; ``id`` is kept only when it is a valid UUID."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    id: str | None = Field(default=None, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)
    emoji: str = Field(default="", max_length=8)
    status: TodoStatus = "pending"


class TodoRead(BaseModel):
    """Public projection of a stored todo."""

    id: UUID
    title: str
    description: str
    emoji: str
    status: TodoStatus
    position: int


class TodoReplace(BaseModel):
    """Request body that replaces the whole list."""

    model_config = ConfigDict(extra="forbid")

    todos: list[TodoItem] = Field(max_length=MAX_TODOS)


class TodoPatch(BaseModel):
    """Request body for a partial update of one todo."""

    model_config = ConfigDict(extra="forbid")

    status: TodoStatus | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)


def to_read(row: ChatTodo) -> TodoRead:
    """Project a stored row onto its public shape."""
    return TodoRead(id=row.id, title=row.title, description=row.description, emoji=row.emoji,
                    status=row.status, position=row.position)


async def _conversation(session: AsyncSession, scope: WorkspaceContext, conversation_id: UUID,
                        *, lock: bool = False) -> Conversation:
    """Load the workspace-scoped parent conversation, optionally locking it to serialize list writers.

    Raises:
        TodoError: ``not_found`` when the conversation is missing or belongs to another workspace.
    """
    stmt = select(Conversation).where(
        Conversation.id == conversation_id, Conversation.workspace_id == scope.workspace_id)
    if lock:
        stmt = stmt.with_for_update()
    conversation = await session.scalar(stmt)
    if conversation is None:
        raise TodoError("not_found", "Conversation not found")
    return conversation


async def _rows(session: AsyncSession, conversation_id: UUID) -> list[ChatTodo]:
    """Return the conversation's todos in stable position order."""
    result = await session.scalars(
        select(ChatTodo).where(ChatTodo.conversation_id == conversation_id)
        .order_by(ChatTodo.position, ChatTodo.created_at, ChatTodo.id))
    return list(result)


async def list_todos(session: AsyncSession, scope: WorkspaceContext, conversation_id: UUID) -> list[ChatTodo]:
    """List a conversation's todos ordered by position.

    Raises:
        TodoError: ``not_found`` for a missing or foreign conversation.
    """
    await _conversation(session, scope, conversation_id)
    return await _rows(session, conversation_id)


def _valid_uuid(value: str | None) -> UUID | None:
    """Parse a model-supplied id; anything that is not a UUID is treated as missing."""
    try:
        return UUID(value) if value else None
    except ValueError:
        return None


async def replace_todos(session: AsyncSession, scope: WorkspaceContext, conversation_id: UUID,
                        items: Sequence[TodoItem]) -> list[ChatTodo]:
    """Atomically replace the conversation's whole list (upstream ``manage_todos`` semantics).

    The parent conversation is locked first so concurrent replacements serialize. Valid UUID ids that
    already belong to this conversation are kept (rows updated in place, ``created_at`` preserved);
    ids that are malformed, duplicated in the payload, or owned by another conversation get a fresh
    UUID so one conversation can never overwrite or probe another's rows. Order is the input order.
    Rows absent from the payload are deleted. The caller commits.

    Raises:
        TodoError: ``not_found`` for a missing or foreign conversation; ``invalid`` above 50 todos.
    """
    if len(items) > MAX_TODOS:
        raise TodoError("invalid", f"At most {MAX_TODOS} todos are allowed")
    conversation = await _conversation(session, scope, conversation_id, lock=True)
    existing = {row.id: row for row in await _rows(session, conversation_id)}
    wanted = [_valid_uuid(item.id) for item in items]
    foreign: set[UUID] = set()
    candidates = [uid for uid in wanted if uid is not None and uid not in existing]
    if candidates:
        foreign = set(await session.scalars(select(ChatTodo.id).where(ChatTodo.id.in_(candidates))))
    kept: dict[UUID, ChatTodo] = {}
    for position, (item, uid) in enumerate(zip(items, wanted, strict=True)):
        if uid is None or uid in kept or uid in foreign:
            uid = uuid4()
        row = existing.get(uid)
        if row is None:
            row = ChatTodo(id=uid, workspace_id=conversation.workspace_id, actor_user_id=conversation.actor_user_id,
                           conversation_id=conversation_id)
            session.add(row)
        row.title, row.description, row.emoji = item.title, item.description, item.emoji
        row.status, row.position = item.status, position
        kept[uid] = row
    for uid, row in existing.items():
        if uid not in kept:
            await session.delete(row)
    await session.flush()
    return list(kept.values())


async def update_todo_status(session: AsyncSession, scope: WorkspaceContext, conversation_id: UUID, todo_id: UUID,
                             *, status: TodoStatus | None = None, title: str | None = None) -> ChatTodo:
    """Change one todo's status and/or title; the caller commits.

    Raises:
        TodoError: ``not_found`` when the conversation or todo is missing or foreign.
    """
    await _conversation(session, scope, conversation_id, lock=True)
    row = await session.scalar(select(ChatTodo).where(
        ChatTodo.id == todo_id, ChatTodo.conversation_id == conversation_id))
    if row is None:
        raise TodoError("not_found", "Todo not found")
    if status is not None:
        row.status = status
    if title is not None:
        row.title = title.strip()[:200]
    await session.flush()
    return row


def _tool_payload(rows: Sequence[ChatTodo]) -> dict[str, Any]:
    """Shape rows as the upstream tool result (``todos`` with id/title/description/emoji/status)."""
    return {"todos": [to_read(r).model_dump(mode="json", exclude={"position"}) for r in rows]}


async def run_todo_tool(name: str, raw_args: str, session: AsyncSession, conversation_id: UUID) -> str:
    """Execute ``manage_todos`` or ``get_todos`` and return the tool-message JSON; never raises.

    The model is trusted only for the arguments: the conversation is loaded by id (the worker already
    holds the run's authority) to derive its workspace scope, so a tool call can only touch the
    conversation of its own run. Writes commit here; failures roll back and return ``{"error": ...}``.
    """
    from modules.chat.scope import owner_default_scope

    try:
        conversation = await session.scalar(select(Conversation).where(Conversation.id == conversation_id))
        if conversation is None:
            return json.dumps({"error": "Conversation not found"})
        scope = await owner_default_scope(session, conversation.actor_user_id)
        if scope.workspace_id != conversation.workspace_id:
            return json.dumps({"error": "Conversation not found"})
        if name == "get_todos":
            return json.dumps(_tool_payload(await list_todos(session, scope, conversation_id)))
        if name == "manage_todos":
            args = json.loads(raw_args or "{}")
            payload = TodoReplace.model_validate({"todos": args.get("todos") if isinstance(args, dict) else None})
            rows = await replace_todos(session, scope, conversation_id, payload.todos)
            await session.commit()
            return json.dumps(_tool_payload(rows))
        return json.dumps({"error": f"Unknown todo tool: {name[:64]}"})
    except (TodoError, ValidationError, ValueError) as exc:
        await session.rollback()
        return json.dumps({"error": str(exc)[:500]})
    except Exception:
        logger.exception("Todo tool %r failed", name[:64])
        await session.rollback()
        return json.dumps({"error": "Todo tool failed"})
