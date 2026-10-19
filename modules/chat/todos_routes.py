"""Owner routes for the conversation todo panel (read, replace, patch)."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from modules.chat import todos
from modules.chat.routes import OwnerRead, OwnerWrite, Session
from modules.chat.scope import owner_default_scope
from modules.settings.public import module_dependency

router = APIRouter(tags=["chat"], dependencies=[Depends(module_dependency("chat"))])
_BASE = "/api/v1/conversations/{conversation_id}/todos"


def _http(exc: todos.TodoError) -> HTTPException:
    """Map a todo domain error onto its HTTP status (404 hides foreign conversations)."""
    return HTTPException(status_code=404 if exc.code == "not_found" else 422, detail=str(exc))


@router.get(_BASE, response_model=list[todos.TodoRead])
async def list_conversation_todos(conversation_id: UUID, session: Session, owner: OwnerRead) -> list[todos.TodoRead]:
    """Return the conversation's todos in display order; 404 when it is not in the owner's workspace."""
    scope = await owner_default_scope(session, owner.owner_id)
    try:
        return [todos.to_read(r) for r in await todos.list_todos(session, scope, conversation_id)]
    except todos.TodoError as exc:
        raise _http(exc) from None


@router.put(_BASE, response_model=list[todos.TodoRead])
async def replace_conversation_todos(
    conversation_id: UUID, payload: todos.TodoReplace, session: Session, owner: OwnerWrite,
) -> list[todos.TodoRead]:
    """Atomically replace the whole list (max 50) and return it; CSRF-checked write."""
    scope = await owner_default_scope(session, owner.owner_id)
    try:
        rows = await todos.replace_todos(session, scope, conversation_id, payload.todos)
        await session.commit()
    except todos.TodoError as exc:
        await session.rollback()
        raise _http(exc) from None
    return [todos.to_read(r) for r in rows]


@router.patch(_BASE + "/{todo_id}", response_model=todos.TodoRead)
async def patch_conversation_todo(
    conversation_id: UUID, todo_id: UUID, payload: todos.TodoPatch, session: Session, owner: OwnerWrite,
) -> todos.TodoRead:
    """Update one todo's status and/or title; CSRF-checked write."""
    scope = await owner_default_scope(session, owner.owner_id)
    try:
        row = await todos.update_todo_status(
            session, scope, conversation_id, todo_id, status=payload.status, title=payload.title)
        await session.commit()
    except todos.TodoError as exc:
        await session.rollback()
        raise _http(exc) from None
    return todos.to_read(row)
