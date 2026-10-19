"""Owner routes for saved widget templates and the conversation's pending template."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response

from modules.chat import templates
from modules.chat.routes import OwnerRead, OwnerWrite, Session
from modules.chat.scope import owner_default_scope
from modules.settings.public import module_dependency

router = APIRouter(tags=["chat"], dependencies=[Depends(module_dependency("chat"))])
_BASE = "/api/v1/chat/templates"
_PENDING = "/api/v1/conversations/{conversation_id}/pending-template"


def _http(exc: templates.TemplateError) -> HTTPException:
    """Map a template domain error onto its HTTP status (404 hides foreign rows, 409 is the cap)."""
    status = {"not_found": 404, "limit": 409}.get(exc.code, 422)
    return HTTPException(status_code=status, detail=str(exc))


@router.get(_BASE, response_model=list[templates.TemplateRead])
async def list_chat_templates(session: Session, owner: OwnerRead) -> list[templates.TemplateRead]:
    """Return the owner's saved templates, newest first (built-in seeds are client/tool side only)."""
    scope = await owner_default_scope(session, owner.owner_id)
    return [templates.to_read(r) for r in await templates.list_templates(session, scope)]


@router.post(_BASE, response_model=templates.TemplateRead, status_code=201)
async def save_chat_template(payload: templates.TemplateIn, session: Session, owner: OwnerWrite) -> templates.TemplateRead:
    """Create a template (or update one when ``id`` is given); 409 beyond 30; CSRF-checked write."""
    scope = await owner_default_scope(session, owner.owner_id)
    try:
        row = await templates.save_template(session, scope, payload)
        await session.commit()
    except templates.TemplateError as exc:
        await session.rollback()
        raise _http(exc) from None
    return templates.to_read(row)


@router.delete(_BASE + "/{template_id}", status_code=204)
async def delete_chat_template(template_id: str, session: Session, owner: OwnerWrite) -> Response:
    """Delete one owned template; 404 for unknown/foreign ids, 422 for built-in seeds; CSRF-checked write."""
    scope = await owner_default_scope(session, owner.owner_id)
    try:
        await templates.delete_template(session, scope, template_id)
        await session.commit()
    except templates.TemplateError as exc:
        await session.rollback()
        raise _http(exc) from None
    return Response(status_code=204)


@router.put(_PENDING, response_model=templates.PendingBody)
async def set_pending_template(
    conversation_id: UUID, payload: templates.PendingBody, session: Session, owner: OwnerWrite,
) -> templates.PendingBody:
    """Mark a template (owned UUID or built-in seed id) as the one the model applies next; CSRF-checked write."""
    scope = await owner_default_scope(session, owner.owner_id)
    try:
        await templates.set_pending(session, scope, conversation_id, payload.template_id)
        await session.commit()
    except templates.TemplateError as exc:
        await session.rollback()
        raise _http(exc) from None
    return payload


@router.delete(_PENDING, status_code=204)
async def clear_pending_template(conversation_id: UUID, session: Session, owner: OwnerWrite) -> Response:
    """Clear the conversation's pending template (idempotent); CSRF-checked write."""
    scope = await owner_default_scope(session, owner.owner_id)
    try:
        await templates.clear_pending(session, scope, conversation_id)
        await session.commit()
    except templates.TemplateError as exc:
        await session.rollback()
        raise _http(exc) from None
    return Response(status_code=204)
