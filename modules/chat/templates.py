"""Owner-saved widget templates and the per-conversation pending template, shared by the API and model tools.

Ported from the CopilotKit OpenIntelligentUI template tools (MIT): the agent-state list becomes the
``chat_templates`` table and upstream's UI-set ``pending_template`` becomes ``chat_conversations.pending_template``.

Invariants:
    * Every call is scoped by workspace and owner (``actor_user_id``); another owner's template is
      indistinguishable from a missing one (``not_found``).
    * Template ``html`` is user/model authored markup. It is stored and returned verbatim, bounded to
      ``MAX_HTML_CHARS``, and is only ever rendered inside the sandbox iframe; this module never interprets it.
    * The three built-in seeds are served read-only from code, never written to the database.
    * Service functions never commit; the route or tool runner owns the unit of work.
"""

import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.schemas import WorkspaceContext
from modules.chat.models import ChatTemplate, Conversation

logger = logging.getLogger(__name__)

MAX_TEMPLATES = 30  # per owner; matches the browser store cap
MAX_HTML_CHARS = 60_000  # matches the frontend MAX_TEMPLATE_CHARS

_CSS = (
    "body{margin:0;font-family:system-ui,sans-serif}.card{padding:16px;border:1px solid #ccc;border-radius:12px;"
    "max-width:420px}.row{display:flex;justify-content:space-between;padding:4px 0}.muted{color:#666}"
    ".grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.big{font-size:28px;font-weight:600}"
)


def _seed(seed_id: str, name: str, description: str, data_description: str, body: str) -> dict[str, Any]:
    """Build one built-in template record (html/css only: no scripts, no network, fictional data)."""
    return {"id": seed_id, "name": name, "description": description, "data_description": data_description,
            "html": f"<style>{_CSS}</style>{body}", "version": 1, "builtin": True}


SEED_TEMPLATES: tuple[dict[str, Any], ...] = (
    _seed("seed-weather-ny", "Weather: New York", "Current conditions with a three-day outlook.",
          "City, temperature, conditions, humidity, wind and three daily highs.",
          '<div class="card"><div class="muted">New York, NY</div><div class="big">64&deg;F</div>'
          '<div>Partly cloudy</div><div class="row muted"><span>Humidity 58%</span><span>Wind 9 mph NW</span></div>'
          '<div class="row"><span>Mon 66&deg;</span><span>Tue 61&deg;</span><span>Wed 59&deg;</span></div></div>'),
    _seed("seed-invoice-northwind", "Invoice: Northwind Labs", "Itemised invoice with a total.",
          "Vendor, invoice number, line items with amounts and the total.",
          '<div class="card"><div class="big">Northwind Labs</div><div class="muted">Invoice #1042</div><hr>'
          '<div class="row"><span>Design sprint</span><span>$2,400.00</span></div>'
          '<div class="row"><span>Prototype build</span><span>$3,100.00</span></div>'
          '<div class="row"><span>Hosting (3 mo)</span><span>$180.00</span></div><hr>'
          '<div class="row"><strong>Total</strong><strong>$5,680.00</strong></div></div>'),
    _seed("seed-kpi-q1", "Q1 KPI dashboard", "Four headline metrics with deltas.",
          "Four metrics, each with a label, a value and a change versus the prior period.",
          '<div class="grid">'
          '<div class="card"><div class="muted">Revenue</div><div class="big">$1.24M</div><div>+8.2%</div></div>'
          '<div class="card"><div class="muted">Active users</div><div class="big">48.3k</div><div>+5.1%</div></div>'
          '<div class="card"><div class="muted">Churn</div><div class="big">2.4%</div><div>-0.3 pt</div></div>'
          '<div class="card"><div class="muted">NPS</div><div class="big">51</div><div>+4</div></div></div>'),
)
_SEEDS_BY_ID = {s["id"]: s for s in SEED_TEMPLATES}


class TemplateError(Exception):
    """Domain failure with a stable machine code (``not_found``, ``invalid`` or ``limit``)."""

    def __init__(self, code: str, message: str = "") -> None:
        """Store the code; ``message`` defaults to it."""
        super().__init__(message or code)
        self.code = code


class TemplateIn(BaseModel):
    """Request body / tool arguments for saving a template; ``id`` (a UUID) updates that template in place."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    id: str | None = Field(default=None, max_length=64, description="Existing template id to update; omit to create.")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    html: str = Field(min_length=1, max_length=MAX_HTML_CHARS)
    data_description: str = Field(default="", max_length=500)


class TemplateRead(BaseModel):
    """Public projection of a stored template."""

    id: UUID
    name: str
    description: str
    html: str
    data_description: str
    version: int
    created_at: datetime


class PendingBody(BaseModel):
    """Body of ``PUT .../pending-template``: the template (user UUID or seed id) the model should apply next."""

    model_config = ConfigDict(extra="forbid")

    template_id: str = Field(min_length=1, max_length=64)


class ModelSaveTemplateArgs(BaseModel):
    """Model-facing arguments of ``save_template``: create-only, so a model can never overwrite an existing template.

    There is deliberately no ``id`` field; any ``id`` the model sends is ignored. Owner routes use ``TemplateIn``.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    html: str = Field(min_length=1, max_length=MAX_HTML_CHARS)
    data_description: str = Field(default="", max_length=500)


class ApplyTemplateArgs(BaseModel):
    """Arguments of ``apply_template``: a ``template_id``, or a ``name`` (most recent match); neither uses pending."""

    template_id: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=120, description="Template name; the most recent match wins.")


def to_read(row: ChatTemplate) -> TemplateRead:
    """Project a stored row onto its public shape."""
    return TemplateRead(id=row.id, name=row.name, description=row.description, html=row.html,
                        data_description=row.data_description, version=row.version, created_at=row.created_at)


def _owned(scope: WorkspaceContext) -> tuple[Any, ...]:
    """WHERE clauses restricting ChatTemplate to the caller's workspace and owner."""
    return ChatTemplate.workspace_id == scope.workspace_id, ChatTemplate.actor_user_id == scope.user_id


def _uuid(value: str | None) -> UUID | None:
    """Parse a template id; anything that is not a UUID reads as missing."""
    try:
        return UUID(value) if value else None
    except ValueError:
        return None


async def list_templates(session: AsyncSession, scope: WorkspaceContext) -> list[ChatTemplate]:
    """List the owner's saved templates, newest first (built-in seeds excluded)."""
    result = await session.scalars(
        select(ChatTemplate).where(*_owned(scope)).order_by(ChatTemplate.created_at.desc(), ChatTemplate.id))
    return list(result)


async def get_template(session: AsyncSession, scope: WorkspaceContext, template_id: str) -> ChatTemplate | dict[str, Any]:
    """Return a seed record (dict) or the owner's stored template.

    Raises:
        TemplateError: ``not_found`` for an unknown id or another owner's template.
    """
    if template_id in _SEEDS_BY_ID:
        return _SEEDS_BY_ID[template_id]
    uid = _uuid(template_id)
    row = await session.scalar(select(ChatTemplate).where(ChatTemplate.id == uid, *_owned(scope))) if uid else None
    if row is None:
        raise TemplateError("not_found", "Template not found")
    return row


async def get_template_by_name(session: AsyncSession, scope: WorkspaceContext, name: str) -> ChatTemplate | dict[str, Any]:
    """Return the owner's most recently created template called ``name`` (case-insensitive), else a seed with that name.

    Raises:
        TemplateError: ``not_found`` when nothing matches.
    """
    wanted = name.strip().casefold()
    for row in await list_templates(session, scope):  # newest first
        if row.name.casefold() == wanted:
            return row
    for seed in SEED_TEMPLATES:
        if seed["name"].casefold() == wanted:
            return seed
    raise TemplateError("not_found", "Template not found")


async def save_template(session: AsyncSession, scope: WorkspaceContext, payload: TemplateIn,
                        *, source_response_id: UUID | None = None) -> ChatTemplate:
    """Create a template, or update one in place (bumping ``version``) when ``payload.id`` names the owner's row.

    A per-owner advisory transaction lock is taken first (row locks alone lock nothing for an owner with zero rows),
    then the rows are locked, so concurrent saves cannot overshoot the 30-template cap. ``source_response_id`` marks a
    template created by a model tool call so evidence cleanup can delete it; owner-UI saves leave it NULL.
    The caller commits.

    Raises:
        TemplateError: ``not_found`` for an unknown ``id``; ``limit`` when 30 templates already exist.
    """
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                          {"key": f"chat.templates:{scope.workspace_id}:{scope.user_id}"})
    existing = list(await session.scalars(select(ChatTemplate).where(*_owned(scope)).with_for_update()))
    if payload.id:
        row = next((r for r in existing if str(r.id) == payload.id), None)
        if row is None:
            raise TemplateError("not_found", "Template not found")
        row.name, row.description, row.html = payload.name, payload.description, payload.html
        row.data_description, row.version = payload.data_description, row.version + 1
    else:
        if len(existing) >= MAX_TEMPLATES:
            raise TemplateError("limit", f"At most {MAX_TEMPLATES} templates are allowed")
        row = ChatTemplate(workspace_id=scope.workspace_id, actor_user_id=scope.user_id, name=payload.name,
                           description=payload.description, html=payload.html,
                           data_description=payload.data_description, version=1,
                           source_response_id=source_response_id)
        session.add(row)
    await session.flush()
    return row


async def delete_template(session: AsyncSession, scope: WorkspaceContext, template_id: str) -> None:
    """Delete one owned template and clear it as pending on the owner's conversations; the caller commits.

    Raises:
        TemplateError: ``invalid`` for a read-only seed; ``not_found`` for an unknown or foreign id.
    """
    if template_id in _SEEDS_BY_ID:
        raise TemplateError("invalid", "Built-in templates are read-only")
    row = await get_template(session, scope, template_id)
    if isinstance(row, ChatTemplate):
        await session.delete(row)
    for conv in await session.scalars(select(Conversation).where(
            Conversation.workspace_id == scope.workspace_id, Conversation.actor_user_id == scope.user_id,
            Conversation.pending_template == template_id)):
        conv.pending_template = None
    await session.flush()


async def _conversation(session: AsyncSession, scope: WorkspaceContext, conversation_id: UUID) -> Conversation:
    """Load the caller's conversation (workspace and owner scoped); foreign reads as missing."""
    conversation = await session.scalar(select(Conversation).where(
        Conversation.id == conversation_id, Conversation.workspace_id == scope.workspace_id,
        Conversation.actor_user_id == scope.user_id).with_for_update())
    if conversation is None:
        raise TemplateError("not_found", "Conversation not found")
    return conversation


async def set_pending(session: AsyncSession, scope: WorkspaceContext, conversation_id: UUID, template_id: str) -> str:
    """Mark a seed or owned template as the conversation's pending template; the caller commits.

    Raises:
        TemplateError: ``not_found`` for a missing conversation or template.
    """
    conversation = await _conversation(session, scope, conversation_id)
    await get_template(session, scope, template_id)  # validates existence and ownership
    conversation.pending_template = template_id
    await session.flush()
    return template_id


async def clear_pending(session: AsyncSession, scope: WorkspaceContext, conversation_id: UUID) -> None:
    """Clear the conversation's pending template (idempotent); the caller commits."""
    conversation = await _conversation(session, scope, conversation_id)
    conversation.pending_template = None
    await session.flush()


def _record(item: ChatTemplate | dict[str, Any], *, with_html: bool) -> dict[str, Any]:
    """Shape a template for tool output; ``html`` only when applying (listing stays small)."""
    if isinstance(item, dict):
        out = {k: item[k] for k in ("id", "name", "description", "data_description", "version")} | {"builtin": True}
        html = item["html"]
    else:
        out = {"id": str(item.id), "name": item.name, "description": item.description,
               "data_description": item.data_description, "version": item.version, "builtin": False}
        html = item.html
    if with_html:
        out["html"] = html[:MAX_HTML_CHARS]
    return out


async def run_template_tool(name: str, raw_args: str, session: AsyncSession, conversation_id: UUID,
                            response_id: UUID | None = None) -> str:
    """Execute a template tool and return the tool-message JSON; never raises.

    Tools: ``save_template`` (create-only; stamps ``response_id`` as provenance), ``list_templates`` (owned plus
    read-only seeds, no html), ``apply_template`` (returns the html of ``template_id``, or of the most recent
    template called ``name``, or when both are omitted of the conversation's pending template, then clears
    pending), ``clear_pending_template``. There is no model-callable delete or overwrite: a prompt-injected
    model must not destroy or replace the owner's templates (owner routes do that). As with todos, the conversation is loaded by id to
    derive the run's own owner scope, so a call can only touch that owner's data. Failures roll back and
    return ``{"error": ...}``.
    """
    from modules.chat.scope import owner_default_scope

    try:
        conversation = await session.scalar(select(Conversation).where(Conversation.id == conversation_id))
        if conversation is None:
            return json.dumps({"error": "Conversation not found"})
        scope = await owner_default_scope(session, conversation.actor_user_id)
        if scope.workspace_id != conversation.workspace_id:
            return json.dumps({"error": "Conversation not found"})
        args = json.loads(raw_args or "{}")
        if not isinstance(args, dict):
            raise TemplateError("invalid", "Arguments must be an object")
        if name == "list_templates":
            mine = [_record(r, with_html=False) for r in await list_templates(session, scope)]
            return json.dumps({"templates": [*(_record(s, with_html=False) for s in SEED_TEMPLATES), *mine],
                               "pending_template": conversation.pending_template})
        if name == "save_template":
            created = ModelSaveTemplateArgs.model_validate(args)
            row = await save_template(session, scope, TemplateIn(**created.model_dump()),
                                      source_response_id=response_id)
            await session.commit()
            return json.dumps({"saved": _record(row, with_html=False)})
        if name == "apply_template":
            apply_args = ApplyTemplateArgs.model_validate(args)
            if apply_args.template_id:
                item = await get_template(session, scope, apply_args.template_id)
            elif apply_args.name:
                item = await get_template_by_name(session, scope, apply_args.name)
            elif conversation.pending_template:
                item = await get_template(session, scope, conversation.pending_template)
            else:
                raise TemplateError("invalid", "No template_id or name given and no pending template")
            item_id = item["id"] if isinstance(item, dict) else str(item.id)
            if conversation.pending_template == item_id:
                conversation.pending_template = None
            await session.commit()
            return json.dumps({"template": _record(item, with_html=True),
                               "instruction": "Render this template with render_sandboxed_ui, replacing its data."})
        if name == "clear_pending_template":
            conversation.pending_template = None
            await session.commit()
            return json.dumps({"cleared": True})
        return json.dumps({"error": f"Unknown template tool: {name[:64]}"})
    except (TemplateError, ValidationError, ValueError) as exc:
        await session.rollback()
        return json.dumps({"error": str(exc)[:500]})
    except Exception:
        logger.exception("Template tool %r failed", name[:64])
        await session.rollback()
        return json.dumps({"error": "Template tool failed"})
