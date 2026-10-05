"""Durable immutable approval decisions and bounded external-effect recovery contracts."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4, uuid5

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.public import revalidate_owner_session
from core.tools.schemas import ToolDefinition, ToolExecutionPrincipal, compute_argument_hash
from modules.agents.models import AgentApproval, AgentEffect, AgentRun, AgentToolCall

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


def action_identity(run_id: UUID, ordinal: int) -> UUID:
    """Derive a stable effect key from the durable run/tool slot, never a model-generated call ID."""
    if not 1 <= ordinal <= 10:
        raise ValueError("Tool ordinal is outside the durable run bound")
    return uuid5(run_id, f"tool-slot:{ordinal}")


async def create_pending_approval(
    session_factory: SessionFactory,
    *,
    run_id: UUID,
    owner_id: int,
    auth_session_hash: str,
    claim_generation: int,
    ordinal: int,
    definition: ToolDefinition,
    arguments: dict[str, object],
    destination_id: str,
    destination_revision: str,
    source_fences: dict[str, object],
    expiry_hours: int,
) -> AgentApproval:
    """Create or reuse only the exact approval bound to a claimed durable tool slot.

    The run lock serializes retries before the unique `(run_id, ordinal)` constraint. The saved
    operation binds registered contract, canonical arguments, source evidence, session and profile.
    """
    action_id = action_identity(run_id, ordinal)
    argument_hash = compute_argument_hash(arguments)
    now = datetime.now(UTC)
    async with session_factory() as session:
        if not await revalidate_owner_session(session, auth_session_hash, owner_id):
            raise HTTPException(status_code=401, detail="Owner session expired")
        from modules.chat.public import has_live_agent_run_link
        if not await has_live_agent_run_link(session, run_id, owner_id, auth_session_hash):
            raise HTTPException(status_code=409, detail="A live Chat link is required for approval")
        run = await session.scalar(select(AgentRun).where(AgentRun.id == run_id).with_for_update())
        if (run is None or run.owner_id != owner_id or run.status != "running" or run.cancel_requested
                or run.claim_generation != claim_generation or run.auth_session_hash != auth_session_hash):
            raise HTTPException(status_code=409, detail="Agent action is no longer current")
        row = await session.scalar(select(AgentApproval).where(
            AgentApproval.run_id == run_id, AgentApproval.ordinal == ordinal,
        ).with_for_update())
        if (not await revalidate_owner_session(session, auth_session_hash, owner_id)
                or not await has_live_agent_run_link(session, run_id, owner_id, auth_session_hash)):
            raise HTTPException(status_code=409, detail="Owner session or Chat link is no longer live")
        if row is None:
            row = AgentApproval(
                id=uuid4(), action_id=action_id,
                run_id=run_id, owner_id=owner_id, auth_session_hash=auth_session_hash,
                ordinal=ordinal, tool_name=definition.name, tool_version=definition.version,
                schema_fingerprint=definition.schema_fingerprint, arguments=arguments,
                argument_hash=argument_hash, destination_id=destination_id,
                destination_revision=destination_revision, source_fences=source_fences,
                status="pending", expires_at=now + timedelta(hours=expiry_hours),
            )
            session.add(row)
        elif (row.action_id != action_id or row.owner_id != owner_id or row.auth_session_hash != auth_session_hash
              or row.tool_name != definition.name or row.tool_version != definition.version
              or row.schema_fingerprint != definition.schema_fingerprint or row.argument_hash != argument_hash
              or row.destination_id != destination_id or row.destination_revision != destination_revision
              or row.source_fences != source_fences):
            raise HTTPException(status_code=409, detail="Tool slot no longer matches its approval")
        await session.commit()
        await session.refresh(row)
        session.expunge(row)
        return row


async def verify_approved_action(
    session_factory: SessionFactory,
    *,
    action_id: UUID,
    run_id: UUID,
    owner_id: int,
    auth_session_hash: str,
    claim_generation: int,
    definition: ToolDefinition,
    arguments: dict[str, object],
    destination_id: str,
    destination_revision: str,
) -> bool:
    """Re-read approved action, effect reservation, run claim and original session before dispatch."""
    async with session_factory() as session:
        row = await session.scalar(select(AgentApproval).where(AgentApproval.action_id == action_id))
        run = await session.scalar(select(AgentRun).where(AgentRun.id == run_id))
        effect = await session.scalar(select(AgentEffect).where(AgentEffect.action_id == action_id))
        if (row is None or run is None or effect is None or row.run_id != run_id
                or row.owner_id != owner_id or row.auth_session_hash != auth_session_hash
                or run.owner_id != owner_id or run.auth_session_hash != auth_session_hash
                or run.claim_generation != claim_generation or run.status != "running" or run.cancel_requested
                or row.status != "approved" or row.expires_at <= datetime.now(UTC)
                or effect.state != "reserved" or effect.payload is None
                or row.tool_name != definition.name or row.tool_version != definition.version
                or row.schema_fingerprint != definition.schema_fingerprint
                or row.argument_hash != compute_argument_hash(arguments) or row.arguments != arguments
                or row.destination_id != destination_id or row.destination_revision != destination_revision
                or effect.payload_hash != row.argument_hash or effect.payload != row.arguments
                or not await revalidate_owner_session(session, auth_session_hash, owner_id)):
            return False
        return True


async def approval_for_slot(session_factory: SessionFactory, run_id: UUID, ordinal: int) -> AgentApproval | None:
    """Read the durable decision for one immutable run/tool ordinal without returning unrelated actions."""
    action_id = action_identity(run_id, ordinal)
    async with session_factory() as session:
        row = await session.scalar(select(AgentApproval).where(AgentApproval.action_id == action_id))
        if row is not None:
            session.expunge(row)
        return row


async def reserve_effect_before_send(
    session_factory: SessionFactory,
    *,
    action_id: UUID,
    run_id: UUID,
    owner_id: int,
    auth_session_hash: str,
    claim_generation: int,
    definition: ToolDefinition,
    arguments: dict[str, object],
    destination_id: str,
    destination_revision: str,
) -> bool:
    """Commit the one-send tombstone under run→approval→effect locks after rechecking session and Chat fences.

    Revalidate the original session before locks and again after all three locks, immediately before
    changing the durable state. The webhook transport separately checks source/profile fences after DNS.
    """
    async with session_factory() as session:
        # Do the latest auth query before row locks; a second send fence occurs after DNS resolution.
        if not await revalidate_owner_session(session, auth_session_hash, owner_id):
            return False
        run = await session.scalar(select(AgentRun).where(AgentRun.id == run_id).with_for_update())
        approval = await session.scalar(select(AgentApproval).where(
            AgentApproval.action_id == action_id,
        ).with_for_update())
        effect = await session.scalar(select(AgentEffect).where(
            AgentEffect.action_id == action_id,
        ).with_for_update())
        now = datetime.now(UTC)
        if (run is None or approval is None or effect is None or run.owner_id != owner_id
                or run.auth_session_hash != auth_session_hash or run.status != "running"
                or run.cancel_requested or run.claim_generation != claim_generation
                or approval.run_id != run_id or approval.owner_id != owner_id
                or approval.auth_session_hash != auth_session_hash or approval.status != "approved"
                or approval.expires_at <= now or effect.state != "reserved" or effect.payload is None
                or approval.tool_name != definition.name or approval.tool_version != definition.version
                or approval.schema_fingerprint != definition.schema_fingerprint
                or approval.arguments != arguments or approval.argument_hash != compute_argument_hash(arguments)
                or approval.destination_id != destination_id
                or approval.destination_revision != destination_revision
                or effect.payload_hash != approval.argument_hash or effect.payload != approval.arguments):
            return False
        from modules.chat.public import has_live_agent_run_link
        if (not await revalidate_owner_session(session, auth_session_hash, owner_id)
                or not await has_live_agent_run_link(session, run_id, owner_id, auth_session_hash)):
            return False
        effect.state = "in_flight"
        await session.commit()
        return True


async def mark_approval_requires_review(
    session_factory: SessionFactory, run_id: UUID, action_id: UUID,
) -> None:
    """Set a fail-closed review stop without making approval or replay capable of clearing uncertainty."""
    async with session_factory() as session:
        run = await session.scalar(select(AgentRun).where(AgentRun.id == run_id).with_for_update())
        row = await session.scalar(select(AgentApproval).where(
            AgentApproval.action_id == action_id, AgentApproval.run_id == run_id,
        ).with_for_update())
        if run is not None and row is not None and row.status == "approved":
            row.status = "requires_review"
            row.resolved_at = datetime.now(UTC)
            await session.commit()


async def mark_effect_outcome(
    session_factory: SessionFactory,
    action_id: str,
    state: str,
    result_reference: str | None,
    result_status_code: int | None = None,
) -> None:
    """Record one provider outcome, retaining review tombstones when cancellation wins before a late receipt."""
    if state not in {"succeeded", "failed", "requires_review"}:
        raise ValueError("Effect outcome is not terminal")
    if result_status_code is not None and not 100 <= result_status_code <= 599:
        raise ValueError("Effect response status is outside the HTTP range")
    async with session_factory() as session:
        candidate_run_id = await session.scalar(select(AgentEffect.run_id).where(
            AgentEffect.action_id == UUID(action_id),
        ))
        if candidate_run_id is None:
            return
        # Keep the same lock order as decision/cancellation while closing a provider attempt.
        await session.scalar(select(AgentRun).where(AgentRun.id == candidate_run_id).with_for_update())
        approval = await session.scalar(select(AgentApproval).where(
            AgentApproval.action_id == UUID(action_id),
        ).with_for_update())
        effect = await session.scalar(select(AgentEffect).where(
            AgentEffect.action_id == UUID(action_id),
        ).with_for_update())
        if effect is None:
            return
        if effect.state == "requires_review":
            # Cancellation may win the row lock while a request already on the wire returns 2xx.
            # Record that receipt on the tombstone without restoring payload or clearing review.
            if (state == "succeeded" and result_status_code is not None
                    and 200 <= result_status_code < 300
                    and (result_reference is None or
                         (isinstance(result_reference, str) and 1 <= len(result_reference) <= 256))):
                effect.result_status_code = result_status_code
                effect.result_reference = result_reference
                effect.payload = None
                await session.commit()
            return
        if effect.state not in {"in_flight", "reserved"}:
            return
        effect.state = state
        effect.result_status_code = result_status_code
        effect.result_reference = result_reference
        effect.payload = None
        if state == "requires_review" and approval is not None:
            approval.status = "requires_review"
            approval.resolved_at = datetime.now(UTC)
        await session.commit()


async def get_effect_state(session_factory: SessionFactory, action_id: UUID) -> str | None:
    """Read only the durable effect state needed to decide whether checkpoint replay must stop."""
    async with session_factory() as session:
        return await session.scalar(select(AgentEffect.state).where(AgentEffect.action_id == action_id))


async def get_effect_outcome(
    session_factory: SessionFactory, action_id: UUID,
) -> tuple[str, int | None, str | None] | None:
    """Read only persisted safe effect outcome fields needed to replay a tool result without sending."""
    async with session_factory() as session:
        row = await session.execute(select(
            AgentEffect.state, AgentEffect.result_status_code, AgentEffect.result_reference,
        ).where(AgentEffect.action_id == action_id))
        value = row.one_or_none()
        return (value[0], value[1], value[2]) if value is not None else None


async def decision(
    session: AsyncSession,
    *,
    session_factory: SessionFactory,
    approval_id: UUID,
    owner_id: int,
    auth_session_hash: str,
    approve: bool,
    expiry_hours: int,
    destination_revision: str,
    definition: ToolDefinition | None,
) -> tuple[AgentApproval, AgentRun]:
    """Resolve one exact owner decision under run→approval→effect locks and queue its bounded continuation."""
    if not await revalidate_owner_session(session, auth_session_hash, owner_id):
        raise HTTPException(status_code=401, detail="Owner session expired")
    candidate = await session.scalar(select(AgentApproval).where(AgentApproval.id == approval_id))
    if candidate is None or candidate.owner_id != owner_id or candidate.auth_session_hash != auth_session_hash:
        raise HTTPException(status_code=404, detail="Approval not found")
    from modules.chat.public import has_live_agent_run_link
    if candidate.arguments is None or not await has_live_agent_run_link(
        session, candidate.run_id, owner_id, auth_session_hash,
    ):
        raise HTTPException(status_code=404, detail="Approval not found")
    from modules.agents.public import _restore_fences
    from modules.tools.public import revalidate_native_output_fences

    principal = ToolExecutionPrincipal(
        actor_id=f"owner:{owner_id}", is_owner=True, allowed_tools=frozenset({candidate.tool_name}),
        owner_all_sources=True, destinations=frozenset(), capabilities=frozenset({"source.read"}),
    )
    try:
        fences_current = await revalidate_native_output_fences(
            session_factory,
            _restore_fences(candidate.source_fences), principal, destination_kind="remote",
        )
    except (TypeError, ValueError, KeyError):
        fences_current = False
    if not fences_current:
        return await _cancel_stale_action(session, candidate, "source_permissions_changed")
    if candidate.destination_revision != destination_revision:
        return await _cancel_stale_action(session, candidate, "webhook_profile_changed")
    run = await session.scalar(select(AgentRun).where(AgentRun.id == candidate.run_id).with_for_update())
    row = await session.scalar(select(AgentApproval).where(AgentApproval.id == approval_id).with_for_update())
    effect = None
    if approve:
        effect = await session.scalar(select(AgentEffect).where(
            AgentEffect.action_id == candidate.action_id,
        ).with_for_update())
    if run is None or row is None or run.cancel_requested or run.status == "cancelled":
        raise HTTPException(status_code=409, detail="Agent run is no longer active")
    if row.status in {"approved", "denied"}:
        if row.status == ("approved" if approve else "denied"):
            return row, run
        raise HTTPException(status_code=409, detail="Approval already has the opposite decision")
    if row.status != "pending" or run.status != "waiting_approval":
        raise HTTPException(status_code=409, detail="Approval is not awaiting a decision")
    if (run.owner_id != owner_id or run.auth_session_hash != auth_session_hash
            or row.run_id != run.id or row.owner_id != owner_id):
        raise HTTPException(status_code=409, detail="Approval authorization changed")
    if (not await revalidate_owner_session(session, auth_session_hash, owner_id)
            or not await has_live_agent_run_link(session, run.id, owner_id, auth_session_hash)):
        raise HTTPException(status_code=409, detail="Owner session or Chat link is no longer live")
    if row.expires_at <= datetime.now(UTC):
        row.status = "expired"
        row.resolved_at = datetime.now(UTC)
        await _resume_denied(session, run, row, "approval_expired")
        run.status, run.dispatch_generation = "queued", run.dispatch_generation + 1
        run.updated_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(row)
        await session.refresh(run)
        return row, run
    if approve and (definition is None or definition.name != row.tool_name
                    or definition.version != row.tool_version
                    or definition.schema_fingerprint != row.schema_fingerprint):
        row.status = "cancelled"
        row.resolved_at = datetime.now(UTC)
        await _resume_denied(session, run, row, "tool_contract_changed")
        run.status, run.dispatch_generation = "queued", run.dispatch_generation + 1
        run.updated_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(row)
        await session.refresh(run)
        return row, run
    if approve:
        if effect is None:
            effect = AgentEffect(
                action_id=row.action_id, run_id=run.id, provider_key=str(row.action_id),
                profile_alias=row.destination_id, profile_revision=row.destination_revision,
                payload=row.arguments, payload_hash=row.argument_hash, state="reserved",
            )
            session.add(effect)
        elif effect.state != "reserved" or effect.payload_hash != row.argument_hash:
            row.status = "requires_review"
            row.resolved_at = datetime.now(UTC)
            await session.commit()
            return row, run
        row.status = "approved"
        row.resolved_at = datetime.now(UTC)
    else:
        row.status = "denied"
        row.resolved_at = datetime.now(UTC)
        await _resume_denied(session, run, row, "approval_denied")
    run.status = "queued"
    run.dispatch_generation += 1
    run.updated_at = datetime.now(UTC)
    run.activities = [*run.activities[-63:], {
        "kind": "status", "status": "queued", "created_at": datetime.now(UTC).isoformat(),
    }]
    await session.commit()
    await session.refresh(row)
    await session.refresh(run)
    return row, run


async def _resume_denied(session: AsyncSession, run: AgentRun, row: AgentApproval, code: str) -> None:
    """Publish a bounded denial into the reserved tool slot so the model continues without an effect."""
    call = await session.scalar(select(AgentToolCall).where(
        AgentToolCall.run_id == run.id, AgentToolCall.ordinal == row.ordinal,
    ).with_for_update())
    if call is not None:
        call.status, call.error_code, call.completed_at = "denied", code, datetime.now(UTC)


async def _cancel_stale_action(
    session: AsyncSession, candidate: AgentApproval, code: str,
) -> tuple[AgentApproval, AgentRun]:
    """Resolve a stale pending action as a bounded denial while preserving its immutable audit hash."""
    run = await session.scalar(select(AgentRun).where(
        AgentRun.id == candidate.run_id,
    ).with_for_update())
    row = await session.scalar(select(AgentApproval).where(
        AgentApproval.id == candidate.id,
    ).with_for_update())
    from modules.chat.public import has_live_agent_run_link
    if (not await revalidate_owner_session(session, candidate.auth_session_hash, candidate.owner_id)
            or not await has_live_agent_run_link(
                session, candidate.run_id, candidate.owner_id, candidate.auth_session_hash,
            )):
        raise HTTPException(status_code=404, detail="Approval not found")
    if run is None or row is None or run.cancel_requested or run.status == "cancelled":
        raise HTTPException(status_code=409, detail="Agent run is no longer active")
    if row.status != "pending" or run.status != "waiting_approval":
        raise HTTPException(status_code=409, detail="Approval is no longer awaiting a decision")
    row.status = "cancelled"
    row.resolved_at = datetime.now(UTC)
    await _resume_denied(session, run, row, code)
    run.status = "queued"
    run.dispatch_generation += 1
    run.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(row)
    await session.refresh(run)
    return row, run


async def expire_pending_approvals(session_factory: SessionFactory, limit: int = 25) -> int:
    """Expire a bounded page of waiting actions and enqueue their safe denied tool result."""
    async with session_factory() as session:
        candidates = list((await session.execute(select(
            AgentApproval.id, AgentApproval.run_id,
        ).where(AgentApproval.status == "pending", AgentApproval.expires_at <= datetime.now(UTC))
          .order_by(AgentApproval.expires_at).limit(limit))).all())
        expired = 0
        for approval_id, run_id in candidates:
            run = await session.scalar(select(AgentRun).where(AgentRun.id == run_id).with_for_update())
            row = await session.scalar(select(AgentApproval).where(AgentApproval.id == approval_id).with_for_update())
            if run is None or row is None or row.status != "pending" or row.expires_at > datetime.now(UTC):
                continue
            row.status, row.resolved_at = "expired", datetime.now(UTC)
            await _resume_denied(session, run, row, "approval_expired")
            if run.status == "waiting_approval" and not run.cancel_requested:
                run.status, run.dispatch_generation = "queued", run.dispatch_generation + 1
                run.updated_at = datetime.now(UTC)
            expired += 1
        if expired:
            await session.commit()
        return expired
