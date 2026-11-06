"""Ordered automation action execution: preparation, approval parking, in-database actions and external dispatch (agent runs, webhooks, briefs)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from arq.connections import ArqRedis
from fastapi import HTTPException
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.errors import DomainError, error_status
from core.worker_context import worker_session_factory
from core.workspaces.schemas import AccessFence, Scope
from modules.agents import public as agents
from modules.agents.public import ProfileRunStart
from modules.automations._execution_core import (
    _action_modules_enabled,
)
from modules.automations._execution_evidence import (
    _run_evidence_current,
)
from modules.automations._execution_runs import (
    _approval_hash,
    _commit,
    _destination_stale,
    _fence_current,
    _finish,
    _locked_run,
    _mark,
    _skip_pending,
)
from modules.automations._execution_types import (
    APPROVAL_ACTIONS,
    MAX_ACTION_ATTEMPTS,
)
from modules.automations.models import (
    AutomationRevision,
    AutomationRun,
    AutomationRunAction,
)
from modules.automations.scope import _actor, _admit
from modules.chat.public import Conversation
from modules.dashboard import public as dashboard
from modules.notifications.public import NotificationEmit, emit
from modules.settings import public as settings_public
from modules.tasks.public import TaskConflict, TaskCreate, create_task_in_uow
from modules.tools.public import send_webhook_once, webhook_profile_revision

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ExternalDispatch:
    """What to send after the in-flight marker is committed and the session is closed."""

    kind: str
    run: Any
    rev: Any
    spec: dict[str, Any]
    session_hash: str | None
    destination: str | None


async def _dispatch_run_agent(
    ctx: dict[str, Any],
    dispatch: _ExternalDispatch,
    ordinal: int,
    *,
    factory: async_sessionmaker[AsyncSession],
    scope: Scope,
    multi_workspace_enabled: bool,
) -> str:
    """Start the Agent run for an action; the start is idempotent so a replay is simply attempted again."""
    return await _start_agent(
        ctx,
        dispatch.run,
        ordinal,
        dispatch.spec,
        dispatch.session_hash,
        dispatch.rev.name,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )


async def _dispatch_call_webhook(
    ctx: dict[str, Any],
    dispatch: _ExternalDispatch,
    ordinal: int,
    *,
    factory: async_sessionmaker[AsyncSession],
    scope: Scope,
    multi_workspace_enabled: bool,
) -> str:
    """Send the approved webhook for an action to its pinned destination revision."""
    return await _send_webhook(
        ctx,
        dispatch.run,
        ordinal,
        dispatch.spec,
        factory,
        dispatch.destination,
        dispatch.session_hash,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )


# Post-commit external effects by action type; a new external action registers here instead of
# extending the dispatch code in ``_action_step``.
_EXTERNAL_DISPATCHERS: dict[str, Callable[..., Awaitable[str]]] = {
    "run_agent": _dispatch_run_agent,
    "call_webhook": _dispatch_call_webhook,
}


async def _settle_missing_evidence(
    session: AsyncSession,
    run: Any,
    row: Any,
    state: str,
    fence: AccessFence,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> str:
    """Close a run whose Document trigger evidence is no longer current.

    A dispatched (in-flight/review) action retains uncertainty as ``requires_review`` because the
    dispatch boundary may already have been crossed and must never be replayed; any other action
    is skipped and the run dropped. Pending siblings are skipped and the change is committed.
    """
    if state in {"in_flight", "requires_review"}:
        row.status, row.error_code = "requires_review", "document_evidence_unavailable"
        _finish(run, "requires_review", "document_evidence_unavailable")
        await _skip_pending(session, run.id, "document_evidence_unavailable")
        await _commit(session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        return "requires_review"
    row.status, row.error_code = "skipped", "document_evidence_unavailable"
    _finish(run, "dropped", "document_evidence_unavailable")
    await _skip_pending(session, run.id, "document_evidence_unavailable")
    await _commit(session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return "dropped"


async def _request_action_approval(
    session: AsyncSession,
    run: Any,
    row: Any,
    rev: Any,
    spec: dict[str, Any],
    ordinal: int,
    settings: Settings,
    fence: AccessFence,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> str:
    """Park an approval-gated action as ``awaiting_approval`` and notify the owner.

    A webhook binds its current deployment profile revision; a missing profile fails the action.
    Commits the awaiting row and its notification together under the access fence.
    """
    kind = spec["type"]
    try:
        destination = (
            webhook_profile_revision(settings, spec["alias"]) if kind == "call_webhook" else None
        )
    except ValueError:
        destination = None
    if kind == "call_webhook" and destination is None:
        row.status, row.error_code = "failed", "webhook_unavailable"
        await _commit(session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        return "failed"
    row.destination_revision = destination
    row.status, row.approval_hash = (
        "awaiting_approval",
        _approval_hash(run, ordinal, spec, destination),
    )
    row.approval_expires_at = datetime.now(UTC) + timedelta(hours=settings.approval_expiry_hours)
    await emit(
        session,
        NotificationEmit(
            dedupe_key=f"automation-approval:{run.id}:{ordinal}",
            kind="automation.approval",
            title=rev.name[:300],
            body="An automation action is waiting for your approval.",
        ),
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    await _commit(session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return "awaiting_approval"


async def _prepare_action(  # noqa: C901, PLR0912
    ctx: dict[str, Any],
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
    ordinal: int,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> str | _ExternalDispatch:
    """Run every in-session step of one action and return a final outcome or an external dispatch.

    Takes the access fence, then run and action row locks. Settled, review-only, parked, paused,
    stale and approval-gated rows return their outcome text; database actions run in this session.
    An external action is committed ``in_flight`` (the point of no return) and returned so the
    caller sends it after the session closes.
    """
    fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
    )
    run = await _locked_run(session, run_id, scope)
    row = await session.scalar(
        select(AutomationRunAction)
        .where(AutomationRunAction.run_id == run_id, AutomationRunAction.ordinal == ordinal)
        .with_for_update()
    )
    if run is None:
        raise RuntimeError("invariant violated: run must not be None")
    rev = await session.scalar(
        select(AutomationRevision).where(
            AutomationRevision.automation_id == run.automation_id,
            AutomationRevision.revision == run.revision,
        )
    )
    if rev is None:
        raise RuntimeError("invariant violated: rev must not be None")
    spec = rev.actions[ordinal - 1]
    if row is None:
        raise RuntimeError("invariant violated: row must not be None")
    state = row.status
    if state == "succeeded":
        return "succeeded"
    if state in ("failed", "denied", "skipped"):
        return "failed"
    if not await _run_evidence_current(
        session,
        run,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    ):
        return await _settle_missing_evidence(
            session,
            run,
            row,
            state,
            fence,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    # run_agent is idempotent (client_request_id), so an interrupted one is simply attempted again.
    if state == "requires_review" or (state == "in_flight" and spec["type"] != "run_agent"):
        row.status, row.error_code = "requires_review", row.error_code or "ambiguous_after_restart"
        await _commit(session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        return "requires_review"
    if state == "awaiting_approval":
        return "awaiting_approval"
    settings = cast(Settings, ctx["settings"])
    kind = spec["type"]
    stale = None
    if not await _fence_current(session, run, scope=scope, share=True):
        stale = "stale_revision"
    elif not await _action_modules_enabled(
        session, spec["type"], scope=scope, multi_workspace_enabled=multi_workspace_enabled
    ):
        # Preserve the approved durable action for dispatch after its owner is re-enabled.
        return "paused"
    elif state == "approved" and _destination_stale(settings, spec, row.destination_revision):
        stale = "stale_destination"
    if stale is not None:
        row.status, row.error_code = "skipped", stale
        await _commit(session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        return "dropped"
    if kind in APPROVAL_ACTIONS and state == "pending":
        return await _request_action_approval(
            session,
            run,
            row,
            rev,
            spec,
            ordinal,
            settings,
            fence,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    row.attempts += 1
    if kind in ("create_notification", "create_task"):
        return await _in_database_action(
            session,
            run,
            row,
            rev,
            spec,
            factory,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=fence,
        )
    if kind == "generate_brief":
        await _commit(
            session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )  # release locks; brief generation commits internally
        return await _generate_brief(
            ctx, run, row, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
    session_hash, destination = row.approved_session_hash, row.destination_revision
    if kind == "call_webhook":
        row.approved_session_hash = (
            None  # restore only if the final pre-send lifecycle fence pauses
        )
    # run_agent keeps the digest until success: its idempotent start may be re-attempted.
    row.status = "in_flight"  # point of no return: committed before the external call
    await _commit(session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return _ExternalDispatch(kind, run, rev, spec, session_hash, destination)


async def _action_step(
    ctx: dict[str, Any],
    run_id: UUID,
    ordinal: int,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> str:
    """Advance one action, durably pausing when its persisted owner module is disabled.

    Resumable: every row state is handled, so calling this after a crash converges. The fence is
    checked before every start, including exact Document trigger evidence. A revoked in-flight action
    remains review-only because the external effect may already have happened; it is never replayed.
    The external effect is sent after the preparing session closes, via ``_EXTERNAL_DISPATCHERS``.
    """
    factory = worker_session_factory(ctx)
    async with factory() as session:
        prepared = await _prepare_action(
            ctx,
            session,
            factory,
            run_id,
            ordinal,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    if isinstance(prepared, str):
        return prepared
    # Any type without its own dispatcher keeps the historical webhook path.
    dispatcher: Callable[..., Awaitable[str]] = _EXTERNAL_DISPATCHERS.get(
        prepared.kind, _dispatch_call_webhook
    )
    return await dispatcher(
        ctx,
        prepared,
        ordinal,
        factory=factory,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )


async def _restore_paused_action(
    factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
    ordinal: int,
    *,
    status: str,
    session_hash: str | None,
    decrement_attempt: bool = True,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Return an unsent effect to its durable resumable state when lifecycle blocks admission."""
    async with factory() as session:
        fence = await _admit(
            session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
        )
        run = await _locked_run(session, run_id, scope)
        row = await session.scalar(
            select(AutomationRunAction)
            .where(
                AutomationRunAction.run_id == run_id,
                AutomationRunAction.ordinal == ordinal,
            )
            .with_for_update()
        )
        if run is not None and (run.document_evidence_revoked or run.status == "requires_review"):
            if row is not None and row.status == "in_flight":
                row.status = "requires_review"
                row.error_code = (
                    "document_evidence_revoked"
                    if run.document_evidence_revoked
                    else (row.error_code or "run_requires_review")
                )
                await _commit(
                    session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled
                )
            return
        if row is not None and row.status in {"pending", "in_flight"}:
            row.status, row.error_code = status, None
            row.approved_session_hash = session_hash
            if decrement_attempt:
                row.attempts = max(row.attempts - 1, 0)
            await _commit(
                session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled
            )


async def _in_database_action(
    session: AsyncSession,
    run: AutomationRun,
    row: AutomationRunAction,
    rev: AutomationRevision,
    spec: Mapping[str, Any],
    factory: async_sessionmaker[AsyncSession],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> str:
    """Create a notification or task and flip the ledger row in one transaction (exactly-once)."""
    try:
        if spec["type"] == "create_notification":
            # The dedupe key makes even a repeated insert harmless.
            await emit(
                session,
                NotificationEmit(
                    dedupe_key=f"automation:{run.id}:{row.ordinal}",
                    kind="automation.rule",
                    title=rev.name[:300],
                    body=spec["message"],
                    link=spec.get("link"),
                ),
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
            reference = f"notification:{run.id}:{row.ordinal}"
        else:
            days = spec.get("due_in_days")
            task = await create_task_in_uow(
                session,
                TaskCreate(
                    title=spec["title"],
                    description=spec.get("description"),
                    due_date=(datetime.now(UTC) + timedelta(days=days)).date()
                    if days is not None
                    else None,
                ),
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
            reference = f"task:{task.id}"
        row.status, row.error_code, row.result_reference = "succeeded", None, reference
        await _commit(
            session, access_fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        return "succeeded"
    except (TaskConflict, ValidationError, ValueError):
        await session.rollback()
        await _mark(
            factory,
            run.id,
            row.ordinal,
            "failed",
            "rejected_by_owner_module",
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        return "failed"
    except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
        await session.rollback()
        return await _transient(
            factory,
            run.id,
            row.ordinal,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )


async def _transient(
    factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
    ordinal: int,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> str:
    """Retry a retry-safe action with bounded attempts; exhausted attempts fail the action.

    ``attempts`` was already incremented when the step started, so it counts the tries made.
    """
    async with factory() as session:
        fence = await _admit(
            session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
        )
        # Children are reached through the run, which must be inside the scope.
        if await _locked_run(session, run_id, scope) is None:
            return "failed"
        row = await session.scalar(
            select(AutomationRunAction)
            .where(AutomationRunAction.run_id == run_id, AutomationRunAction.ordinal == ordinal)
            .with_for_update()
        )
        if row is None:
            raise RuntimeError("invariant violated: row must not be None")
        if row.attempts >= MAX_ACTION_ATTEMPTS:
            if row is None:
                raise RuntimeError("invariant violated: row must not be None")
            row.status, row.error_code = "failed", "retries_exhausted"
            await _commit(
                session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled
            )
            return "failed"
        await _commit(session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        return "retry"


async def _generate_brief(
    ctx: dict[str, Any],
    run: AutomationRun,
    row: AutomationRunAction,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> str:
    """Generate through Dashboard only while the persisted trigger evidence remains eligible."""
    factory = worker_session_factory(ctx)
    try:
        async with factory() as session:
            # Model I/O follows, so admission is non-locking; the transaction is released after generation.
            fence = await _admit(
                session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
            )
            if not await _action_modules_enabled(
                session,
                row.action_type,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            ):
                await session.rollback()
                await _restore_paused_action(
                    factory,
                    run.id,
                    row.ordinal,
                    status="pending",
                    session_hash=None,
                    scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled,
                )
                return "paused"
            current_run = await session.scalar(
                select(AutomationRun).where(
                    AutomationRun.workspace_id == scope.workspace_id,
                    AutomationRun.owner_id == _actor(scope),
                    AutomationRun.id == run.id,
                )
            )
            if current_run is None or not await _run_evidence_current(
                session,
                current_run,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
                access_fence=fence,
            ):
                await session.rollback()
                await _mark(
                    factory,
                    run.id,
                    row.ordinal,
                    "skipped",
                    "document_evidence_unavailable",
                    scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled,
                )
                return "dropped"
            schedule = await dashboard.read_schedule(
                session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
            )
            day = datetime.now(UTC).astimezone(ZoneInfo(schedule.timezone)).date()
            run_id = run.id

            async def evidence_guard(guarded: AsyncSession) -> bool:
                """Re-take the trigger-evidence fence before the brief's own locks (egress and publish)."""
                fresh = await guarded.scalar(
                    select(AutomationRun)
                    .where(
                        AutomationRun.workspace_id == scope.workspace_id,
                        AutomationRun.owner_id == _actor(scope),
                        AutomationRun.id == run_id,
                    )
                    .execution_options(populate_existing=True)
                )
                return fresh is not None and await _run_evidence_current(
                    guarded,
                    fresh,
                    scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled,
                    access_fence=fence,
                )

            brief = await dashboard.generate_brief(
                session,
                day,
                schedule.timezone,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
                settings=cast(Settings, ctx["settings"]),
                redis=cast(Redis, ctx["redis"]),
                force=False,
                publish_guard=evidence_guard,
            )
        await _mark(
            factory,
            run.id,
            row.ordinal,
            "succeeded",
            None,
            f"brief:{getattr(brief, 'id', 'daily')}",
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        return "succeeded"
    except dashboard.BriefEmpty:
        await _mark(
            factory,
            run.id,
            row.ordinal,
            "succeeded",
            None,
            "brief:empty",
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        return "succeeded"
    except (
        dashboard.BriefEvidenceRevoked
    ):  # purge landed mid-call: drop like the step-start check, any attempt
        await _mark(
            factory,
            run.id,
            row.ordinal,
            "skipped",
            "document_evidence_unavailable",
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        return "dropped"
    except dashboard.BriefUnavailable:
        return await _transient(
            factory,
            run.id,
            row.ordinal,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
        return await _transient(
            factory,
            run.id,
            row.ordinal,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )


async def _automation_conversation(
    session: AsyncSession, automation_id: UUID, name: str, *, scope: Scope
) -> UUID:
    """Return the rule's dedicated Chat conversation, creating it once (same model the Chat route creates).

    P07 profile runs require a live Chat link; this per-rule thread is where the owner opens the
    run, its activity and any approval the agent itself raises.
    """
    existing = await session.scalar(
        select(Conversation.id)
        .where(
            Conversation.workspace_id == scope.workspace_id,
            Conversation.context_kind == "automation",
            Conversation.context_resource_id == automation_id,
            Conversation.archived.is_(False),
        )
        .limit(1)
    )
    if existing is not None:
        return existing
    conversation = Conversation(
        workspace_id=scope.workspace_id,
        actor_user_id=_actor(scope),
        title=f"Automation: {name}"[:255],
        context_kind="automation",
        context_resource_id=automation_id,
        metadata_json={},
    )
    session.add(conversation)
    await session.flush()
    return conversation.id


async def _start_agent(
    ctx: dict[str, Any],
    run: AutomationRun,
    ordinal: int,
    spec: Mapping[str, Any],
    session_hash: str | None,
    rule_name: str,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> str:
    """Start the approved P07 profile run through ``create_profile_run_in_uow`` (row is already in_flight).

    Persisted Document trigger evidence is rechecked before Agent creation. The run uses the rule's
    ``profile_id`` at its current revision in the rule's Automation Chat
    conversation, so P07 approvals inside it work. ``client_request_id`` derives from
    (run, ordinal) and the owner-session scope, so a retry after a crash returns the same agent run
    rather than a second one; that is why this action is safe to resume. A rejection raised before
    the run row is written is a clean failure.
    """
    factory = worker_session_factory(ctx)
    registry = ctx.get("agent_tool_registry")
    if session_hash is None or registry is None:
        await _mark(
            factory,
            run.id,
            ordinal,
            "failed",
            "agent_unavailable",
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        return "failed"
    try:
        async with factory() as session:
            # Config resolution may reach Redis/settings I/O, so admission is non-locking here.
            snapshot = await _admit(
                session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
            )
            config = await settings_public.get_ai_execution_config(
                session, cast(Settings, ctx["settings"]), cast(Redis, ctx["redis"]), scope=scope
            )
            # Lock order: admission fence first (same as other writers), then run, then Source locks.
            fence = await _admit(
                session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
            )
            if fence != snapshot:
                await session.rollback()
                return await _transient(
                    factory,
                    run.id,
                    ordinal,
                    scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled,
                )
            current_run = await _locked_run(session, run.id, scope)
            if current_run is None or not await _run_evidence_current(
                session,
                current_run,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
                access_fence=fence,
            ):
                await session.rollback()
                await _mark(
                    factory,
                    run.id,
                    ordinal,
                    "skipped",
                    "document_evidence_unavailable",
                    scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled,
                )
                return "dropped"
            if not await _action_modules_enabled(
                session, spec["type"], scope=scope, multi_workspace_enabled=multi_workspace_enabled
            ):
                await session.rollback()
                await _restore_paused_action(
                    factory,
                    run.id,
                    ordinal,
                    status="approved",
                    session_hash=session_hash,
                    scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled,
                )
                return "paused"
            profile_id = spec["profile_id"]
            revision = await agents.current_profile_revision(
                session,
                profile_id,
                registry,
                config,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
            conversation_id = await _automation_conversation(
                session, run.automation_id, rule_name, scope=scope
            )
            # The producer's title/condition metadata is never passed into the remote agent prompt.
            started = await agents.create_profile_run_in_uow(
                session,
                session_hash,
                profile_id,
                ProfileRunStart(
                    prompt=spec["instruction"],
                    expected_profile_revision=revision,
                    conversation_id=conversation_id,
                    client_request_id=str(uuid5(run.id, f"action:{ordinal}")),
                ),
                registry,
                config,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
            await _commit(
                session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled
            )
        try:  # PostgreSQL is the queue; a lost push is replayed by the agent reconciler.
            await cast(ArqRedis, ctx["redis"]).enqueue_job(
                "process_agent_run", str(started.id), 1, _job_id=f"agent-run:{started.id}:1"
            )
        except Exception:  # best-effort push: the agent reconciler replays a lost enqueue
            logger.debug("Agent run push failed; the reconciler replays it", exc_info=True)
    except asyncio.CancelledError:
        raise  # stays in_flight; the idempotent start is attempted again on resume
    except (HTTPException, DomainError) as exc:
        if (
            cast(int, error_status(exc)) >= 500
        ):  # replay-storage blip etc.: retry, never a terminal rejection
            return await _transient(
                factory,
                run.id,
                ordinal,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        await _mark(
            factory,
            run.id,
            ordinal,
            "failed",
            "agent_rejected",
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        return "failed"
    except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
        return await _transient(
            factory, run.id, ordinal, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
    await _mark(
        factory,
        run.id,
        ordinal,
        "succeeded",
        None,
        f"agent_run:{started.id}",
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    return "succeeded"


async def _send_webhook(
    ctx: dict[str, Any],
    run: AutomationRun,
    ordinal: int,
    spec: Mapping[str, Any],
    factory: async_sessionmaker[AsyncSession],
    destination: str | None,
    session_hash: str | None,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> str:
    """Send the approved webhook once through the shared SSRF-safe transport (row is already in_flight).

    The body is metadata only (rule, run, revision, depth), with no Document condition payload or
    title. Origin and depth go out as headers so a receiver that calls back into an inbound hook
    continues the same causal chain. Persisted evidence, revision and destination are checked after
    DNS and just before the socket write; a non-2xx or timeout after write begins is ``requires_review``.
    """
    settings = cast(Settings, ctx["settings"])
    lifecycle_blocked = False
    evidence_blocked = False
    publication_session: AsyncSession | None = None

    async def still_current() -> bool:
        """Hold evidence and rule fences until the transport's immediate socket-write boundary completes."""
        nonlocal lifecycle_blocked, evidence_blocked, publication_session
        if publication_session is not None:
            await publication_session.rollback()
            await publication_session.close()
            publication_session = None
        session = factory()
        try:
            # Held open through the socket write (network I/O), so admission is non-locking.
            fence = await _admit(
                session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
            )
            if not await _action_modules_enabled(
                session, spec["type"], scope=scope, multi_workspace_enabled=multi_workspace_enabled
            ):
                lifecycle_blocked = True
                await session.rollback()
                await session.close()
                return False
            persisted = await session.scalar(
                select(AutomationRun).where(
                    AutomationRun.workspace_id == scope.workspace_id,
                    AutomationRun.owner_id == _actor(scope),
                    AutomationRun.id == run.id,
                )
            )
            if persisted is None or not await _run_evidence_current(
                session,
                persisted,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
                access_fence=fence,
            ):
                evidence_blocked = True
                await session.rollback()
                await session.close()
                return False
            valid = await _fence_current(
                session, persisted, scope=scope, share=True
            ) and not _destination_stale(settings, spec, destination)
            if not valid:
                await session.rollback()
                await session.close()
                return False
            publication_session = session
            return True
        except Exception:
            await session.rollback()
            await session.close()
            raise

    body = {
        "event": spec["event"],
        "automation_id": str(run.automation_id),
        "run_id": str(run.id),
        "revision": run.revision,
        "trigger": run.trigger_type,
        "depth": run.depth,
    }
    try:
        outcome = await send_webhook_once(
            settings,
            spec["alias"],
            body,
            idempotency_key=str(uuid5(run.id, f"action:{ordinal}")),
            headers={
                "X-Umwelt-Automation-Id": str(run.automation_id),
                "X-Umwelt-Automation-Origin": str(run.id),
                "X-Umwelt-Automation-Depth": str(run.depth),
            },
            before_send=still_current,
        )
    except asyncio.CancelledError:
        await asyncio.shield(
            _mark(
                factory,
                run.id,
                ordinal,
                "requires_review",
                "cancelled_in_flight",
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        )
        raise
    finally:
        if publication_session is not None:
            await publication_session.rollback()
            await publication_session.close()
    if outcome == "succeeded":
        await _mark(
            factory,
            run.id,
            ordinal,
            "succeeded",
            None,
            f"webhook:{run.id}:{ordinal}",
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        return "succeeded"
    if outcome == "unsent":
        if evidence_blocked:
            await _mark(
                factory,
                run.id,
                ordinal,
                "skipped",
                "document_evidence_unavailable",
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
            return "dropped"
        async with factory() as session:
            await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
            current = await _fence_current(session, run, scope=scope)
            modules_enabled = await _action_modules_enabled(
                session, spec["type"], scope=scope, multi_workspace_enabled=multi_workspace_enabled
            )
        if lifecycle_blocked or not modules_enabled:
            await _restore_paused_action(
                factory,
                run.id,
                ordinal,
                status="approved",
                session_hash=session_hash,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
            return "paused"
        if not current or _destination_stale(settings, spec, destination):
            await _mark(
                factory,
                run.id,
                ordinal,
                "skipped",
                "stale_revision" if not current else "stale_destination",
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
            return "dropped"
        await _mark(
            factory,
            run.id,
            ordinal,
            "failed",
            "webhook_unsent",
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        return "failed"
    await _mark(
        factory,
        run.id,
        ordinal,
        "requires_review",
        "outcome_unknown",
        f"webhook:{run.id}:{ordinal}",
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    return "requires_review"
