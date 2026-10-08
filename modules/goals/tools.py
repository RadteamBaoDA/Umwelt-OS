"""Goal agent tools on the P07 ToolRegistry: reads plus approval-gated writes.

Plan proposals are not a tool: tasks are only created by the owner accepting a plan in the UI or by
an individually approved ``tasks.create`` call.
"""

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.tools import ToolDefinition, ToolRegistry, ToolResult, ToolRisk
from core.tools.registry import ToolHandler
from core.workspaces.schemas import Scope
from modules.goals import public
from modules.goals.schemas import GoalCreate, GoalFilter, GoalUpdate

_Action = Callable[[AsyncSession, Scope, bool, dict[str, Any], Callable[[Any], UUID]], Awaitable[str]]
_UUID = {"type": "string", "format": "uuid"}
_REVISION = {"type": "integer", "minimum": 1, "maximum": 9007199254740991}
_WRITE_OUT = {
    "type": "object", "required": ["accepted", "status_code", "result_reference"], "additionalProperties": False,
    "properties": {
        "accepted": {"type": "boolean"}, "status_code": {"type": "integer"},
        "result_reference": {"type": "string", "minLength": 1, "maxLength": 256},
    },
}
_MILESTONE = {
    "type": "object", "required": ["title"], "additionalProperties": False,
    "properties": {
        "id": _UUID, "title": {"type": "string", "minLength": 1, "maxLength": 500}, "completed": {"type": "boolean"},
        "due_date": {"type": ["string", "null"], "format": "date"}, "order": {"type": "integer"},
        "task_id": {"type": ["string", "null"], "format": "uuid"},
    },
}
_GOAL_FIELDS = {
    "title": {"type": "string", "minLength": 1, "maxLength": 500},
    "description": {"type": ["string", "null"], "maxLength": 10000},
    "desired_outcome": {"type": ["string", "null"], "maxLength": 10000},
    "deadline": {"type": ["string", "null"], "format": "date"},
    "progress": {"type": ["number", "null"], "minimum": 0, "maximum": 100},
    "manual_progress": {"type": "boolean"},
    "status": {"enum": ["active", "completed", "paused", "cancelled"]},
    "milestones": {"type": "array", "maxItems": 100, "items": _MILESTONE},
    "entity_ids": {"type": "array", "maxItems": 100, "items": _UUID},
}


async def _list(arguments: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Read one bounded owner-scoped goal page."""
    async with context["session_factory"]() as session:
        page = await public.list_goals(
            session, GoalFilter.model_validate(arguments), scope=context["principal"].scope,
            multi_workspace_enabled=context["settings"].multi_workspace_enabled,
        )
    return ToolResult(success=True, data=page.model_dump(mode="json"))


async def _get(arguments: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Read one owner goal by ID; a missing goal is a clean tool failure."""
    async with context["session_factory"]() as session:
        try:
            goal = await public.get_goal(
                session, UUID(arguments["goal_id"]), scope=context["principal"].scope,
                multi_workspace_enabled=context["settings"].multi_workspace_enabled,
            )
        except public.GoalMissing:
            return ToolResult(success=False, error="Goal not found", error_code="execution_failed")
    return ToolResult(success=True, data=goal.model_dump(mode="json"))


def _writer(action: _Action) -> ToolHandler:
    """Wrap an owner-scoped goal mutation in the approved-write lifecycle."""
    async def handler(arguments: dict[str, Any], context: dict[str, Any]) -> ToolResult:
        """Execute exactly one approved goal mutation and record its effect outcome."""
        from modules.agents.internal_writes import run_approved_write, uuid_arg

        async def perform(session: AsyncSession, scope: Scope) -> str:
            """Run the public goal service call with the admitted approval scope."""
            return await action(session, scope, context["settings"].multi_workspace_enabled, arguments, uuid_arg)

        return await run_approved_write(arguments, context, perform, (public.GoalConflict, public.GoalMissing))
    return handler


async def _create(
    session: AsyncSession, scope: Scope, multi_workspace_enabled: bool,
    args: dict[str, Any], uuid_arg: Callable[[Any], UUID],
) -> str:
    """Create a goal from validated arguments."""
    return f"goal:{(await public.create_goal(session, GoalCreate.model_validate(args), scope=scope, multi_workspace_enabled=multi_workspace_enabled)).id}"


async def _update(
    session: AsyncSession, scope: Scope, multi_workspace_enabled: bool,
    args: dict[str, Any], uuid_arg: Callable[[Any], UUID],
) -> str:
    """Patch a goal under its expected revision."""
    body = {key: value for key, value in args.items() if key != "goal_id"}
    updated = await public.update_goal(session, uuid_arg(args["goal_id"]), GoalUpdate.model_validate(body), scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return f"goal:{updated.id}"


async def _delete(
    session: AsyncSession, scope: Scope, multi_workspace_enabled: bool,
    args: dict[str, Any], uuid_arg: Callable[[Any], UUID],
) -> str:
    """Delete a goal under its expected revision."""
    goal_id = uuid_arg(args["goal_id"])
    await public.delete_goal(session, goal_id, args["expected_revision"], scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return f"goal:{goal_id}"


def register_goal_tools(registry: ToolRegistry, allowed_names: frozenset[str]) -> None:
    """Register goal tools the Goals descriptor declares; writes always require durable approval."""
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
            permissions=("goals.write",) if write else ("goals.read",), module="goals",
        ), handler)

    add("goals.list", "List the owner's goals with optional status and text filters.", {
        "type": "object", "additionalProperties": False,
        "properties": {
            "status": {"enum": ["active", "completed", "paused", "cancelled"]},
            "q": {"type": "string", "maxLength": 300}, "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
    }, _list, write=False, output={"type": "object"})
    add("goals.get", "Read one goal with its milestones and progress.", {
        "type": "object", "required": ["goal_id"], "additionalProperties": False, "properties": {"goal_id": _UUID},
    }, _get, write=False, output={"type": "object"})
    add("goals.create", "Create one goal after explicit owner approval.", {
        "type": "object", "required": ["title"], "additionalProperties": False, "properties": _GOAL_FIELDS,
    }, _writer(_create), write=True)
    add("goals.update", "Update one goal at its expected revision after explicit owner approval.", {
        "type": "object", "required": ["goal_id", "expected_revision"], "additionalProperties": False,
        "properties": {"goal_id": _UUID, "expected_revision": _REVISION, **_GOAL_FIELDS},
    }, _writer(_update), write=True)
    add("goals.delete", "Delete one goal at its expected revision after explicit owner approval.", {
        "type": "object", "required": ["goal_id", "expected_revision"], "additionalProperties": False,
        "properties": {"goal_id": _UUID, "expected_revision": _REVISION},
    }, _writer(_delete), write=True)
