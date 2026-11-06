"""Agent run lifecycle: conversation-action purge, run purge, cancellation and current profile revision."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import (
    NotFoundError,
)
from core.realtime import commit_with_replay
from core.tools import ToolRegistry
from core.workspaces.schemas import Scope
from modules.agents._public.budget import (
    publish_agent_activity_safely,
)
from modules.agents._public.runs import (
    _read,
    _restore_fences,
    _result_principal,
)
from modules.agents._public.types import (
    TERMINAL_STATUSES,
)
from modules.agents.access import actor, admit
from modules.agents.models import (
    AgentApproval,
    AgentEffect,
    AgentRun,
    AgentToolCall,
)
from modules.agents.schemas import (
    AgentRunRead,
)
from modules.tools.public import (
    purge_browser_results_in_uow,
    revalidate_native_output_fences,
)


async def purge_conversation_actions(
    session: AsyncSession,
    conversation_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> int:
    """Cancel linked runs and redact action payloads before Chat deletes their live activity link.

    Effect tombstones survive independently. An in-flight effect is retained as review-only because
    conversation deletion cannot prove whether the remote receiver accepted the request.
    """
    from modules.chat.public import list_agent_run_ids_for_delete

    purged = 0
    cursor: UUID | None = None
    while True:
        run_ids = await list_agent_run_ids_for_delete(
            session, conversation_id, actor(scope), cursor
        )
        if not run_ids:
            return purged
        purged += await purge_agent_runs(
            session,
            run_ids,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        cursor = run_ids[-1]


async def _redact_run_effects_and_approvals(session: AsyncSession, run: AgentRun) -> None:
    """Lock and redact one run's approvals and effects while retaining independent effect tombstones.

    Locks approvals (by id) then effects (by action id) for ``run`` -- the same order as the inline purge it was
    extracted from. In-flight effects become ``requires_review`` and reserved ones ``failed`` so an ambiguous send
    is never replayed; pending or approved approvals are cancelled (or kept for review when their effect is);
    payloads, arguments and source fences are cleared.
    """
    approvals = list(
        (
            await session.scalars(
                select(AgentApproval)
                .where(
                    AgentApproval.run_id == run.id,
                )
                .order_by(AgentApproval.id)
                .with_for_update()
            )
        ).all()
    )
    effects = list(
        (
            await session.scalars(
                select(AgentEffect)
                .where(
                    AgentEffect.run_id == run.id,
                )
                .order_by(AgentEffect.action_id)
                .with_for_update()
            )
        ).all()
    )
    effects_by_id = {effect.action_id: effect for effect in effects}
    for effect in effects:
        if effect.state == "in_flight":
            effect.state = "requires_review"
        elif effect.state == "reserved":
            effect.state = "failed"
        effect.payload = None
    for approval in approvals:
        approval_effect = effects_by_id.get(approval.action_id)
        if approval.status in {"pending", "approved"}:
            approval.status = (
                "requires_review"
                if approval_effect and approval_effect.state == "requires_review"
                else "cancelled"
            )
            approval.resolved_at = datetime.now(UTC)
        approval.arguments = None
        approval.source_fences = {}


async def purge_agent_runs(
    session: AsyncSession,
    run_ids: list[UUID],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> int:
    """Cancel and redact a bounded set of Chat-owned runs while retaining independent effect tombstones.

    The caller holds the admission fence and Chat locks; this admits ``scope`` without locking and
    selects only runs of its workspace and actor before the row locks.
    """
    await admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    runs = (
        list(
            (
                await session.scalars(
                    select(AgentRun)
                    .where(
                        AgentRun.id.in_(run_ids),
                        AgentRun.workspace_id == scope.workspace_id,
                        AgentRun.owner_id == actor(scope),
                    )
                    .order_by(AgentRun.id)
                    .with_for_update()
                )
            ).all()
        )
        if run_ids
        else []
    )
    for run in runs:
        await _redact_run_effects_and_approvals(session, run)
        await session.execute(
            update(AgentToolCall)
            .where(
                AgentToolCall.run_id == run.id,
            )
            .values(arguments={})
        )
        run.cancel_requested = True
        run.prompt = ""
        run.answer = None
        run.profile_snapshot = None
        run.profile_revision_hash = None
        run.source_fences = {}
        run.activities = []
        if run.status in {"queued", "waiting_approval"}:
            run.status = "cancelled"
            run.completed_at = datetime.now(UTC)
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            await session.execute(
                text(f"DELETE FROM {table} WHERE thread_id = :thread_id"),  # noqa: S608  # table name comes from a pinned literal tuple; thread id is bound
                {"thread_id": run.checkpoint_thread_id},
            )
    await purge_browser_results_in_uow(
        session,
        run_ids=[run.id for run in runs],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    return len(runs)


async def request_cancel(
    session: AsyncSession,
    run_id: UUID,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> AgentRunRead:
    """Commit PostgreSQL cancellation intent, terminalizing unclaimed work immediately.

    The access fence is locked first and validates the cancellation commit.

    Running executions observe the committed flag before every gateway retry, tool call and
    output publication. A completed remote read cannot be undone; its late result is fenced.
    Linked chat status is an optional bounded side effect and is retried from durable state.
    """
    fence = await admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
    )
    row = await session.scalar(
        select(AgentRun)
        .where(
            AgentRun.id == run_id,
            AgentRun.workspace_id == scope.workspace_id,
            AgentRun.owner_id == actor(scope),
        )
        .with_for_update()
    )
    if row is None:
        raise NotFoundError("Agent run not found")
    if row.status not in TERMINAL_STATUSES:
        row.cancel_requested = True
        approvals = list(
            (
                await session.scalars(
                    select(AgentApproval)
                    .where(
                        AgentApproval.run_id == run_id,
                        AgentApproval.status.in_({"pending", "approved"}),
                    )
                    .order_by(AgentApproval.id)
                    .with_for_update()
                )
            ).all()
        )
        effects = list(
            (
                await session.scalars(
                    select(AgentEffect)
                    .where(
                        AgentEffect.run_id == run_id,
                    )
                    .order_by(AgentEffect.action_id)
                    .with_for_update()
                )
            ).all()
        )
        effects_by_id = {item.action_id: item for item in effects}
        for approval in approvals:
            effect = effects_by_id.get(approval.action_id)
            if effect is not None and effect.state == "in_flight":
                approval.status = "requires_review"
            elif effect is not None and effect.state == "reserved" or approval.status == "pending":
                approval.status = "cancelled"
            approval.resolved_at = datetime.now(UTC)
        for effect in effects:
            if effect.state == "in_flight":
                effect.state = "requires_review"
            elif effect.state == "reserved":
                effect.state = "failed"
            effect.payload = None
        await purge_browser_results_in_uow(
            session,
            run_ids=[run_id],
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if row.status in {"queued", "waiting_approval"}:
            row.status = "cancelled"
            row.completed_at = datetime.now(UTC)
            row.error_code = None
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                await session.execute(
                    text(f"DELETE FROM {table} WHERE thread_id = :thread_id"),  # noqa: S608  # table name comes from a pinned literal tuple; thread id is bound
                    {"thread_id": row.checkpoint_thread_id},
                )
        await commit_with_replay(
            session,
            (),
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=fence,
        )
        await session.refresh(row)
        publication = (row.owner_id, row.auth_session_hash, row.status)
        await publish_agent_activity_safely(
            session_factory,
            run_id=run_id,
            owner_id=publication[0],
            auth_session_hash=publication[1],
            status=publication[2],
        )
    result = _read(row)
    if result.answer is not None and row.source_fences:
        # Reuse the same fresh source authorization before returning an answer from any route.
        principal = _result_principal(row)
        try:
            restored: dict[str, Any] | None = _restore_fences(row.source_fences)
        except (TypeError, ValueError, KeyError):
            restored = None
        current = bool(
            restored is not None
            and principal
            and await revalidate_native_output_fences(
                session_factory,
                restored,
                principal,
                destination_kind="remote",
                multi_workspace_enabled=multi_workspace_enabled,
            )
        )
        if not current:
            result = result.model_copy(update={"answer": None})
    return result


async def request_cancel_for_owner(
    session: AsyncSession,
    run_id: UUID,
    auth_session_hash: str,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> AgentRunRead:
    """Cancel only a run whose original session and live Chat parent authorize the action.

    Chat authorization and its conversation lock precede ``request_cancel``'s run and approval
    locks, matching the current Chat-delete order. Since ``request_cancel`` commits before returning,
    the live Chat link is reauthorized under the conversation lock afterward; a concurrent delete
    therefore suppresses the result. The legacy cancellation signature stays frozen.
    """
    await admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    owner_id = actor(scope)
    row = await session.scalar(
        select(AgentRun).where(
            AgentRun.id == run_id,
            AgentRun.workspace_id == scope.workspace_id,
            AgentRun.owner_id == owner_id,
        )
    )
    if row is None or row.auth_session_hash != auth_session_hash:
        raise NotFoundError("Agent run not found")
    if row.chat_link_required:
        from modules.chat.public import authorize_agent_run_access

        allowed = await authorize_agent_run_access(
            session,
            run_id,
            owner_id,
            auth_session_hash,
            require_original_session=True,
            lock_conversation=True,
        )
        if not allowed:
            raise NotFoundError("Agent run not found")
    result = await request_cancel(
        session,
        run_id,
        session_factory,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if row.chat_link_required:
        # Cancellation commits internally; reacquire the parent lock before returning linked data.
        allowed = await authorize_agent_run_access(
            session,
            run_id,
            owner_id,
            auth_session_hash,
            require_original_session=True,
            lock_conversation=True,
        )
        if not allowed:
            raise NotFoundError("Agent run not found")
    return result


async def current_profile_revision(
    session: AsyncSession,
    profile_id: str,
    registry: ToolRegistry,
    config: Any,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> int:
    """Return the selected profile's current revision (for a caller that pins ``expected_profile_revision``).

    Raises ``HTTPException`` 404 for an unknown profile, exactly like the profile read route.
    """
    from modules.agents.specialists import get_profile

    return (
        await get_profile(
            session,
            profile_id,
            registry,
            config,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    ).revision
