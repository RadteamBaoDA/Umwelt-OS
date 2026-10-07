"""Owner-approved internal writes (task/goal mutations) on the P07 approval and effect ledger.

Internal writes reuse the exact lifecycle of ``webhook.send``: a durable pending approval bound to the
canonical arguments, a ``reserved`` effect created on approval, a pre-write fence that flips it to
``in_flight``, and a terminal outcome. They differ only in destination: a fixed local pseudo-destination
instead of a webhook profile, so no network profile can be substituted.
"""

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from core.tools import ToolResult

# Fixed approval destination; the revision only changes if this contract itself changes.
INTERNAL_DESTINATION = ("local", "1")

TASK_GOAL_WRITE_TOOLS = frozenset({
    "tasks.create", "tasks.update", "tasks.complete", "tasks.delete",
    "goals.create", "goals.update", "goals.delete",
})
# The Automation Agent proposes a DISABLED draft rule; enabling it is a separate owner action.
AUTOMATION_PROFILE_TOOLS = frozenset({"automations.create", "automations.list"})
INTERNAL_WRITE_TOOLS = TASK_GOAL_WRITE_TOOLS | {"automations.create"}
# Profiles whose plan says they use task/goal tools; every other profile never sees them.
INTERNAL_WRITE_PROFILES = frozenset({"personal", "planning"})
INTERNAL_READ_TOOLS = frozenset({"tasks.list", "goals.list", "goals.get"})
INTERNAL_PROFILE_TOOLS = TASK_GOAL_WRITE_TOOLS | INTERNAL_READ_TOOLS


def is_internal_write(definition: Any) -> bool:
    """Return whether a registered definition is one of the approval-gated internal writes."""
    return (
        definition.name in INTERNAL_WRITE_TOOLS and definition.confirmation_required
        and definition.risk.value == "INTERNAL_WRITE"
    )


async def run_approved_write(
    arguments: dict[str, Any], context: dict[str, Any],
    perform: Callable[[Any, int], Awaitable[str]],
    domain_errors: tuple[type[Exception], ...],
) -> ToolResult:
    """Run one approved write exactly once and record its outcome on the effect ledger.

    ``perform(session, owner_id)`` executes the owner-scoped public service call and returns a short
    reference such as ``task:<id>``. The pre-write fence (``before_internal_write``) re-checks the
    approval, session, run claim and Chat link; a denied fence is a clean failure with no write. Domain
    validation errors leave the database untouched and are recorded as ``failed``; anything ambiguous
    (including cancellation after the fence) becomes ``requires_review`` so it can never be replayed.
    """
    import asyncio

    from modules.agents.approvals import mark_effect_outcome

    action_id, before = context.get("action_id"), context.get("before_internal_write")
    factory = context.get("session_factory")
    if not isinstance(action_id, str) or not callable(before) or factory is None:
        return ToolResult(success=False, error="Approved action is unavailable", error_code="forbidden")
    if not await before(action_id):
        await mark_effect_outcome(factory, action_id, "failed", None)
        return ToolResult(success=False, error="Approved action is unavailable", error_code="forbidden")
    caught: tuple[type[BaseException], ...] = (*domain_errors, ValidationError, ValueError)
    try:
        async with factory() as session:
            reference = await perform(session, 1)
    except asyncio.CancelledError:
        await asyncio.shield(mark_effect_outcome(factory, action_id, "requires_review", f"action:{action_id}"))
        raise
    except caught as exc:
        await mark_effect_outcome(factory, action_id, "failed", None)
        return ToolResult(success=False, error=type(exc).__name__, error_code="execution_failed")
    except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
        await mark_effect_outcome(factory, action_id, "requires_review", f"action:{action_id}")
        return ToolResult(success=False, error="Write outcome requires review", error_code="execution_failed")
    await mark_effect_outcome(factory, action_id, "succeeded", reference, result_status_code=200)
    return ToolResult(success=True, data={"accepted": True, "status_code": 200, "result_reference": reference})


def uuid_arg(value: Any) -> UUID:
    """Parse a UUID argument, raising ValueError (a clean ``failed`` outcome) when malformed."""
    return UUID(str(value))
