"""Harness tool execution: argument decoding, durable slots, approval resolution, effect settlement and result publication."""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.public import revalidate_account_session
from core.model_gateway.schemas import AIExecutionConfig, ModelMapping, RequestPolicy
from core.realtime import commit_with_replay
from core.tools import ToolExecutionPrincipal, ToolRisk
from core.tools.validator import validate_json_schema
from core.workspaces.schemas import AccessFence
from modules.agents._harness.context import (
    HarnessContext,
)
from modules.agents._harness.handoff import (
    run_specialist_handoff,
)
from modules.agents._harness.steps import (
    _lock_native_output_fences_for_publication,
    _reserve_step,
)
from modules.agents._harness.types import (
    MAX_TOOL_ARGUMENT_BYTES,
    MAX_TOOL_CALLS,
    MAX_TOOL_RESPONSE_BYTES,
    HarnessState,
    RunCancelled,
    RunIncompatible,
    RunLimitReached,
)
from modules.agents.handoff import (
    HANDOFF_TOOL,
)
from modules.agents.internal_writes import INTERNAL_DESTINATION, is_internal_write
from modules.agents.models import AgentRun, AgentToolCall
from modules.agents.public import (
    PROMPT_VERSION,
    SPECIALIST_WORKFLOW_VERSION,
    WORKFLOW_TOOLS,
    WORKFLOW_VERSION,
    publish_agent_activity_safely,
)


def _decode_tool_arguments(raw: Any) -> tuple[dict[str, Any], str | None]:
    """Parse model-supplied tool arguments into ``(arguments, forced_error)``.

    Non-object JSON, undecodable text, non-finite numbers and a re-encoding above
    ``MAX_TOOL_ARGUMENT_BYTES`` all yield ``invalid_arguments``; undecodable/non-object input
    returns ``{}`` so the durable slot is recorded with empty arguments. Pure; no I/O.
    """
    try:
        arguments = json.loads(raw)
    except (json.JSONDecodeError, TypeError, RecursionError):
        arguments = None
    if not isinstance(arguments, dict):
        return {}, "invalid_arguments"
    try:
        encoded_size = len(json.dumps(arguments, separators=(",", ":"), allow_nan=False).encode())
        if encoded_size > MAX_TOOL_ARGUMENT_BYTES:
            return arguments, "invalid_arguments"
    except (TypeError, ValueError, RecursionError):
        return arguments, "invalid_arguments"
    return arguments, None


def _legacy_tool_ordinal(
    context: HarnessContext,
    state: HarnessState,
    request: dict[str, str],
    index: int,
    definition: Any,
) -> int:
    """Recover the durable ordinal of a legacy (slot-less) read-only checkpoint from its messages.

    Old read-only checkpoints retain model call IDs in their assistant messages. Reuse that stable
    sequence position; never infer an old write slot from mutable counters. Raises ``RunIncompatible``
    for every identity or budget inconsistency. Pure; no I/O.
    """
    if (
        context.workflow_version != WORKFLOW_VERSION
        or context.prompt_version != PROMPT_VERSION
        or definition is None
        or request["name"] not in WORKFLOW_TOOLS
        or request["name"] not in context.allowed_tools
        or definition.risk != ToolRisk.READ_ONLY
        or definition.confirmation_required
        or not context.supports_definition(
            definition, context.tool_contracts.get(request["name"], {})
        )
    ):
        raise RunIncompatible("Legacy checkpoint does not contain a compatible read-only tool call")
    messages = state.get("messages")
    pending_calls = state.get("pending_tool_calls")
    if (
        not isinstance(messages, list)
        or not isinstance(pending_calls, list)
        or any(not isinstance(call, dict) for call in pending_calls)
    ):
        raise RunIncompatible("Legacy checkpoint tool-call state is invalid")
    legacy_groups: list[list[str]] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        group: list[str] = []
        for tool_call in tool_calls:
            call_id = tool_call.get("id") if isinstance(tool_call, dict) else None
            if not isinstance(call_id, str) or not 1 <= len(call_id) <= 128:
                raise RunIncompatible("Legacy checkpoint tool-call identity is invalid")
            group.append(call_id)
        legacy_groups.append(group)
    pending_ids = [call.get("call_id") for call in pending_calls]
    if not legacy_groups or legacy_groups[-1] != pending_ids:
        raise RunIncompatible(
            "Legacy checkpoint pending calls do not match its last assistant action"
        )
    current_id = request.get("call_id")
    if (
        not isinstance(current_id, str)
        or index >= len(pending_ids)
        or pending_ids[index] != current_id
    ):
        raise RunIncompatible("Legacy checkpoint tool-call identity is ambiguous")
    ordinal = sum(len(group) for group in legacy_groups[:-1]) + index + 1
    if ordinal > MAX_TOOL_CALLS:
        raise RunIncompatible("Legacy checkpoint tool-call count exceeds its durable budget")
    return ordinal


def _resolve_tool_slots(
    context: HarnessContext,
    state: HarnessState,
    request: dict[str, str],
    index: int,
    definition: Any,
) -> tuple[int, bool]:
    """Return ``(ordinal, legacy_readonly_slot)`` for the call at ``index``.

    Current checkpoints carry strictly increasing ``tool_slots`` within 1..MAX_TOOL_CALLS matching the
    pending calls one-to-one; legacy checkpoints (no ``tool_slots``) derive the ordinal from history.
    Raises ``RunIncompatible`` on any mismatch. Pure; no I/O.
    """
    slots = state.get("tool_slots")
    if slots is None:
        return _legacy_tool_ordinal(context, state, request, index, definition), True
    if (
        len(slots) != len(state["pending_tool_calls"])
        or any(type(slot) is not int or not 1 <= slot <= MAX_TOOL_CALLS for slot in slots)
        or any(left >= right for left, right in zip(slots, slots[1:], strict=False))
    ):
        raise RunIncompatible("Checkpoint tool-slot identities are incompatible")
    return slots[index], False


async def _load_durable_slot(
    context: HarnessContext,
    ordinal: int,
    request: dict[str, str],
    arguments: dict[str, Any],
) -> None:
    """Lock the durable call row and require it to match the checkpointed action.

    Opens a short session and takes ``FOR UPDATE`` on the (run, ordinal) row without committing.
    Raises ``RunCancelled`` when the row is missing and ``RunIncompatible`` when arguments or tool
    name differ from the checkpoint.
    """
    async with context.session_factory() as session:
        call = await session.scalar(
            select(AgentToolCall)
            .where(
                AgentToolCall.run_id == context.run_id,
                AgentToolCall.ordinal == ordinal,
            )
            .with_for_update()
        )
        if call is None:
            raise RunCancelled("Durable tool slot could not be recovered")
        if call.arguments != arguments or call.tool_name != request["name"]:
            raise RunIncompatible("Checkpointed tool action no longer matches its durable slot")


@dataclass
class _ToolOutcome:
    """Result of authorizing and invoking one tool call, before publication.

    ``waiting`` short-circuits the node with the approval-pause update; otherwise the payload,
    status, error code and evidence refs are what get stored, and ``encoded_sink`` is the
    JSON-encoded provenance to publish with them.
    """

    result_payload: dict[str, Any] = field(default_factory=dict)
    call_status: str = "denied"
    error_code: str | None = None
    evidence_refs: tuple[str, ...] = ()
    encoded_sink: dict[str, Any] = field(default_factory=dict)
    action_id: UUID | None = None
    destination: str = ""
    waiting: bool = False

    def apply(self, precomputed: tuple[dict[str, Any], str, str | None, tuple[str, ...]]) -> None:
        """Adopt a ``(payload, status, error_code, evidence_refs)`` tuple."""
        self.result_payload, self.call_status, self.error_code, self.evidence_refs = precomputed


@dataclass
class _ApprovalResolution:
    """What the approval ledger decided for one write slot.

    Hooks are left ``None`` unless the action is approved with no terminal ledger outcome, so a
    precomputed denial or replay can never reach the registry with live send authority.
    """

    waiting: bool = False
    precomputed: tuple[dict[str, Any], str, str | None, tuple[str, ...]] | None = None
    action_id: UUID | None = None
    verifier: Any = None
    before_internal_write: Any = None
    before_webhook_send: Any = None


async def _verify_approval(
    context: HarnessContext,
    row: AgentRun,
    destination: tuple[str, str],
    action_id: UUID,
    current: Any,
    values: dict[str, Any],
    _principal: ToolExecutionPrincipal,
    _phase: str,
) -> bool:
    """Match registry policy admission to this exact persisted approval and effect slot."""
    from modules.agents.approvals import verify_approved_action

    return bool(
        destination
        and action_id
        and await verify_approved_action(
            context.session_factory,
            action_id=action_id,
            run_id=context.run_id,
            owner_id=row.owner_id,
            auth_session_hash=row.auth_session_hash,
            claim_generation=context.claim_generation,
            definition=current,
            arguments=values,
            destination_id=destination[0],
            destination_revision=destination[1],
            scope=context.scope,
            original_fence=context.original_fence,
            multi_workspace_enabled=context.multi_workspace_enabled,
        )
    )


async def _reserve_internal_write(
    context: HarnessContext,
    state: HarnessState,
    internal_write: bool,
    action_id: UUID,
    definition: Any,
    arguments: dict[str, Any],
    requested_action_id: str,
) -> bool:
    """Reserve the exact approved task/goal write after current run fences pass."""
    return bool(
        internal_write
        and action_id
        and requested_action_id == str(action_id)
        and await context.authorize_internal_write(state, action_id, definition, arguments)
    )


async def _reserve_webhook_send(
    context: HarnessContext,
    state: HarnessState,
    webhook_profile: Any,
    action_id: UUID,
    definition: Any,
    arguments: dict[str, Any],
    profile: Any,
    requested_action_id: str,
) -> bool:
    """Reserve the matching effect only after current profile and source fences pass."""
    return bool(
        webhook_profile
        and action_id
        and requested_action_id == str(action_id)
        and await context.authorize_external_send(
            state,
            profile,
            action_id,
            definition,
            arguments,
            profile.alias,
        )
    )


async def _park_new_approval(
    context: HarnessContext,
    state: HarnessState,
    row: AgentRun,
    ordinal: int,
    definition: Any,
    arguments: dict[str, Any],
    destination: tuple[str, str],
) -> None:
    """Create the pending approval, then mark the durable call ``approval_pending`` and commit.

    The approval is created (and committed by its own helper) before the call row is locked and
    flipped, so the owner can never see a parked call without an approval to decide.
    """
    from modules.agents.approvals import create_pending_approval

    # Old checkpoints lack the frozen group field; their cumulative sink is
    # retained as conservative historical provenance, never as a clean marker.
    approval_fences = state.get("pending_source_fences")
    if approval_fences is None:
        approval_fences = state["source_fences"]
    await create_pending_approval(
        context.session_factory,
        run_id=context.run_id,
        owner_id=row.owner_id,
        auth_session_hash=row.auth_session_hash,
        claim_generation=context.claim_generation,
        ordinal=ordinal,
        definition=definition,
        arguments=arguments,
        destination_id=destination[0],
        destination_revision=destination[1],
        source_fences=approval_fences,
        expiry_hours=context.settings.approval_expiry_hours,
        scope=context.scope,
        original_fence=context.original_fence,
        multi_workspace_enabled=context.multi_workspace_enabled,
    )
    async with context.session_factory() as session:
        pending_call = await session.scalar(
            select(AgentToolCall)
            .where(
                AgentToolCall.run_id == context.run_id,
                AgentToolCall.ordinal == ordinal,
            )
            .with_for_update()
        )
        if pending_call is not None:
            pending_call.status = "approval_pending"
            await session.commit()


async def _replay_effect_outcome(
    context: HarnessContext,
    action_id: UUID,
) -> tuple[bool, tuple[dict[str, Any], str, str | None, tuple[str, ...]] | None]:
    """Consult the effect ledger for an approved action: ``(waiting, precomputed_result)``.

    A recorded success with a well-formed 2xx code and reference replays as accepted; a recorded
    failure replays as ``execution_failed``; in-flight/review entries (and malformed successes)
    pause the run. No entry returns ``(False, None)`` so the tool may run.
    """
    from modules.agents.approvals import get_effect_outcome

    prior_outcome = await get_effect_outcome(context.session_factory, action_id)
    if prior_outcome is None:
        return False, None
    if prior_outcome[0] == "succeeded":
        prior_code, prior_reference = prior_outcome[1], prior_outcome[2]
        if (
            type(prior_code) is int
            and 200 <= prior_code < 300
            and isinstance(prior_reference, str)
            and 1 <= len(prior_reference) <= 256
        ):
            return False, (
                {
                    "ok": {
                        "accepted": True,
                        "status_code": prior_code,
                        "result_reference": prior_reference,
                    }
                },
                "succeeded",
                None,
                (),
            )
        return True, None
    if prior_outcome[0] == "failed":
        return False, ({"error": "execution_failed"}, "failed", "execution_failed", ())
    if prior_outcome[0] in {"in_flight", "requires_review"}:
        return True, None
    return False, None


async def _resolve_approval(
    context: HarnessContext,
    state: HarnessState,
    request_name: str,
    arguments: dict[str, Any],
    definition: Any,
    ordinal: int,
    row: AgentRun,
) -> _ApprovalResolution:
    """Decide how an approval-gated tool (webhook or internal write) may proceed for this slot.

    Webhook actions bind a deployment profile; internal writes bind the fixed local destination;
    anything else is ``tool_unavailable``. A missing approval is created and the run parks; pending
    or review approvals park; non-approved terminal states are denied as ``approval_<status>``;
    an approved action first consults the effect ledger and otherwise receives the send/verify hooks.
    """
    from modules.agents.approvals import action_identity, approval_for_slot
    from modules.tools.public import load_webhook_profiles

    resolution = _ApprovalResolution()
    profile_alias = arguments.get("profile")
    webhook_profile = (
        load_webhook_profiles(context.settings).get(profile_alias)
        if isinstance(profile_alias, str)
        else None
    )
    internal_write = is_internal_write(definition)
    destination = (
        INTERNAL_DESTINATION
        if internal_write
        else (webhook_profile.alias, webhook_profile.revision)
        if request_name == "webhook.send" and webhook_profile is not None
        else None
    )
    if destination is None:
        resolution.precomputed = ({"error": "tool_unavailable"}, "denied", "tool_unavailable", ())
        return resolution
    action_id = resolution.action_id = action_identity(context.run_id, ordinal)
    approval = await approval_for_slot(context.session_factory, context.run_id, ordinal)
    if approval is None:
        await _park_new_approval(context, state, row, ordinal, definition, arguments, destination)
        resolution.waiting = True
        return resolution
    if approval.status in {"pending", "requires_review"}:
        resolution.waiting = True
        return resolution
    if approval.status != "approved":
        error = "approval_" + approval.status
        resolution.precomputed = ({"error": error}, "denied", error, ())
        return resolution
    resolution.waiting, resolution.precomputed = await _replay_effect_outcome(context, action_id)
    if resolution.waiting:
        return resolution
    resolution.verifier = partial(_verify_approval, context, row, destination, action_id)
    resolution.before_internal_write = partial(
        _reserve_internal_write,
        context,
        state,
        internal_write,
        action_id,
        definition,
        arguments,
    )
    resolution.before_webhook_send = partial(
        _reserve_webhook_send,
        context,
        state,
        webhook_profile,
        action_id,
        definition,
        arguments,
    )
    return resolution


async def _before_embedding_send(
    context: HarnessContext,
    state: HarnessState,
    embedding_config: AIExecutionConfig,
    embedding_mapping: ModelMapping | None,
    embedding_policy: RequestPolicy,
    source_generations: dict[UUID, int],
) -> None:
    """Recheck the live agent and original query source snapshot before each embedding send."""
    await context.authorize_embedding_send(
        state,
        embedding_config,
        embedding_mapping,
        embedding_policy,
        source_generations,
    )


async def _run_handoff(
    context: HarnessContext,
    state: HarnessState,
    ordinal: int,
    sink: dict[str, Any],
    handoff_usage: dict[str, Any],
    handoff_arguments: dict[str, Any],
) -> dict[str, Any]:
    """Delegate this slot to a specialist; sink and usage fold back into the parent."""
    return await run_specialist_handoff(
        context, state, handoff_arguments, ordinal, sink, handoff_usage
    )


def _tool_invocation_context(
    context: HarnessContext,
    state: HarnessState,
    request_name: str,
    ordinal: int,
    row: AgentRun,
    destination: str,
    sink: dict[str, Any],
    handoff_usage: dict[str, Any],
    resolution: _ApprovalResolution,
) -> dict[str, Any]:
    """Build the registry execution-context mapping handed to the tool handler.

    Specialist workflows additionally receive the slot/claim/profile fields their tools use to
    bind their own durable effects; only the handoff tool receives the delegation runner.
    """
    return {
        "session_factory": context.session_factory,
        "redis": context.redis,
        "settings": context.settings,
        "destination_id": destination,
        "destination_kind": "remote",
        "principal_revalidator": context.revalidate_principal,
        "output_fence_sink": sink,
        "before_embedding_send": partial(_before_embedding_send, context, state),
        "approval_verifier": resolution.verifier,
        "before_webhook_send": resolution.before_webhook_send,
        "before_internal_write": resolution.before_internal_write,
        "action_id": str(resolution.action_id) if resolution.action_id else None,
        "run_id": str(context.run_id),
        **(
            {"handoff_runner": partial(_run_handoff, context, state, ordinal, sink, handoff_usage)}
            if request_name == HANDOFF_TOOL
            else {}
        ),
        **(
            {
                "tool_ordinal": ordinal,
                "claim_generation": context.claim_generation,
                "profile_revision_hash": row.profile_revision_hash,
                "workflow_version": context.workflow_version,
                "remaining_active_seconds": context.remaining_active(),
            }
            if context.workflow_version == SPECIALIST_WORKFLOW_VERSION
            else {}
        ),
    }


async def _invoke_registered_tool(
    context: HarnessContext,
    state: HarnessState,
    request_name: str,
    arguments: dict[str, Any],
    definition: Any,
    ordinal: int,
    row: AgentRun,
    principal: ToolExecutionPrincipal,
    resolution: _ApprovalResolution,
    sink: dict[str, Any],
    handoff_usage: dict[str, Any],
    outcome: _ToolOutcome,
) -> None:
    """Invoke the registry tool under the remaining active-time budget and fill ``outcome``.

    Raises ``RunLimitReached`` when no time remains and ``TimeoutError`` (via ``asyncio.timeout``)
    when the tool overruns; a policy denial is a non-effect result and the model never receives raw
    exception text. Results above ``MAX_TOOL_RESPONSE_BYTES`` (or not JSON-encodable) become
    ``invalid_result`` and fall back to the previous sink so oversize output never publishes fences.
    """
    remaining = context.remaining_active()
    if remaining <= 0:
        raise RunLimitReached("Active execution budget exhausted")
    destination = outcome.destination
    async with asyncio.timeout(remaining):
        result = await context.registry.invoke_tool(
            request_name,
            arguments,
            principal,
            version=definition.version,
            context=_tool_invocation_context(
                context,
                state,
                request_name,
                ordinal,
                row,
                destination,
                sink,
                handoff_usage,
                resolution,
            ),
        )
    await context._run_snapshot()
    if result.success:
        outcome.result_payload = {"ok": result.data}
        outcome.call_status, outcome.error_code = "succeeded", None
    else:
        # Policy denial is a non-effect result; the model never receives raw exception text.
        outcome.result_payload = {"error": result.error_code or "execution_failed"}
        outcome.call_status, outcome.error_code = "denied", result.error_code or "execution_failed"
    outcome.evidence_refs = result.evidence_refs
    outcome.encoded_sink = context.encode_fences(sink)
    try:
        if (
            len(json.dumps(outcome.result_payload, separators=(",", ":"), allow_nan=False).encode())
            > MAX_TOOL_RESPONSE_BYTES
        ):
            raise ValueError("Tool result exceeds its response bound")
    except (TypeError, ValueError, RecursionError):
        outcome.result_payload = {"error": "invalid_result"}
        outcome.call_status, outcome.error_code = "failed", "invalid_result"
        outcome.encoded_sink = state["source_fences"]


async def _authorize_and_invoke(
    context: HarnessContext,
    state: HarnessState,
    request_name: str,
    arguments: dict[str, Any],
    definition: Any,
    ordinal: int,
    row: AgentRun,
    handoff_usage: dict[str, Any],
    sink: dict[str, Any],
) -> _ToolOutcome:
    """Run the approval gate (for effectful tools) and then the registry invocation.

    Refreshes the run snapshot and gateway destination first. Returns an outcome with ``waiting``
    set when the approval ledger parks the run; precomputed ledger results skip the invocation.
    """
    await context._run_snapshot()
    config = await context.read_gateway_config()
    outcome = _ToolOutcome(destination=config.endpoint_destination_id or "")
    principal = context.owner_principal(state, outcome.destination)
    resolution = _ApprovalResolution()
    if definition.risk != ToolRisk.READ_ONLY or definition.confirmation_required:
        resolution = await _resolve_approval(
            context, state, request_name, arguments, definition, ordinal, row
        )
        outcome.action_id = resolution.action_id
        if resolution.waiting:
            outcome.waiting = True
            return outcome
    if resolution.precomputed is not None:
        outcome.apply(resolution.precomputed)
        outcome.encoded_sink = state["source_fences"]
        return outcome
    await _invoke_registered_tool(
        context,
        state,
        request_name,
        arguments,
        definition,
        ordinal,
        row,
        principal,
        resolution,
        sink,
        handoff_usage,
        outcome,
    )
    return outcome


async def _settle_effect_state(context: HarnessContext, outcome: _ToolOutcome) -> bool:
    """Close any effect left in flight and report whether the run must pause for owner review.

    A tool that returned (timeout/denial) while the send fence was crossed has an unknown outcome:
    it is closed as ``requires_review`` so the model can never re-propose it, and the outcome is
    rewritten to a failed ``requires_review`` result. No-op for calls without an action ID.
    """
    if outcome.action_id is None:
        return False
    from modules.agents.approvals import get_effect_state, mark_effect_outcome

    action_id = outcome.action_id
    effect_state = await get_effect_state(context.session_factory, action_id)
    if effect_state == "in_flight":
        await mark_effect_outcome(
            context.session_factory,
            str(action_id),
            "requires_review",
            f"action:{action_id}",
        )
        effect_state = "requires_review"
    if effect_state != "requires_review":
        return False
    outcome.result_payload = {"error": "requires_review"}
    outcome.call_status, outcome.error_code = "failed", "requires_review"
    return True


def _require_publishable_run(context: HarnessContext, run: AgentRun | None) -> AgentRun:
    """Return the locked run row if it still belongs to this claim, else raise ``RunCancelled``.

    The row must exist, be owned by the context's owner, be ``running`` under the same claim generation and be
    neither cancel-requested nor evidence-revoked; anything else means publication must not proceed.
    """
    if (
        run is None
        or run.owner_id != context.owner_id
        or run.status != "running"
        or run.claim_generation != context.claim_generation
        or run.cancel_requested
        or run.evidence_revoked
    ):
        raise RunCancelled("Run was cancelled before tool publication")
    return run


async def _publish_tool_result(
    context: HarnessContext,
    state: HarnessState,
    request: dict[str, str],
    ordinal: int,
    outcome: _ToolOutcome,
    tool_messages: list[dict[str, Any]],
    *,
    publish_provenance: bool,
) -> None:
    """Persist the call result, activity and (when allowed) source provenance in one transaction.

    Lock order: access fence (original epoch, ``admit_original(lock=True)``), then native output
    fences (privacy, Source, Document), then the run row, then the call row. Final fence staleness
    rewrites the outcome (and its tool message) to ``forbidden``. Provenance is written to the run
    row before the commit so cleanup sees it before the saver can checkpoint the result. Raises
    ``RunCancelled`` when the run, session, Chat link or lease is no longer valid.
    """
    async with context.session_factory() as session:
        # Access fence first (original epoch), then privacy, Source, Document and run locks.
        publication_fence = await context.admit_original(session, lock=True)
        if publish_provenance:
            await _recheck_publication_fences(
                context,
                state,
                session,
                publication_fence,
                request,
                outcome,
                tool_messages,
            )
        run = _require_publishable_run(
            context,
            await session.scalar(
                select(AgentRun)
                .where(
                    AgentRun.id == context.run_id,
                    AgentRun.workspace_id == context.scope.workspace_id,
                )
                .with_for_update()
            ),
        )
        # Approval invalidation takes run→approval→effect→call; publication must lock
        # run→call too, or a stale-approval GET can deadlock against a completed tool.
        stored_call = await session.scalar(
            select(AgentToolCall)
            .where(
                AgentToolCall.run_id == context.run_id,
                AgentToolCall.ordinal == ordinal,
            )
            .with_for_update()
        )
        if not await revalidate_account_session(
            session,
            run.auth_session_hash,
            run.owner_id,
            multi_workspace_enabled=context.multi_workspace_enabled,
        ):
            raise RunCancelled("Owner session expired before tool publication")
        from modules.chat.public import has_live_agent_run_link

        if run.chat_link_required and not await has_live_agent_run_link(
            session,
            run.id,
            run.owner_id,
            run.auth_session_hash,
        ):
            raise RunCancelled("Chat link expired before tool publication")
        await context.assert_lease()
        if stored_call is not None:
            stored_call.status = outcome.call_status
            stored_call.error_code = outcome.error_code
            stored_call.evidence_refs = list(outcome.evidence_refs)
            stored_call.completed_at = datetime.now(UTC)
        if publish_provenance:
            # Publish provenance before the saver can checkpoint this result; cleanup queries
            # the durable run fence even if worker segment accounting has not run yet.
            run.source_fences = outcome.encoded_sink
        entries = list(run.activities)
        entries.append(
            {
                "kind": "tool",
                "status": outcome.call_status,
                "tool_name": request["name"][:160],
                "created_at": datetime.now(UTC).isoformat(),
            }
        )
        run.activities = entries[-64:]
        await commit_with_replay(
            session,
            (),
            scope=context.scope,
            multi_workspace_enabled=context.multi_workspace_enabled,
            access_fence=publication_fence,
        )


async def _recheck_publication_fences(
    context: HarnessContext,
    state: HarnessState,
    session: AsyncSession,
    publication_fence: AccessFence,
    request: dict[str, str],
    outcome: _ToolOutcome,
    tool_messages: list[dict[str, Any]],
) -> None:
    """Lock and revalidate native output fences under the publication transaction.

    A failed or erroring check (canonical locks make it serialize with hard deletion) turns the
    outcome into a denied ``forbidden`` result, restores the previous sink and rewrites the
    already-appended tool message for this call.
    """
    try:
        fences_current = await _lock_native_output_fences_for_publication(
            session,
            context.session_factory,
            outcome.encoded_sink,
            context.owner_principal(state, outcome.destination),
            multi_workspace_enabled=context.multi_workspace_enabled,
            access_fence=publication_fence,
        )
    except (TypeError, ValueError, KeyError, RunCancelled):
        fences_current = False
    if fences_current:
        return
    outcome.result_payload = {"error": "forbidden"}
    outcome.call_status, outcome.error_code, outcome.evidence_refs = "denied", "forbidden", ()
    outcome.encoded_sink = state["source_fences"]
    if tool_messages and tool_messages[-1].get("tool_call_id") == request["call_id"]:
        tool_messages[-1]["content"] = json.dumps(outcome.result_payload, separators=(",", ":"))


def _merge_handoff_usage(state: HarnessState, handoff_usage: dict[str, Any]) -> dict[str, Any]:
    """Fold a specialist's token usage into the parent's total (not a separate budget).

    Returns the ``token_usage``/``token_usage_unknown`` state update, or ``{}`` without handoff
    usage. A total reaching 2**31 becomes unknown. Pure; no I/O.
    """
    if not handoff_usage:
        return {}
    prior, spent = state.get("token_usage"), handoff_usage.get("tokens")
    total = (
        prior + spent
        if prior is not None and spent is not None
        else spent
        if spent is not None
        else prior
    )
    return {
        "token_usage": total if total is None or total < 2**31 else None,
        "token_usage_unknown": bool(
            state.get("token_usage_unknown")
            or handoff_usage.get("unknown")
            or (total is not None and total >= 2**31)
        ),
    }


def _tool_is_exact(context: HarnessContext, name: str, definition: Any) -> bool:
    """Report whether ``name`` resolves to a registered, allowed tool whose frozen contract is still supported.

    Pure predicate over the run's allow-list and contracts; an inexact tool is denied as ``tool_unavailable``
    by ``execute_tool`` without invocation.
    """
    return (
        definition is not None
        and name in context.allowed_tools
        and context.supports_definition(definition, context.tool_contracts.get(name, {}))
    )


async def execute_tool(context: HarnessContext, state: HarnessState) -> dict[str, Any]:
    """Invoke one exact tool in a replay-stable slot and fence output against its run and Chat lifetime.

    A run created with a Chat link must retain that live link before publishing tool output.
    The persisted requirement marker preserves never-linked legacy read-only runs while deleted
    or expired original links remain a cancellation condition. Pipeline: decode arguments, resolve
    the durable slot, reserve and lock it, authorize (approval ledger) and invoke, settle the effect
    state, then publish result and provenance in one transaction before reporting activity.
    """
    index = state["tool_index"]
    if index >= len(state["pending_tool_calls"]):
        return {"segment_done": True}
    request = state["pending_tool_calls"][index]
    if context.remaining_active() <= 0:
        raise RunLimitReached("Active execution budget exhausted")
    arguments, forced_error = _decode_tool_arguments(request["arguments"])
    definition = context.registry.get_tool(request["name"])
    ordinal, legacy_readonly_slot = _resolve_tool_slots(context, state, request, index, definition)
    row = await _reserve_step(
        context,
        tool_name=request["name"],
        tool_ordinal=ordinal,
        tool_definition=definition,
        arguments=arguments,
        input_source_fences=state.get("pending_source_fences"),
        legacy_readonly_slot=legacy_readonly_slot,
    )
    exact = _tool_is_exact(context, request["name"], definition)
    if (
        exact
        and definition is not None
        and validate_json_schema(arguments, definition.input_schema)
    ):
        forced_error = "invalid_arguments"
    await _load_durable_slot(context, ordinal, request, arguments)
    tool_messages = list(state["messages"])
    sink = context.decode_fences(state["source_fences"])
    handoff_usage: dict[str, Any] = {}
    if forced_error or not exact or definition is None:
        error = forced_error or "tool_unavailable"
        outcome = _ToolOutcome(
            {"error": error}, "denied", error, encoded_sink=state["source_fences"]
        )
    else:
        outcome = await _authorize_and_invoke(
            context,
            state,
            request["name"],
            arguments,
            definition,
            ordinal,
            row,
            handoff_usage,
            sink,
        )
        if outcome.waiting:
            return {"waiting_approval": True, "segment_done": True}
    pause_for_review = await _settle_effect_state(context, outcome)
    if not pause_for_review:
        tool_messages.append(
            {
                "role": "tool",
                "tool_call_id": request["call_id"],
                "content": json.dumps(
                    outcome.result_payload, ensure_ascii=False, separators=(",", ":")
                ),
            }
        )
    await _publish_tool_result(
        context,
        state,
        request,
        ordinal,
        outcome,
        tool_messages,
        publish_provenance=bool(exact and not forced_error and not pause_for_review),
    )
    await publish_agent_activity_safely(
        context.session_factory,
        run_id=context.run_id,
        owner_id=row.owner_id,
        auth_session_hash=row.auth_session_hash,
        status=outcome.call_status,
        tool_name=request["name"],
    )
    if pause_for_review:
        return {"waiting_approval": True, "segment_done": True}
    return {
        "messages": tool_messages,
        "tool_index": index + 1,
        "waiting_approval": False,
        "segment_steps": state["segment_steps"] + 1,
        "source_fences": outcome.encoded_sink
        if exact and not forced_error
        else state["source_fences"],
        **_merge_handoff_usage(state, handoff_usage),
    }
