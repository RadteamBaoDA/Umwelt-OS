"""Specialist handoff driver: snapshot resolution, child segment loop, fence folding and ``run_specialist_handoff``."""

from typing import Any, cast

from fastapi import HTTPException
from sqlalchemy import func, or_, select

from core.errors import DomainError
from modules.agents._harness_context import (
    HarnessContext,
)
from modules.agents._harness_steps import (
    HandoffContext,
    _assert_handoff_allowed,
)
from modules.agents._harness_types import (
    HarnessState,
)
from modules.agents.handoff import (
    HANDOFF_EXCLUDED_TOOLS,
    HANDOFF_TARGETS,
    HANDOFF_TOOL,
    MAX_HANDOFF_ANSWER_CHARS,
    MAX_HANDOFF_CITATIONS,
    HandoffRefused,
)
from modules.agents.models import AgentToolCall


async def _resolve_specialist_snapshot(
    parent: HarnessContext,
    specialist: str,
    ordinal: int,
) -> tuple[dict[str, Any], str]:
    """Check the single-delegation budget and resolve the specialist's pinned profile snapshot.

    Opens one read session on the parent's factory: counts other durable handoff slots of this run,
    then resolves the profile against the live gateway config. A disabled/unavailable specialist
    or stale revision (``HTTPException``) is refused as ``tool_unavailable`` rather than degraded.
    """
    from modules.agents.specialists import get_profile, resolve_profile_snapshot

    async with parent.session_factory() as session:
        used = await session.scalar(
            select(func.count())
            .select_from(AgentToolCall)
            .where(
                AgentToolCall.run_id == parent.run_id,
                AgentToolCall.tool_name == HANDOFF_TOOL,
                AgentToolCall.ordinal != ordinal,
                # Refusals are 'denied' with other codes and do not consume the single delegation.
                or_(
                    AgentToolCall.status.in_(("started", "succeeded", "failed")),
                    AgentToolCall.error_code.in_(("timeout", "execution_failed", "invalid_result")),
                ),
            )
        )
        if used:
            raise HandoffRefused("forbidden")
        config = await parent.read_gateway_config()
        try:
            profile = await get_profile(
                session,
                specialist,
                parent.registry,
                config,
                scope=parent.scope,
                multi_workspace_enabled=parent.multi_workspace_enabled,
            )
            return await resolve_profile_snapshot(
                session,
                specialist,
                profile.revision,
                parent.registry,
                config,
                scope=parent.scope,
                multi_workspace_enabled=parent.multi_workspace_enabled,
            )
        except (HTTPException, DomainError) as exc:
            # Disabled, unavailable model/tools or a stale revision: refuse rather than degrade.
            raise HandoffRefused("tool_unavailable") from exc


async def _drive_child_segments(child: HandoffContext, holder: dict[str, Any]) -> bool:
    """Run the specialist graph for at most eight bounded segments and report whether it answered.

    ``holder["state"]`` always carries the latest child state (updated after every segment) so the
    caller's token accounting still sees partial usage when a later segment raises. Raises
    ``RuntimeError`` when the child waits for approval, ends without an answer, or never finishes
    within the segment bound; returns ``True`` once an answer is ready.
    """
    # Deferred import: the workflow module wires ``execute_tool``, which reaches this handoff driver, so a
    # module-level import would form a cycle (workflow -> tools -> handoff -> workflow).
    from modules.agents._harness_workflow import build_workflow

    graph = build_workflow(child, None)
    for _ in range(8):
        holder["state"] = child_state = await graph.ainvoke(
            holder["state"], {"recursion_limit": 32}
        )
        if child_state.get("waiting_approval"):
            raise RuntimeError("Specialist handoff cannot wait for approval")
        if child_state.get("answer") is not None and not child_state.get("segment_done"):
            return True
        if not child_state.get("segment_done"):
            raise RuntimeError("Specialist workflow ended without an answer")
        holder["state"] = {**child_state, "segment_steps": 0, "segment_done": False}
    raise RuntimeError("Specialist handoff exceeded its segment bound")


def _fold_child_fences(
    parent: HarnessContext,
    child_state: dict[str, Any],
    sink: dict[str, Any],
) -> list[dict[str, Any]]:
    """Merge the specialist's source fences into the parent sink and derive bounded citations.

    Raises ``RuntimeError`` when the merged record count or a source generation would conflict with
    the parent's sink; otherwise extends the sink in place and returns de-duplicated citations.
    """
    fences = parent.decode_fences(child_state["source_fences"])
    records, generations = sink["records"], sink["source_generations"]
    if len(records) + len(fences["records"]) > 100 or any(
        generations.get(key, value) != value for key, value in fences["source_generations"].items()
    ):
        raise RuntimeError("Specialist source fences exceed the parent's bound")
    generations.update(fences["source_generations"])
    records.extend(fences["records"])
    seen: set[tuple[Any, ...]] = set()
    citations: list[dict[str, Any]] = []
    for fence in fences["records"]:
        key = (fence.document_version_id, fence.chunk_id)
        if key not in seen and len(citations) < MAX_HANDOFF_CITATIONS:
            seen.add(key)
            citations.append(
                {
                    "document_id": str(fence.document_id),
                    "document_version_id": str(fence.document_version_id),
                    "source_id": str(fence.source_id),
                    "chunk_id": str(fence.chunk_id) if fence.chunk_id else None,
                }
            )
    return citations


async def run_specialist_handoff(
    parent: HarnessContext,
    state: HarnessState,
    arguments: dict[str, Any],
    ordinal: int,
    sink: dict[str, Any],
    usage_box: dict[str, Any],
) -> dict[str, Any]:
    """Run one specialist sub-run and return only its final answer and citations.

    Invariants: depth is 1 (only a top-level Supervisor may delegate); the handoff is the turn's
    sole tool call, so the specialist's durable tool ordinals continue the parent's counter and
    consume the parent's 10-call budget; delegation is allowed once per run, judged from durable
    tool-call rows so a replay of this same slot is not a second use. The specialist is stateless
    here: if a worker segment is cut mid-handoff, replay re-runs the read-only specialist and
    its budget is charged again, never skipped. Effects are not offered to the specialist, so
    no approval can be bypassed. Source scope is the intersection of both profiles. The specialist
    inherits the frozen parent input fences because its request text was model-generated from them;
    unknown legacy lineage remains unavailable through the child call slots.
    """
    parent_snapshot = cast(dict[str, Any], parent.profile_snapshot)
    frozen_parent_fences = _assert_handoff_allowed(parent, state)
    specialist, request_text = arguments["specialist"], arguments["request"]
    if specialist not in HANDOFF_TARGETS:
        raise HandoffRefused("invalid_arguments")
    snapshot, digest = await _resolve_specialist_snapshot(parent, specialist, ordinal)
    tools = [
        item for item in snapshot["allowed_tools"] if item["name"] not in HANDOFF_EXCLUDED_TOOLS
    ]
    if not tools:
        raise HandoffRefused("tool_unavailable")
    parent_sources = {str(item) for item in parent_snapshot.get("source_ids", [])}
    snapshot["allowed_tools"] = tools
    snapshot["source_ids"] = [item for item in snapshot["source_ids"] if item in parent_sources]
    child = HandoffContext(
        parent,
        snapshot,
        frozenset(item["name"] for item in tools),
        {
            item["name"]: {"version": item["version"], "fingerprint": item["fingerprint"]}
            for item in tools
        },
    )
    holder: dict[str, Any] = {
        "state": {
            "prompt": request_text,
            "messages": [],
            "pending_tool_calls": [],
            "tool_slots": [],
            "tool_index": 0,
            "source_fences": frozen_parent_fences or {"records": [], "source_generations": {}},
            "source_provenance_available": frozen_parent_fences is not None,
            "answer": None,
            "segment_steps": 0,
            "segment_done": False,
            "token_usage": None,
            "token_usage_unknown": False,
            "profile_id": specialist,
            "profile_revision_hash": digest,
            "owner_record_state": "authorized",
        }
    }
    completed = False
    try:
        completed = await _drive_child_segments(child, holder)
        child_state = holder["state"]
        await child.authorize_remote_send(cast(HarnessState, child_state))
        citations = _fold_child_fences(parent, child_state, sink)
        answer = child_state["answer"]
        return {
            "specialist": specialist,
            "answer": answer[:MAX_HANDOFF_ANSWER_CHARS],
            "truncated": len(answer) > MAX_HANDOFF_ANSWER_CHARS,
            "citations": citations,
        }
    finally:
        # Token accounting: fold the specialist's usage into the parent run even when it failed.
        child_state = holder["state"]
        usage_box["tokens"] = child_state.get("token_usage")
        usage_box["unknown"] = (
            bool(child_state.get("token_usage_unknown"))
            or child.unobservable_model_usage
            or not completed
        )
        parent.unobservable_model_usage = (
            parent.unobservable_model_usage or child.unobservable_model_usage
        )
