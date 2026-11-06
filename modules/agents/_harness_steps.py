"""Harness step reservation, publication-fence locking, tool specs and the handoff child context."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.realtime import commit_with_replay
from core.tools import ToolExecutionPrincipal
from core.workspaces.schemas import AccessFence
from modules.agents._harness_context import (
    HarnessContext,
)
from modules.agents._harness_types import (
    MAX_STEPS,
    MAX_TOOL_CALLS,
    HarnessState,
    RunCancelled,
    RunIncompatible,
    RunLimitReached,
)
from modules.agents.handoff import (
    HandoffRefused,
)
from modules.agents.models import AgentRun, AgentToolCall
from modules.tools.public import revalidate_native_output_fences


async def _lock_native_output_fences_for_publication(
    session: AsyncSession,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    encoded: dict[str, Any],
    principal: ToolExecutionPrincipal,
    *,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> bool:
    """Hold privacy→Source→Document fences through a later Agent publication commit.

    The ordinary Tools revalidation uses short independent read sessions, so by itself it cannot
    serialize a successful check with hard deletion. Acquire canonical owner locks before AgentRun
    locks, then revalidate while those locks remain held through the caller's transaction commit.
    ``access_fence`` is the caller's already locked original fence (it precedes every lock here);
    each Source lock re-compares it and the Document locks run under the principal's scope.
    """
    fences = HarnessContext.decode_fences(encoded)
    records = fences["records"]
    generations = fences["source_generations"]
    if len(records) > 100 or len(generations) > 100:
        return False
    if not records and not generations:
        return await revalidate_native_output_fences(
            session_factory,
            fences,
            principal,
            destination_kind="remote",
            multi_workspace_enabled=multi_workspace_enabled,
        )
    from modules.knowledge.documents import public as documents
    from modules.memory.public import lock_export_privacy
    from modules.sources import public as sources

    await lock_export_privacy(session, principal.scope.workspace_id, shared=True)
    for source_id, generation in sorted(generations.items(), key=lambda item: str(item[0])):
        source = await sources.lock_source(
            session,
            source_id,
            scope=principal.scope,
            multi_workspace_enabled=multi_workspace_enabled,
            expected_access_fence=access_fence,
        )
        if source is None or source.status != "active" or source.generation != generation:
            return False
    document_ids = sorted({item.document_id for item in records}, key=str)
    if (
        await documents.lock_document_ids(
            session,
            document_ids,
            scope=principal.scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        != document_ids
    ):
        return False
    return await revalidate_native_output_fences(
        session_factory,
        fences,
        principal,
        destination_kind="remote",
        multi_workspace_enabled=multi_workspace_enabled,
    )


def _check_slot_replay(
    row: AgentRun,
    existing: AgentToolCall | None,
    tool_ordinal: int,
    input_source_fences: dict[str, Any] | None,
    recover_legacy_reservation: bool,
) -> None:
    """Reject a tool-slot reservation that disagrees with the persisted run counters or recorded model input.

    A replayed slot must present the same captured model-input evidence, a new slot must be the next ordinal
    (unless a legacy read-only reservation is being recovered), and an existing slot cannot exceed the persisted
    tool counter. Raises ``RunIncompatible``; reads only, no side effects.
    """
    existing_call = existing.id if existing is not None else None
    if existing is not None and existing.input_provenance_version == 1:
        if input_source_fences is None or existing.input_source_fences != input_source_fences:
            raise RunIncompatible("Tool slot model-input evidence changed")
    if (
        existing_call is None
        and not recover_legacy_reservation
        and row.tool_calls != tool_ordinal - 1
    ):
        raise RunIncompatible("Persisted tool counter has no matching durable action slot")
    if existing_call is not None and row.tool_calls < tool_ordinal:
        raise RunIncompatible("Durable action slot exceeds the persisted tool counter")


async def _reserve_step(
    context: HarnessContext,
    *,
    tool_name: str | None = None,
    tool_ordinal: int | None = None,
    tool_definition: Any = None,
    arguments: dict[str, Any] | None = None,
    input_source_fences: dict[str, Any] | None = None,
    legacy_readonly_slot: bool = False,
) -> AgentRun:
    """Consume one budget unit and bind a new durable call slot to its frozen model-input evidence.

    A replay must present the same captured context for a classified slot. Legacy checkpoints with
    no group snapshot remain explicitly unavailable instead of being assigned an empty dependency set.
    """
    await context.assert_lease()
    async with context.session_factory() as session:
        fence = await context.admit_original(session, lock=True)
        row = await session.scalar(
            select(AgentRun)
            .where(
                AgentRun.id == context.run_id,
                AgentRun.workspace_id == context.scope.workspace_id,
            )
            .with_for_update()
        )
        if (
            row is None
            or row.status != "running"
            or row.cancel_requested
            or row.evidence_revoked
            or row.claim_generation != context.claim_generation
        ):
            raise RunCancelled("Run was cancelled or reclaimed")
        if row.steps >= MAX_STEPS or context.remaining_active() <= 0:
            raise RunLimitReached("Run step or active-time budget exhausted")
        existing = (
            await session.scalar(
                select(AgentToolCall).where(
                    AgentToolCall.run_id == context.run_id,
                    AgentToolCall.ordinal == tool_ordinal,
                )
            )
            if tool_ordinal is not None
            else None
        )
        existing_call = existing.id if existing is not None else None
        recover_legacy_reservation = (
            legacy_readonly_slot
            and existing_call is None
            and tool_ordinal is not None
            and row.tool_calls >= tool_ordinal
        )
        if tool_name is not None and tool_ordinal is not None:
            _check_slot_replay(
                row, existing, tool_ordinal, input_source_fences, recover_legacy_reservation
            )
        if (
            tool_name is not None
            and existing_call is None
            and not recover_legacy_reservation
            and row.tool_calls >= MAX_TOOL_CALLS
        ):
            raise RunLimitReached("Tool-call budget exhausted")
        row.steps += int(existing_call is None and not recover_legacy_reservation)
        if tool_name is not None and existing_call is None:
            if not recover_legacy_reservation:
                row.tool_calls += 1
            session.add(
                AgentToolCall(
                    run_id=context.run_id,
                    ordinal=tool_ordinal,
                    tool_name=tool_name[:160],
                    tool_version=tool_definition.version if tool_definition else None,
                    schema_fingerprint=tool_definition.schema_fingerprint
                    if tool_definition
                    else None,
                    arguments=arguments or {},
                    status="started",
                    input_source_fences=input_source_fences,
                    input_provenance_version=1 if input_source_fences is not None else None,
                )
            )
        await commit_with_replay(
            session,
            (),
            scope=context.scope,
            multi_workspace_enabled=context.multi_workspace_enabled,
            access_fence=fence,
        )
        session.expunge(row)
        return row


def _tool_specs(context: HarnessContext) -> list[dict[str, Any]]:
    """Project exact current registered READ_ONLY tools to the OpenAI-compatible call format."""
    items = {
        item.name: item
        for item in context.registry.list_tools(
            allowed_tools=context.allowed_tools, modules=context.modules
        )
    }
    if set(items) != set(context.allowed_tools):
        raise RunIncompatible("A persisted workflow tool is no longer registered")
    for name, definition in items.items():
        expected = context.tool_contracts.get(name, {})
        if not context.supports_definition(definition, expected):
            raise RunIncompatible("A persisted workflow tool contract changed")
    return [
        {
            "type": "function",
            "function": {
                "name": item.name,
                "description": item.description,
                "parameters": item.input_schema,
            },
        }
        for item in sorted(items.values(), key=lambda value: value.name)
    ]


class HandoffContext(HarnessContext):
    """Run a specialist's compiled workflow inside its supervisor's claimed run, row and lease.

    The child shares the parent's run id, claim generation, PostgreSQL lease and active-time clock,
    so every step, tool call and second it consumes is charged to the parent's limits, and a
    parent cancellation, session expiry or lease loss reaches it through ``_run_snapshot``. Only
    the profile (tools, source scope, pinned gateway policy) is the specialist's own.
    """

    def __init__(
        self,
        parent: HarnessContext,
        profile_snapshot: dict[str, Any],
        allowed_tools: frozenset[str],
        tool_contracts: dict[str, dict[str, str]],
    ) -> None:
        """Bind the specialist profile to the parent's process handles and budget clock."""
        super().__init__(
            parent.run_id,
            parent.scope,
            parent.original_fence,
            parent.claim_generation,
            parent.session_factory,
            parent.engine,
            parent.settings,
            parent.redis,
            parent.registry,
            parent.lease_connection,
            parent.lease_key,
            parent.segment_started,
            allowed_tools,
            tool_contracts,
            parent.active_seconds,
            workflow_version=parent.workflow_version,
            prompt_version=parent.prompt_version,
            profile_snapshot=profile_snapshot,
        )
        self.parent = parent
        self.handoff_depth = parent.handoff_depth + 1

    async def _run_snapshot(self) -> AgentRun:
        """Validate the durable PARENT row; the specialist has no run row or profile pin of its own."""
        return await self.parent._run_snapshot()


def _assert_handoff_allowed(parent: HarnessContext, state: HarnessState) -> Any:
    """Enforce depth, supervisor identity, sole-call and frozen-fence preconditions of a handoff.

    Returns the frozen parent input fences the specialist inherits. Any violation is a
    ``HandoffRefused("forbidden")``; no database access happens here.
    """
    parent_snapshot = parent.profile_snapshot
    if (
        parent.handoff_depth != 0
        or parent_snapshot is None
        or parent_snapshot.get("id") != "supervisor"
        or len(state["pending_tool_calls"]) != 1
    ):
        raise HandoffRefused("forbidden")
    frozen_parent_fences = state.get("pending_source_fences")
    if (
        frozen_parent_fences is None
        or frozen_parent_fences != state.get("source_fences")
        or state.get("source_provenance_available", True) is not True
    ):
        raise HandoffRefused("forbidden")
    return frozen_parent_fences
