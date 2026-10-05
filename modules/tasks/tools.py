"""Task agent tools on the P07 ToolRegistry: one read tool and approval-gated writes."""

from typing import Any

from core.tools import ToolDefinition, ToolRegistry, ToolResult, ToolRisk
from modules.tasks import public
from modules.tasks.schemas import TaskCreate, TaskFilter, TaskUpdate

_STATUS = ["inbox", "todo", "in_progress", "blocked", "done", "cancelled"]
_UUID = {"type": "string", "format": "uuid"}
_WRITE_OUT = {
    "type": "object", "required": ["accepted", "status_code", "result_reference"], "additionalProperties": False,
    "properties": {
        "accepted": {"type": "boolean"}, "status_code": {"type": "integer"},
        "result_reference": {"type": "string", "minLength": 1, "maxLength": 256},
    },
}
_REVISION = {"type": "integer", "minimum": 1, "maximum": 9007199254740991}
_TASK_FIELDS = {
    "title": {"type": "string", "minLength": 1, "maxLength": 500},
    "description": {"type": ["string", "null"], "maxLength": 10000},
    "status": {"enum": _STATUS},
    "due_date": {"type": ["string", "null"], "format": "date"},
    "due_at": {"type": ["string", "null"], "format": "date-time"},
    "goal_id": {"type": ["string", "null"], "format": "uuid"},
    "entity_ids": {"type": "array", "maxItems": 100, "items": _UUID},
}


async def _list(arguments: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Read one bounded owner-scoped task page; no write path and no session from the arguments."""
    async with context["session_factory"]() as session:
        page = await public.list_tasks(session, 1, TaskFilter.model_validate(arguments))
    return ToolResult(success=True, data=page.model_dump(mode="json"))


def _writer(action):
    """Wrap an owner-scoped task mutation in the approved-write lifecycle."""
    async def handler(arguments: dict[str, Any], context: dict[str, Any]) -> ToolResult:
        """Execute exactly one approved task mutation and record its effect outcome."""
        from modules.agents.internal_writes import run_approved_write, uuid_arg

        async def perform(session, owner_id: int) -> str:
            """Run the public task service call and return its reference."""
            return await action(session, owner_id, arguments, uuid_arg)

        return await run_approved_write(arguments, context, perform, (public.TaskConflict, public.TaskMissing))
    return handler


async def _create(session, owner_id, args, uuid_arg) -> str:
    """Create a task from validated arguments."""
    return f"task:{(await public.create_task(session, owner_id, TaskCreate.model_validate(args))).id}"


async def _update(session, owner_id, args, uuid_arg) -> str:
    """Patch a task under its expected revision."""
    body = {key: value for key, value in args.items() if key != "task_id"}
    updated = await public.update_task(session, owner_id, uuid_arg(args["task_id"]), TaskUpdate.model_validate(body))
    return f"task:{updated.id}"


async def _complete(session, owner_id, args, uuid_arg) -> str:
    """Mark a task done under its expected revision."""
    payload = TaskUpdate(status="done", expected_revision=args["expected_revision"])
    return f"task:{(await public.update_task(session, owner_id, uuid_arg(args['task_id']), payload)).id}"


async def _delete(session, owner_id, args, uuid_arg) -> str:
    """Soft-delete a task under its expected revision."""
    task_id = uuid_arg(args["task_id"])
    await public.delete_task(session, owner_id, task_id, args["expected_revision"])
    return f"task:{task_id}"


def register_task_tools(registry: ToolRegistry, allowed_names: frozenset[str]) -> None:
    """Register task tools the Tasks descriptor declares; writes always require durable approval."""
    def add(name: str, description: str, schema: dict[str, Any], handler, *, write: bool, output=None) -> None:
        """Register one definition when declared by an enabled descriptor."""
        if name not in allowed_names:
            return
        registry.register_tool(ToolDefinition(
            name=name, version="1.0.0", description=description, input_schema=schema,
            output_schema=output or _WRITE_OUT,
            risk=ToolRisk.INTERNAL_WRITE if write else ToolRisk.READ_ONLY, confirmation_required=write,
            timeout_seconds=30, max_arguments_bytes=64_000, max_result_bytes=8_192 if write else 256_000,
            permissions=("tasks.write",) if write else ("tasks.read",), module="tasks",
        ), handler)

    add("tasks.list", "List the owner's tasks with optional view, status, goal and text filters.", {
        "type": "object", "additionalProperties": False,
        "properties": {
            "view": {"enum": ["inbox", "today", "upcoming", "blocked", "completed", "all"]},
            "status": {"enum": _STATUS}, "goal_id": _UUID, "q": {"type": "string", "maxLength": 300},
            "timezone": {"type": "string", "maxLength": 64}, "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
    }, _list, write=False, output={"type": "object"})
    add("tasks.create", "Create one task after explicit owner approval.", {
        "type": "object", "required": ["title"], "additionalProperties": False, "properties": _TASK_FIELDS,
    }, _writer(_create), write=True)
    add("tasks.update", "Update one task at its expected revision after explicit owner approval.", {
        "type": "object", "required": ["task_id", "expected_revision"], "additionalProperties": False,
        "properties": {"task_id": _UUID, "expected_revision": _REVISION, **_TASK_FIELDS},
    }, _writer(_update), write=True)
    add("tasks.complete", "Mark one task done at its expected revision after explicit owner approval.", {
        "type": "object", "required": ["task_id", "expected_revision"], "additionalProperties": False,
        "properties": {"task_id": _UUID, "expected_revision": _REVISION},
    }, _writer(_complete), write=True)
    add("tasks.delete", "Delete one task at its expected revision after explicit owner approval.", {
        "type": "object", "required": ["task_id", "expected_revision"], "additionalProperties": False,
        "properties": {"task_id": _UUID, "expected_revision": _REVISION},
    }, _writer(_delete), write=True)
