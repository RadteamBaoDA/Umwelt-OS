"""Harness graph routing and ``build_workflow`` assembly."""

from functools import partial
from typing import Any, Literal

from modules.agents._harness.context import (
    HarnessContext,
)
from modules.agents._harness.model import (
    call_model,
)
from modules.agents._harness.tools import (
    execute_tool,
)
from modules.agents._harness.types import (
    SEGMENT_GRAPH_STEPS,
    HarnessState,
)


def _route_model(state: HarnessState) -> Literal["execute_tool", "yield_segment", "finish"]:
    """Select the next sequential call or end the current bounded graph segment."""
    if state["segment_steps"] >= SEGMENT_GRAPH_STEPS:
        return "yield_segment"
    return "execute_tool" if state["pending_tool_calls"] else "finish"


def _route_tool(state: HarnessState) -> Literal["execute_tool", "call_model", "yield_segment"]:
    """Continue one-at-a-time tool dispatch, return to the model, or release this worker segment."""
    if state.get("waiting_approval"):
        return "yield_segment"
    if state["segment_steps"] >= SEGMENT_GRAPH_STEPS:
        return "yield_segment"
    return (
        "execute_tool" if state["tool_index"] < len(state["pending_tool_calls"]) else "call_model"
    )


async def _yield_segment(context: HarnessContext, state: HarnessState) -> dict[str, Any]:
    """Persist a continuation marker so ARQ capacity is released between bounded graph segments."""
    await context._run_snapshot()
    return {"segment_done": True}


def _finish(state: HarnessState) -> dict[str, Any]:
    """Mark the final answer ready for owner publication without exposing hidden model fields."""
    return {"segment_done": False}


def _route_start(state: HarnessState) -> Literal["execute_tool", "call_model", "finish"]:
    """Resume pending tool dispatch or a produced answer before requesting another model turn."""
    if state.get("answer") is not None:
        return "finish"
    return (
        "execute_tool"
        if state.get("tool_index", 0) < len(state.get("pending_tool_calls", []))
        else "call_model"
    )


def build_workflow(context: HarnessContext, checkpointer: Any) -> Any:
    """Compile the single sequential read-only workflow with a JSON-safe PostgreSQL checkpointer.

    Node functions live at module level and are bound to ``context`` with ``functools.partial`` so
    each is independently testable; the compiled graph topology is unchanged.
    """
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(HarnessState)
    builder.add_node("call_model", partial(call_model, context))
    builder.add_node("execute_tool", partial(execute_tool, context))
    builder.add_node("yield_segment", partial(_yield_segment, context))
    builder.add_node("finish", _finish)
    builder.add_conditional_edges(START, _route_start)
    builder.add_conditional_edges("call_model", _route_model)
    builder.add_conditional_edges("execute_tool", _route_tool)
    builder.add_edge("yield_segment", END)
    builder.add_edge("finish", END)
    return builder.compile(checkpointer=checkpointer)
