"""Automation Agent tools on the P07 ToolRegistry: a read tool and an approval-gated draft proposal.

The agent never enables a rule. Owner approval of ``automations.create`` persists a DISABLED draft that
is validated by the same registered schemas as the REST API; starting it is a separate owner action.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.modules import register_modules
from core.tools import ToolDefinition, ToolRegistry, ToolResult, ToolRisk
from core.tools.registry import ToolHandler
from core.workspaces.schemas import Scope
from modules.automations import public
from modules.automations.schemas import AutomationCreate

_WRITE_OUT = {
    "type": "object", "required": ["accepted", "status_code", "result_reference"], "additionalProperties": False,
    "properties": {
        "accepted": {"type": "boolean"}, "status_code": {"type": "integer"},
        "result_reference": {"type": "string", "minLength": 1, "maxLength": 256},
    },
}


# The model-facing schema is intentionally flat (no $defs/$ref/oneOf/discriminator/titles) so every
# provider dialect accepts it, like the tasks/goals tools. It is looser than the real contract: the
# server re-validates the arguments with AutomationCreate and validate_definition when the tool runs.
_SCALAR = {"anyOf": [{"type": "string", "maxLength": 500}, {"type": "number"}, {"type": "boolean"}]}
_CREATE_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["name", "trigger", "actions"], "additionalProperties": False,
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 200},
        "trigger": {
            "type": "object", "required": ["type"], "additionalProperties": False,
            "properties": {
                "type": {"enum": ["schedule", "new_event", "new_document", "entity_changed", "task_due",
                                  "goal_deadline", "connector_sync_result"]},
                "cron": {"type": "string", "maxLength": 120}, "timezone": {"type": "string", "maxLength": 64},
                "lead_minutes": {"type": "integer", "minimum": 0, "maximum": 10080},
                "lead_days": {"type": "integer", "minimum": 0, "maximum": 365},
            },
        },
        "conditions": {"type": "array", "maxItems": 20, "items": {
            "type": "object", "required": ["field", "operator", "value"], "additionalProperties": False,
            "properties": {
                "field": {"type": "string", "maxLength": 64},
                "operator": {"enum": ["eq", "ne", "in", "gt", "gte", "lt", "lte"]},
                "value": {"anyOf": [*_SCALAR["anyOf"], {"type": "array", "maxItems": 50, "items": _SCALAR}]},
            },
        }},
        "actions": {"type": "array", "minItems": 1, "maxItems": 10, "items": {
            "type": "object", "required": ["type"], "additionalProperties": False,
            "properties": {
                "type": {"enum": ["run_agent", "create_task", "create_notification", "generate_brief", "call_webhook"]},
                "profile_id": {"enum": ["knowledge", "research", "personal", "project", "news", "planning", "automation"]},
                "instruction": {"type": "string", "maxLength": 2000},
                "title": {"type": "string", "maxLength": 500}, "description": {"type": ["string", "null"], "maxLength": 10000},
                "due_in_days": {"type": ["integer", "null"], "minimum": 0, "maximum": 365},
                "message": {"type": "string", "maxLength": 300}, "link": {"type": ["string", "null"], "maxLength": 300},
                "scope": {"enum": ["daily"]},
                "alias": {"type": "string", "maxLength": 40}, "event": {"type": "string", "maxLength": 100},
            },
        }},
    },
}


async def _list(arguments: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Read the owner's live rules (definition, enabled flag, revision) so proposals avoid duplicates."""
    async with context["session_factory"]() as session:
        page = await public.list_automations(
            session, scope=context["principal"].scope,
            multi_workspace_enabled=context["settings"].multi_workspace_enabled)
    # Compact summary keeps the result inside the byte limit even with many large rules.
    return ToolResult(success=True, data={"items": [
        {"id": str(i.id), "name": i.name, "enabled": i.enabled, "trigger": i.trigger.get("type"),
         "actions": [a.get("type") for a in i.actions]} for i in page.items]})


async def _create(arguments: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Persist one owner-approved proposal as a disabled draft rule and return its reference."""
    from modules.agents.internal_writes import run_approved_write

    async def perform(session: AsyncSession, scope: Scope) -> str:
        """Validate the proposal and create it disabled; returns ``automation:<id>``."""
        payload = AutomationCreate.model_validate({**arguments, "enabled": False})
        created = await public.create_automation(
            session, payload, register_modules(), context["settings"], scope=scope,
            multi_workspace_enabled=context["settings"].multi_workspace_enabled)
        return f"automation:{created.id}"

    return await run_approved_write(
        arguments, context, perform, (public.AutomationInvalid, public.AutomationConflict, public.AutomationMissing))


def register_automation_tools(registry: ToolRegistry, allowed_names: frozenset[str]) -> None:
    """Register automation tools the Automations descriptor declares; the write always needs approval."""
    def add(name: str, description: str, schema: dict[str, Any], handler: ToolHandler, *, write: bool,
        output: dict[str, Any] | None = None) -> None:
        """Register one definition when declared by an enabled descriptor."""
        if name not in allowed_names:
            return
        registry.register_tool(ToolDefinition(
            name=name, version="1.0.0", description=description, input_schema=schema,
            output_schema=output or _WRITE_OUT,
            risk=ToolRisk.INTERNAL_WRITE if write else ToolRisk.READ_ONLY, confirmation_required=write,
            timeout_seconds=30, max_arguments_bytes=64_000, max_result_bytes=8_192 if write else 256_000,
            permissions=("automations.write",) if write else ("automations.read",), module="automations",
        ), handler)

    add("automations.list", "List the owner's automation rules and whether each is enabled.",
        {"type": "object", "additionalProperties": False, "properties": {}}, _list, write=False,
        output={"type": "object"})
    add("automations.create", "Propose one automation rule. After explicit owner approval it is saved "
        "DISABLED as a draft; the owner enables it separately.", _CREATE_SCHEMA, _create, write=True)
