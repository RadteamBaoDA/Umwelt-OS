"""Agent write admission, browser-run budget reservation, browser authority revalidation and safe activity publication."""

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import (
    DomainError,
    error_status,
)
from core.workspaces.schemas import AccessFence, Scope
from modules.agents._public_types import (
    SPECIALIST_WORKFLOW_VERSION,
    BrowserRunAuthorization,
)
from modules.agents.access import actor, admit, run_epoch
from modules.agents.models import (
    AgentProfile,
    AgentRun,
    AgentToolCall,
)

logger = logging.getLogger(__name__)


def _browser_profile_current(run: AgentRun, profile: AgentProfile | None) -> bool:
    """Require the active specialist profile to still authorize its frozen browser tool contract."""
    if (
        profile is None
        or not profile.enabled
        or run.profile_snapshot is None
        or not run.profile_revision_hash
        or run.workflow_version != SPECIALIST_WORKFLOW_VERSION
    ):
        return False
    frozen = run.profile_snapshot
    if (
        frozen.get("id") != profile.profile_id
        or frozen.get("profile_revision_hash") != run.profile_revision_hash
    ):
        return False
    if frozen.get("source_ids") != profile.source_ids:
        return False
    frozen_contracts = run.tool_contracts.get("browser.read")
    for contract in profile.allowed_tools:
        if isinstance(contract, dict) and contract.get("name") == "browser.read":
            return (
                contract.get("version") == "1.0.0"
                and isinstance(frozen_contracts, dict)
                and contract.get("fingerprint") == frozen_contracts.get("fingerprint")
            )
    return False


async def lock_write_admission(
    session: AsyncSession,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> AccessFence:
    """Lock owner admission ahead of an in-UoW Agents write so the caller can commit with its fence.

    Members are denied (403) before any statement. Callers pass the returned fence to
    ``core.realtime.commit_with_replay`` once the in-UoW helper has flushed its rows.
    """
    return await admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
    )


def _epoch_current(run: AgentRun, fence: AccessFence) -> bool:
    """Require the live access fence to equal the run's captured original epoch (NULL fails closed)."""
    epoch = run_epoch(run)
    return epoch is not None and epoch[1] == fence


async def reserve_browser_run_budget_in_uow(
    session: AsyncSession,
    run_id: UUID,
    claim_generation: int,
    tool_slot: int,
    args_digest: str,
    requested_pages: int,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> BrowserRunAuthorization:
    """Reserve run-wide browser ceilings once for an exact durable tool slot.

    The run row serializes concurrent slots and survives worker handoff. An exact
    duplicate does not spend budget twice; changed arguments for a slot conflict.
    The caller owns the transaction and its fences; this helper admits ``scope`` without
    locking, binds the run to that workspace/actor and requires the live access fence to equal
    the run's captured original epoch (legacy NULL epochs are refused, never rebased).
    The returned object contains no ORM row or owner privilege.
    """
    from modules.chat import public as chat

    fence = await admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    owner_id = actor(scope)
    if (
        type(claim_generation) is not int
        or claim_generation < 1
        or type(tool_slot) is not int
        or not 1 <= tool_slot <= 10
        or type(requested_pages) is not int
        or not 1 <= requested_pages <= 3
        or len(args_digest) != 64
    ):
        raise PermissionError("Browser run authority is invalid")
    run = await session.scalar(
        select(AgentRun)
        .where(
            AgentRun.id == run_id,
            AgentRun.workspace_id == scope.workspace_id,
            AgentRun.owner_id == owner_id,
        )
        .with_for_update()
    )
    if (
        run is None
        or run.status != "running"
        or run.cancel_requested
        or run.evidence_revoked
        or run.claim_generation != claim_generation
        or run.auth_session_hash is None
    ):
        raise PermissionError("Browser run claim is no longer current")
    if not _epoch_current(run, fence):
        raise PermissionError("Browser run workspace authorization changed")
    profile_id = str((run.profile_snapshot or {}).get("id", ""))
    raw_sources = (run.profile_snapshot or {}).get("source_ids")
    try:
        source_ids = (
            frozenset(UUID(item) for item in raw_sources)
            if isinstance(raw_sources, list)
            else frozenset()
        )
    except (TypeError, ValueError) as exc:
        raise PermissionError("Browser profile source scope is invalid") from exc
    profile = await session.scalar(
        select(AgentProfile).where(
            AgentProfile.workspace_id == scope.workspace_id,
            AgentProfile.profile_id == profile_id,
        )
    )
    if not _browser_profile_current(run, profile):
        raise PermissionError("Current specialist profile no longer permits browser reads")
    tool_call = await session.scalar(
        select(AgentToolCall).where(
            AgentToolCall.run_id == run.id,
            AgentToolCall.ordinal == tool_slot,
        )
    )
    if (
        tool_call is None
        or tool_call.tool_name != "browser.read"
        or not isinstance(tool_call.arguments, dict)
        or hashlib.sha256(
            json.dumps(
                tool_call.arguments, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        ).hexdigest()
        != args_digest
    ):
        raise PermissionError("Browser tool slot does not match its persisted arguments")
    conversation_id = await chat.live_agent_conversation_id(
        session,
        run_id,
        owner_id,
        run.auth_session_hash,
    )
    if conversation_id is None:
        raise PermissionError("Original Chat session is no longer active")
    reservations = dict(run.browser_budget_reservations or {})
    key = str(tool_slot)
    previous = reservations.get(key)
    if previous is not None:
        if not isinstance(previous, dict) or previous.get("arguments_hash") != args_digest:
            raise ValueError("Browser slot arguments conflict with its prior reservation")
        if previous.get("requested_pages") != requested_pages:
            raise ValueError("Browser slot page budget conflicts with its prior reservation")
        budget_pages_before = max(0, run.browser_pages - requested_pages)
        budget_bytes_before = max(0, run.browser_bytes - 5 * 1024 * 1024)
    else:
        if run.browser_jobs >= 2 or run.browser_pages + requested_pages > 6:
            raise PermissionError("Browser run budget is exhausted")
        run.browser_jobs += 1
        run.browser_pages += requested_pages
        run.browser_bytes += 5 * 1024 * 1024
        if run.browser_bytes > 10 * 1024 * 1024:
            raise PermissionError("Browser run byte budget is exhausted")
        reservations[key] = {"arguments_hash": args_digest, "requested_pages": requested_pages}
        run.browser_budget_reservations = reservations
        budget_pages_before = run.browser_pages - requested_pages
        budget_bytes_before = run.browser_bytes - 5 * 1024 * 1024
        await session.flush()
    elapsed = max(
        0, int((datetime.now(UTC) - (run.claim_started_at or datetime.now(UTC))).total_seconds())
    )
    remaining_active = max(0, min(45, 300 - run.active_seconds - elapsed))
    if remaining_active <= 0:
        raise PermissionError("Browser run active-time budget is exhausted")
    return BrowserRunAuthorization(
        owner_id,
        run_id,
        tool_slot,
        args_digest,
        run.auth_session_hash,
        conversation_id,
        profile_id,
        run.profile_revision_hash or "",
        source_ids,
        claim_generation,
        max(0, 2 - run.browser_jobs),
        max(0, 6 - budget_pages_before),
        max(0, 10 * 1024 * 1024 - budget_bytes_before),
        remaining_active,
    )


async def revalidate_browser_run_authority(
    session: AsyncSession,
    authorization: BrowserRunAuthorization,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> bool:
    """Recheck the live run claim, session, profile, Chat link and original workspace epoch.

    Admission precedes every query; a run outside ``scope``'s workspace or actor, or whose
    captured epoch no longer equals the live access fence, is simply not current.
    """
    from modules.chat import public as chat

    try:
        fence = await admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    except (HTTPException, DomainError) as exc:
        if error_status(exc) in {401, 403, 404, 409}:
            return False
        raise
    if authorization.owner_id != actor(scope):
        return False
    run = await session.scalar(
        select(AgentRun)
        .where(
            AgentRun.id == authorization.run_id,
            AgentRun.workspace_id == scope.workspace_id,
            AgentRun.owner_id == authorization.owner_id,
        )
        .with_for_update()
    )
    if (
        run is None
        or not _epoch_current(run, fence)
        or run.status != "running"
        or run.cancel_requested
        or run.evidence_revoked
        or run.claim_generation != authorization.claim_generation
        or run.auth_session_hash != authorization.auth_session_hash
        or run.profile_revision_hash != authorization.profile_revision_hash
    ):
        return False
    profile = await session.scalar(
        select(AgentProfile)
        .where(
            AgentProfile.profile_id == authorization.profile_id,
            AgentProfile.workspace_id == scope.workspace_id,
        )
        .with_for_update()
    )
    if not _browser_profile_current(run, profile):
        return False
    current_conversation = await chat.live_agent_conversation_id(
        session,
        run.id,
        authorization.owner_id,
        authorization.auth_session_hash,
    )
    return current_conversation == authorization.conversation_id


async def publish_agent_activity_safely(
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    *,
    run_id: UUID,
    owner_id: int,
    auth_session_hash: str,
    status: str,
    tool_name: str | None = None,
) -> None:
    """Attempt bounded secondary chat delivery without changing durable run outcomes.

    PostgreSQL run status remains authoritative. The worker reconciler retries delivery from that
    row; the chat owner contract still performs its own session and expiry authorization. An
    optional bounded tool name selects a tool activity record while status-only calls remain
    deduplicated by the chat contract.
    """
    from modules.chat.public import publish_agent_activity

    try:
        async with asyncio.timeout(2):
            await publish_agent_activity(
                session_factory,
                run_id=run_id,
                owner_id=owner_id,
                auth_session_hash=auth_session_hash,
                status=status,
                tool_name=tool_name,
            )
    except Exception as exc:  # noqa: BLE001  # boundary: failure logged, caller degrades safely
        logger.warning("Agent activity delivery deferred for %s (%s)", run_id, type(exc).__name__)
