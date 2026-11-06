"""Agent approval listing, lookup and stale-approval invalidation under owner fences."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import (
    NotFoundError,
)
from core.tools.schemas import ToolExecutionPrincipal
from core.workspaces.schemas import Scope
from modules.agents._public_runs import (
    _restore_fences,
)
from modules.agents.access import actor, admit
from modules.agents.models import (
    AgentApproval,
    AgentEffect,
    AgentRun,
    AgentToolCall,
)
from modules.agents.schemas import (
    ApprovalRead,
)
from modules.tools.public import (
    revalidate_native_output_fences,
)


async def _conversation_approval_rows(
    session: AsyncSession, run_ids: list[UUID], owner_id: int, *, scope: Scope
) -> list[AgentApproval]:
    """Read up to 50 newest owner approvals with stored arguments for ``run_ids`` in the caller's workspace."""
    return list(
        (
            await session.scalars(
                select(AgentApproval)
                .where(
                    AgentApproval.run_id.in_(run_ids),
                    AgentApproval.workspace_id == scope.workspace_id,
                    AgentApproval.owner_id == owner_id,
                    AgentApproval.arguments.is_not(None),
                    AgentApproval.status.in_(
                        {"pending", "approved", "denied", "expired", "cancelled", "requires_review"}
                    ),
                )
                .order_by(AgentApproval.created_at.desc())
                .limit(50)
            )
        ).all()
    )


async def _approval_effects(
    session: AsyncSession, rows: list[AgentApproval], *, scope: Scope
) -> dict[UUID, tuple[str | None, str | None]]:
    """Map each approval's action id to its effect ``(result_reference, state)``; empty when no rows."""
    if not rows:
        return {}
    return {
        item[0]: (item[1], item[2])
        for item in (
            await session.execute(
                select(
                    AgentEffect.action_id,
                    AgentEffect.result_reference,
                    AgentEffect.state,
                ).where(
                    AgentEffect.action_id.in_([item.action_id for item in rows]),
                    AgentEffect.workspace_id == scope.workspace_id,
                )
            )
        ).all()
    }


async def list_conversation_approvals(
    session: AsyncSession,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    conversation_id: UUID,
    auth_session_hash: str,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[ApprovalRead]:
    """List bounded actions only while the owner session, Chat link and current output fences remain valid."""
    from core.auth.public import revalidate_account_session
    from modules.chat.public import list_agent_run_ids_for_owner

    await admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    owner_id = actor(scope)
    if not await revalidate_account_session(
        session, auth_session_hash, owner_id, multi_workspace_enabled=multi_workspace_enabled
    ):
        return []

    run_ids = await list_agent_run_ids_for_owner(
        session,
        conversation_id,
        owner_id,
        auth_session_hash,
        scope=scope,
    )
    if not run_ids:
        return []
    rows = await _conversation_approval_rows(session, run_ids, owner_id, scope=scope)
    effects = await _approval_effects(session, rows, scope=scope)
    reads: list[ApprovalRead] = []
    # The account session was revalidated above; one batched link check replaces a per-row query
    # (it also re-checks the Chat session and the link's retention window in Chat's own contract).
    from modules.chat.public import filter_live_agent_run_ids

    live_run_ids = await filter_live_agent_run_ids(
        session,
        sorted({item.run_id for item in rows}, key=str),
        owner_id,
        auth_session_hash,
        conversation_id=conversation_id,
    )
    revoked_by_run: dict[UUID, bool] = {}
    for item in rows:
        if item.run_id not in live_run_ids:
            continue
        # Several approvals usually share one run; its revocation state is checked once per page.
        if item.run_id not in revoked_by_run:
            revoked_by_run[item.run_id] = await _approval_run_is_revoked(
                session_factory,
                item.run_id,
                scope.workspace_id,
            )
        run_revoked = revoked_by_run[item.run_id]
        fences_current = (
            not run_revoked
            and item.auth_session_hash == auth_session_hash
            and await _approval_fences_current(
                session_factory,
                item,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        )
        if not fences_current and not run_revoked:
            await _invalidate_stale_approval(session, item)
            current_effect = await session.execute(
                select(
                    AgentEffect.result_reference,
                    AgentEffect.state,
                ).where(
                    AgentEffect.action_id == item.action_id,
                    AgentEffect.workspace_id == scope.workspace_id,
                )
            )
            effect_row = current_effect.one_or_none()
            if effect_row is not None:
                effects[item.action_id] = (effect_row[0], effect_row[1])
        reads.append(
            ApprovalRead(
                id=item.id,
                action_id=item.action_id,
                run_id=item.run_id,
                conversation_id=conversation_id,
                tool_name=item.tool_name,
                tool_version=item.tool_version,
                arguments=item.arguments if fences_current else None,
                argument_hash=item.argument_hash,
                destination_id=item.destination_id,
                destination_revision=item.destination_revision,
                status=item.status,
                effect_status=effects.get(item.action_id, (None, None))[1],
                result_reference=effects.get(item.action_id, (None, None))[0],
                created_at=item.created_at,
                expires_at=item.expires_at,
            )
        )
    return reads


async def get_approval(
    session: AsyncSession,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    approval_id: UUID,
    auth_session_hash: str,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> ApprovalRead:
    """Return exact bounded action detail only while its original Chat link and owner session are live."""
    from core.auth.public import revalidate_account_session
    from modules.chat.public import live_agent_conversation_id

    await admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    owner_id = actor(scope)
    if not await revalidate_account_session(
        session, auth_session_hash, owner_id, multi_workspace_enabled=multi_workspace_enabled
    ):
        raise NotFoundError("Approval not found")
    item = await session.scalar(
        select(AgentApproval).where(
            AgentApproval.id == approval_id,
            AgentApproval.workspace_id == scope.workspace_id,
            AgentApproval.owner_id == owner_id,
            AgentApproval.auth_session_hash == auth_session_hash,
            AgentApproval.arguments.is_not(None),
        )
    )
    conversation_id = (
        await live_agent_conversation_id(session, item.run_id, owner_id, auth_session_hash)
        if item is not None
        else None
    )
    if item is None or conversation_id is None or item.arguments is None:
        raise NotFoundError("Approval not found")
    run_revoked = await _approval_run_is_revoked(session_factory, item.run_id, scope.workspace_id)
    fences_current = not run_revoked and await _approval_fences_current(
        session_factory,
        item,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if not await revalidate_account_session(
        session, auth_session_hash, owner_id, multi_workspace_enabled=multi_workspace_enabled
    ) or not await live_agent_conversation_id(
        session,
        item.run_id,
        owner_id,
        auth_session_hash,
    ):
        raise NotFoundError("Approval not found")
    if not fences_current and not run_revoked:
        await _invalidate_stale_approval(session, item)
    effect = await session.scalar(
        select(AgentEffect).where(
            AgentEffect.action_id == item.action_id,
            AgentEffect.workspace_id == scope.workspace_id,
        )
    )
    return ApprovalRead(
        id=item.id,
        action_id=item.action_id,
        run_id=item.run_id,
        conversation_id=conversation_id,
        tool_name=item.tool_name,
        tool_version=item.tool_version,
        arguments=item.arguments if fences_current else None,
        argument_hash=item.argument_hash,
        destination_id=item.destination_id,
        destination_revision=item.destination_revision,
        status=item.status,
        effect_status=effect.state if effect is not None else None,
        result_reference=effect.result_reference if effect is not None else None,
        created_at=item.created_at,
        expires_at=item.expires_at,
    )


async def _approval_fences_current(
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    item: AgentApproval,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> bool:
    """Revalidate this action's persisted workspace-scoped evidence snapshot only."""
    principal = ToolExecutionPrincipal(
        actor_id=f"owner:{actor(scope)}",
        scope=scope,
        is_owner=True,
        allowed_tools=frozenset({item.tool_name}),
        owner_all_sources=True,
        destinations=frozenset(),
        capabilities=frozenset({"source.read"}),
    )
    try:
        sink = _restore_fences(item.source_fences)
    except (TypeError, ValueError, KeyError):
        return False
    return await revalidate_native_output_fences(
        session_factory,
        sink,
        principal,
        destination_kind="remote",
        multi_workspace_enabled=multi_workspace_enabled,
    )


async def _approval_run_is_revoked(
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    run_id: UUID,
    workspace_id: UUID,
) -> bool:
    """Hide action arguments for a revoked/missing run of this workspace without mutating its action ledger."""
    async with session_factory() as session:
        revoked = await session.scalar(
            select(AgentRun.evidence_revoked).where(
                AgentRun.id == run_id,
                AgentRun.workspace_id == workspace_id,
            )
        )
    return revoked is None or revoked


async def _invalidate_stale_approval(session: AsyncSession, item: AgentApproval) -> None:
    """Redact stale action evidence and durably deny an unconsumed pending slot under owner locks."""
    run = await session.scalar(
        select(AgentRun)
        .where(
            AgentRun.id == item.run_id,
            AgentRun.workspace_id == item.workspace_id,
        )
        .with_for_update()
    )
    if run is None or run.evidence_revoked:
        # The run-wide read/replay fence must not destructively scrub an independent approval.
        return
    row = await session.scalar(
        select(AgentApproval)
        .where(
            AgentApproval.id == item.id,
            AgentApproval.workspace_id == item.workspace_id,
        )
        .with_for_update()
    )
    if row is None:
        return
    effect = await session.scalar(
        select(AgentEffect)
        .where(
            AgentEffect.action_id == row.action_id,
            AgentEffect.workspace_id == item.workspace_id,
        )
        .with_for_update()
    )
    if row.status in {"pending", "approved"}:
        now = datetime.now(UTC)
        if effect is not None and effect.state in {"in_flight", "requires_review"}:
            effect.state, effect.payload = "requires_review", None
            row.status = "requires_review"
        else:
            if effect is not None and effect.state == "reserved":
                effect.state, effect.payload = "failed", None
            row.status = "cancelled"
            call = await session.scalar(
                select(AgentToolCall)
                .where(
                    AgentToolCall.run_id == run.id,
                    AgentToolCall.ordinal == row.ordinal,
                )
                .with_for_update()
            )
            if call is not None:
                call.status, call.error_code, call.completed_at = (
                    "denied",
                    "source_permissions_changed",
                    now,
                )
            if run.status == "waiting_approval" and not run.cancel_requested:
                run.status, run.dispatch_generation = "queued", run.dispatch_generation + 1
                run.updated_at = now
        row.resolved_at = now
    row.arguments = None
    row.source_fences = {}
    await session.commit()
