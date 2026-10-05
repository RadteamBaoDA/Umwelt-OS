"""Supervisor-only read tool that delegates one bounded request to a specialist inside the same run.

The tool definition and its refusal vocabulary live here; execution is owned by the harness
(``modules.agents.harness.run_specialist_handoff``), which injects a ``handoff_runner`` callable
into the registry call context. No second orchestration path exists: the specialist runs the very
same compiled workflow, limits and fences as any other profile run.
"""

from typing import Any

from core.tools import ToolDefinition, ToolRegistry, ToolResult, ToolRisk
from modules.agents.internal_writes import INTERNAL_WRITE_TOOLS

HANDOFF_TOOL = "agents.handoff"
# Supervisor and Automation are never targets: depth is 1, and Automation belongs to Phase 10.
HANDOFF_TARGETS = ("knowledge", "research", "personal", "project", "news", "planning")
# Tools a delegated specialist never receives: effects need the owner approval card of a directly
# started run, nested handoff would exceed depth 1, and browser budget is bound to the run profile.
HANDOFF_EXCLUDED_TOOLS = frozenset({"webhook.send", HANDOFF_TOOL, "browser.read", *INTERNAL_WRITE_TOOLS})
MAX_HANDOFF_ANSWER_CHARS = 12_000
MAX_HANDOFF_CITATIONS = 20


class HandoffRefused(Exception):
    """Carry a registry-safe error code for a refused delegation; no detail reaches the model."""

    def __init__(self, code: str) -> None:
        """Remember the normalized tool error code (forbidden, invalid_arguments, tool_unavailable)."""
        super().__init__(code)
        self.code = code


async def _handle_handoff(arguments: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Delegate through the harness-supplied runner or report the tool as unavailable outside a run."""
    runner = context.get("handoff_runner")
    if not callable(runner):
        return ToolResult(success=False, error="Handoff is unavailable", error_code="tool_unavailable")
    try:
        data = await runner(arguments)
    except HandoffRefused as exc:
        return ToolResult(success=False, error="Handoff refused", error_code=exc.code)
    return ToolResult(success=True, data=data)


def register_handoff_tool(registry: ToolRegistry, allowed_names: frozenset[str]) -> None:
    """Register the read-only handoff tool when the Agents descriptor declares it."""
    if HANDOFF_TOOL not in allowed_names:
        return
    registry.register_tool(ToolDefinition(
        name=HANDOFF_TOOL, version="1.0.0",
        description=(
            "Delegate one request to a specialist agent and receive only its final answer and "
            "citations. Call it alone, at most once per run."
        ),
        input_schema={
            "type": "object", "required": ["specialist", "request"], "additionalProperties": False,
            "properties": {
                "specialist": {"type": "string", "enum": list(HANDOFF_TARGETS)},
                "request": {"type": "string", "minLength": 1, "maxLength": 4000},
            },
        },
        output_schema={
            "type": "object", "required": ["specialist", "answer", "truncated", "citations"],
            "additionalProperties": False,
            "properties": {
                "specialist": {"type": "string"},
                "answer": {"type": "string", "maxLength": MAX_HANDOFF_ANSWER_CHARS},
                "truncated": {"type": "boolean"},
                "citations": {"type": "array", "maxItems": MAX_HANDOFF_CITATIONS, "items": {"type": "object"}},
            },
        },
        risk=ToolRisk.READ_ONLY, confirmation_required=False, timeout_seconds=120,
        max_arguments_bytes=8_000, max_result_bytes=200_000,
        permissions=(HANDOFF_TOOL,), module="agents",
    ), _handle_handoff)
