"""Owner-scoped agent run creation, lookup, and cancellation contracts."""

import asyncio
import base64
import binascii
import hashlib
import json
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import and_, exists, func, or_, select, text, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.pagination import decode_cursor, encode_cursor
from core.realtime import commit_with_replay
from core.telemetry import RunMeta as _RunMeta
from core.tools import ToolRegistry, ToolRisk
from core.tools.schemas import ToolExecutionPrincipal, ToolOutputFence
from core.workspaces.schemas import AccessFence, Scope
from modules.agents.access import actor, admit, run_epoch
from modules.agents.leases import try_agent_run_lease_in_uow
from modules.agents.models import (
    AgentApproval,
    AgentEffect,
    AgentEvidenceCleanup,
    AgentProfile,
    AgentProfileRevision,
    AgentRun,
    AgentToolCall,
)
from modules.agents.schemas import (
    AgentRunPage,
    AgentRunRead,
    AgentRunStart,
    ApprovalRead,
    ProfileRunStart,
)
from modules.agents.specialists import resolve_profile_snapshot
from modules.knowledge.documents.public import DocumentCleanupEvidenceScope
from modules.tools.public import purge_browser_results_in_uow, revalidate_native_output_fences

WORKFLOW_VERSION = "assistant-readonly-v1"
APPROVAL_WORKFLOW_VERSION = "assistant-approved-v1"
PROMPT_VERSION = "assistant-prompt-v1"
APPROVAL_PROMPT_VERSION = "assistant-approval-prompt-v1"
SPECIALIST_WORKFLOW_VERSION = "specialist-approved-v1"
SPECIALIST_PROMPT_VERSION = "specialist-prompt-v1"
CHECKPOINT_SCHEMA_VERSION = 1
SPECIALIST_CHECKPOINT_SCHEMA_VERSION = 2
AGENT_CLEANUP_LIMIT = 100
AGENT_INPUT_PROVENANCE_VERSION = 1
WORKFLOW_TOOLS = frozenset({
    "knowledge.get_document", "knowledge.list_documents", "search.query",
    "sources.list_sources", "sources.get_source",
})
APPROVAL_WORKFLOW_TOOLS = frozenset({*WORKFLOW_TOOLS, "webhook.send"})
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
logger = logging.getLogger(__name__)


async def unresolved_backup_effects(session: AsyncSession) -> dict[str, int]:
    """Project agent journal states whose remote action outcome remains unknown.

    The owner journal, rather than the generic worker activity receipt, is authoritative for
    whether a remote action may still have taken effect. Keep unresolved payloads private.
    """
    rows = (await session.execute(
        select(AgentEffect.state, func.count()).where(
            AgentEffect.state.in_({"in_flight", "requires_review"}),
        ).group_by(AgentEffect.state)
    )).all()
    return {str(state): int(count) for state, count in rows}


@dataclass(frozen=True)
class BrowserRunAuthorization:
    """Detached current run/profile/session claim and bounded browser budget identity."""

    owner_id: int
    run_id: UUID
    tool_slot: int
    arguments_hash: str
    auth_session_hash: str
    conversation_id: UUID
    profile_id: str
    profile_revision_hash: str
    source_ids: frozenset[UUID]
    claim_generation: int
    remaining_jobs: int
    remaining_pages: int
    remaining_bytes: int
    remaining_active_seconds: int


@dataclass(frozen=True)
class AgentCopiedEvidenceCleanupProgress:
    """Report one bounded Agent cleanup page and whether legacy identity remains unavailable."""

    next_cursor: str | None
    complete: bool
    rows_processed: int
    unavailable: bool
    unavailable_count: int = 0
    lease_pending: bool = False
    preflight_stale: bool = False


@dataclass(frozen=True)
class AgentCleanupLeasePreflight:
    """Detached preparation for a single Agent run on the caller's still-open UoW."""

    operation_id: UUID
    scope_fingerprint: str
    candidate_run_id: UUID | None
    agent_cursor: str | None
    marker_state: str
    lease_required: bool
    lease_acquired: bool
    blocked: bool


def _browser_profile_current(run: AgentRun, profile: AgentProfile | None) -> bool:
    """Require the active specialist profile to still authorize its frozen browser tool contract."""
    if (
        profile is None or not profile.enabled or run.profile_snapshot is None
        or not run.profile_revision_hash or run.workflow_version != SPECIALIST_WORKFLOW_VERSION
    ):
        return False
    frozen = run.profile_snapshot
    if frozen.get("id") != profile.profile_id or frozen.get("profile_revision_hash") != run.profile_revision_hash:
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
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool,
) -> AccessFence:
    """Lock owner admission ahead of an in-UoW Agents write so the caller can commit with its fence.

    Members are denied (403) before any statement. Callers pass the returned fence to
    ``core.realtime.commit_with_replay`` once the in-UoW helper has flushed its rows.
    """
    return await admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)


def _epoch_current(run: AgentRun, fence: AccessFence) -> bool:
    """Require the live access fence to equal the run's captured original epoch (NULL fails closed)."""
    epoch = run_epoch(run)
    return epoch is not None and epoch[1] == fence


async def reserve_browser_run_budget_in_uow(
    session: AsyncSession, run_id: UUID, claim_generation: int,
    tool_slot: int, args_digest: str, requested_pages: int,
    *, scope: Scope, multi_workspace_enabled: bool,
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
        type(claim_generation) is not int or claim_generation < 1
        or type(tool_slot) is not int or not 1 <= tool_slot <= 10
        or type(requested_pages) is not int or not 1 <= requested_pages <= 3
        or len(args_digest) != 64
    ):
        raise PermissionError("Browser run authority is invalid")
    run = await session.scalar(
        select(AgentRun).where(
            AgentRun.id == run_id, AgentRun.workspace_id == scope.workspace_id,
            AgentRun.owner_id == owner_id,
        ).with_for_update()
    )
    if (
        run is None or run.status != "running" or run.cancel_requested or run.evidence_revoked
        or run.claim_generation != claim_generation or run.auth_session_hash is None
    ):
        raise PermissionError("Browser run claim is no longer current")
    if not _epoch_current(run, fence):
        raise PermissionError("Browser run workspace authorization changed")
    profile_id = str((run.profile_snapshot or {}).get("id", ""))
    raw_sources = (run.profile_snapshot or {}).get("source_ids")
    try:
        source_ids = frozenset(UUID(item) for item in raw_sources) if isinstance(raw_sources, list) else frozenset()
    except (TypeError, ValueError):
        raise PermissionError("Browser profile source scope is invalid")
    profile = await session.scalar(select(AgentProfile).where(
        AgentProfile.workspace_id == scope.workspace_id, AgentProfile.profile_id == profile_id,
    ))
    if not _browser_profile_current(run, profile):
        raise PermissionError("Current specialist profile no longer permits browser reads")
    tool_call = await session.scalar(select(AgentToolCall).where(
        AgentToolCall.run_id == run.id, AgentToolCall.ordinal == tool_slot,
    ))
    if (
        tool_call is None or tool_call.tool_name != "browser.read"
        or not isinstance(tool_call.arguments, dict)
        or hashlib.sha256(json.dumps(
            tool_call.arguments, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")).hexdigest() != args_digest
    ):
        raise PermissionError("Browser tool slot does not match its persisted arguments")
    conversation_id = await chat.live_agent_conversation_id(
        session, run_id, owner_id, run.auth_session_hash,
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
    elapsed = max(0, int((datetime.now(UTC) - (run.claim_started_at or datetime.now(UTC))).total_seconds()))
    remaining_active = max(0, min(45, 300 - run.active_seconds - elapsed))
    if remaining_active <= 0:
        raise PermissionError("Browser run active-time budget is exhausted")
    return BrowserRunAuthorization(
        owner_id, run_id, tool_slot, args_digest, run.auth_session_hash,
        conversation_id, profile_id, run.profile_revision_hash or "", source_ids, claim_generation,
        max(0, 2 - run.browser_jobs), max(0, 6 - budget_pages_before),
        max(0, 10 * 1024 * 1024 - budget_bytes_before), remaining_active,
    )


async def revalidate_browser_run_authority(
    session: AsyncSession, authorization: BrowserRunAuthorization,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Recheck the live run claim, session, profile, Chat link and original workspace epoch.

    Admission precedes every query; a run outside ``scope``'s workspace or actor, or whose
    captured epoch no longer equals the live access fence, is simply not current.
    """
    from modules.chat import public as chat

    try:
        fence = await admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    except HTTPException as exc:
        if exc.status_code in {401, 403, 404, 409}:
            return False
        raise
    if authorization.owner_id != actor(scope):
        return False
    run = await session.scalar(select(AgentRun).where(
        AgentRun.id == authorization.run_id,
        AgentRun.workspace_id == scope.workspace_id,
        AgentRun.owner_id == authorization.owner_id,
    ).with_for_update())
    if (
        run is None or not _epoch_current(run, fence)
        or run.status != "running" or run.cancel_requested or run.evidence_revoked
        or run.claim_generation != authorization.claim_generation
        or run.auth_session_hash != authorization.auth_session_hash
        or run.profile_revision_hash != authorization.profile_revision_hash
    ):
        return False
    profile = await session.scalar(select(AgentProfile).where(
        AgentProfile.profile_id == authorization.profile_id,
        AgentProfile.workspace_id == scope.workspace_id,
    ).with_for_update())
    if not _browser_profile_current(run, profile):
        return False
    current_conversation = await chat.live_agent_conversation_id(
        session, run.id, authorization.owner_id, authorization.auth_session_hash,
    )
    return current_conversation == authorization.conversation_id


async def publish_agent_activity_safely(
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    *, run_id: UUID, owner_id: int, auth_session_hash: str, status: str,
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
                session_factory, run_id=run_id, owner_id=owner_id,
                auth_session_hash=auth_session_hash, status=status, tool_name=tool_name,
            )
    except Exception as exc:  # noqa: BLE001  # boundary: failure logged, caller degrades safely
        logger.warning("Agent activity delivery deferred for %s (%s)", run_id, type(exc).__name__)


def _read(row: AgentRun) -> AgentRunRead:
    """Project private persisted fields into the bounded owner response."""
    return AgentRunRead(
        id=row.id, agent_id=row.agent_id, status=row.status,
        answer=None if row.evidence_revoked else row.answer,
        error_code=row.error_code, steps=row.steps, tool_calls=row.tool_calls,
        active_seconds=row.active_seconds, token_usage=row.token_usage,
        token_budget=row.token_budget,
        token_budget_available=False,
        token_usage_unknown=row.token_usage_unknown, activities=row.activities,
        profile_id=(str(row.profile_snapshot.get("id")) if row.profile_snapshot else None),
        profile_revision_hash=row.profile_revision_hash,
        created_at=row.created_at, updated_at=row.updated_at, completed_at=row.completed_at,
    )


def _result_principal(row: AgentRun) -> ToolExecutionPrincipal | None:
    """Rebuild output authorization from a profile's original exact source grant, failing closed on malformed snapshots.

    The principal's scope is the run's captured original epoch (Recipe J); a legacy run with
    NULL epochs yields None, so its answer stays suppressed rather than being rebased.
    """
    epoch = run_epoch(row)
    if epoch is None:
        return None
    source_ids: frozenset[UUID]
    if row.profile_snapshot is None:
        source_ids, owner_all_sources = frozenset(), True
    else:
        raw_ids = row.profile_snapshot.get("source_ids")
        if not isinstance(raw_ids, list) or len(raw_ids) > 32:
            return None
        try:
            source_ids = frozenset(UUID(item) for item in raw_ids)
        except (ValueError, TypeError, AttributeError):
            return None
        owner_all_sources = False
    capabilities = {"source.read"}
    if "webhook.send" in row.allowed_tools:
        capabilities.add("webhook.send")
    return ToolExecutionPrincipal(
        actor_id=f"owner:{row.owner_id}", scope=epoch[0], is_owner=True,
        allowed_tools=frozenset(row.allowed_tools), source_ids=source_ids,
        owner_all_sources=owner_all_sources, destinations=frozenset(),
        capabilities=frozenset(capabilities),
    )


async def create_profile_run_in_uow(
    session: AsyncSession, auth_session_hash: str, profile_id: str,
    request: ProfileRunStart, registry: ToolRegistry, config: Any,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> AgentRunRead:
    """Persist a replay-safe linked profile run without committing the caller's run/link transaction.

    The idempotency key is scoped to the workspace, owner and session digest. A byte-identical
    retry returns its original run; reusing the key for different prompt, profile revision, or
    conversation is a 409. Token budgets remain explicitly unavailable and are rejected before
    worker/model egress. The caller must take ``lock_write_admission`` before any other lock; the
    lock taken here is a re-entrant re-check. The fence's membership and configuration revisions
    are stored on the run as the original epoch Recipe J later compares, and the caller commits with
    ``commit_with_replay`` using the fence returned by ``lock_write_admission``.
    """
    fence = await admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    owner_id = actor(scope)
    if request.token_budget is not None:
        raise HTTPException(status_code=422, detail="token_budget_unavailable")
    request_body = {
        "profile_id": profile_id, "prompt": request.prompt,
        "expected_profile_revision": request.expected_profile_revision,
        "conversation_id": str(request.conversation_id), "token_budget": request.token_budget,
    }
    request_hash = hashlib.sha256(json.dumps(
        request_body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    lock_material = f"{scope.workspace_id}:{owner_id}:{auth_session_hash}:{request.client_request_id}".encode()
    lock_key = int.from_bytes(hashlib.sha256(lock_material).digest()[:8], "big", signed=True)
    # Serialize the absent-row case as well as ordinary reads so parallel retries cannot create duplicate work.
    await session.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key})
    existing = await session.scalar(select(AgentRun).where(
        AgentRun.workspace_id == scope.workspace_id,
        AgentRun.owner_id == owner_id,
        AgentRun.auth_session_hash == auth_session_hash,
        AgentRun.client_request_id == request.client_request_id,
    ))
    if existing is not None:
        if existing.request_hash != request_hash:
            raise HTTPException(status_code=409, detail="Client request ID was already used for another run")
        from modules.chat.public import has_live_agent_run_link

        if not await has_live_agent_run_link(session, existing.id, owner_id, auth_session_hash):
            raise HTTPException(status_code=404, detail="Agent run not found")
        retry_factory = async_sessionmaker(session.bind) if session.bind is not None else None
        return await _read_current_result(
            existing, retry_factory, multi_workspace_enabled=multi_workspace_enabled,
        )
    snapshot, snapshot_hash = await resolve_profile_snapshot(
        session, profile_id, request.expected_profile_revision, registry, config,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    revision = snapshot["revision"]
    if revision:
        revision_row = await session.scalar(select(AgentProfileRevision).where(
            AgentProfileRevision.workspace_id == scope.workspace_id,
            AgentProfileRevision.profile_id == profile_id,
            AgentProfileRevision.revision == revision,
        ))
        if revision_row is None:
            raise HTTPException(status_code=409, detail="Agent profile revision is unavailable")
        if revision_row.snapshot_hash != snapshot_hash:
            raise HTTPException(status_code=409, detail="Agent profile revision no longer matches its stored snapshot")
    allowed = [item["name"] for item in snapshot["allowed_tools"]]
    contracts = {item["name"]: {"version": item["version"], "fingerprint": item["fingerprint"]}
                 for item in snapshot["allowed_tools"]}
    profile_id_value = snapshot["id"]
    run = AgentRun(
        id=uuid4(), workspace_id=scope.workspace_id, owner_id=owner_id,
        membership_revision=fence.membership_revision,
        configuration_revision=fence.configuration_revision,
        auth_session_hash=auth_session_hash,
        agent_id=profile_id_value,
        workflow_version=SPECIALIST_WORKFLOW_VERSION,
        prompt_version=SPECIALIST_PROMPT_VERSION,
        checkpoint_schema_version=SPECIALIST_CHECKPOINT_SCHEMA_VERSION,
        checkpoint_thread_id=str(uuid4()), prompt=request.prompt,
        profile_snapshot=snapshot, profile_revision_hash=snapshot_hash,
        client_request_id=request.client_request_id, request_hash=request_hash,
        allowed_tools=allowed, tool_contracts=contracts,
        chat_link_required=True, status="queued", dispatch_generation=1,
        activities=[{"kind": "status", "status": "queued", "created_at": datetime.now(UTC).isoformat()}],
    )
    session.add(run)
    from modules.chat.public import link_agent_run

    await link_agent_run(session, request.conversation_id, run.id, owner_id, auth_session_hash)
    await session.flush()
    return _read(run)


async def list_runs(
    session: AsyncSession, *, profile_id: str | None = None,
    conversation_id: UUID | None = None, auth_session_hash: str | None = None,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    limit: int = 25, cursor: str | None = None,
    scope: Scope, multi_workspace_enabled: bool,
) -> AgentRunPage:
    """Page bounded owner run history after Chat-link and current-output authorization.

    Candidate scans stop after a bounded number of rows. Linked runs are filtered by Chat's
    session/expiry projection, and answer-bearing rows pass the same current evidence fence as
    direct run reads. The opaque cursor advances over examined candidates so expired ephemeral
    links cannot hide later live runs or leak their retained answers.
    """
    await admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    owner_id = actor(scope)
    if not 1 <= limit <= 25:
        raise HTTPException(status_code=422, detail="Run history page size is outside its supported bound")
    anchor = decode_cursor(cursor) if cursor else None
    page: list[AgentRun] = []
    scanned = 0
    has_more = False
    scan_anchor = anchor
    while scanned < 100 and len(page) <= limit:
        statement = select(AgentRun).where(
            AgentRun.workspace_id == scope.workspace_id, AgentRun.owner_id == owner_id,
        )
        if profile_id is not None:
            statement = statement.where(AgentRun.agent_id == profile_id)
        if scan_anchor is not None:
            statement = statement.where(tuple_(AgentRun.created_at, AgentRun.id) < scan_anchor)
        batch = list((await session.scalars(statement.order_by(
            AgentRun.created_at.desc(), AgentRun.id.desc(),
        ).limit(min(25, 100 - scanned)))).all())
        if not batch:
            break
        from modules.chat.public import filter_live_agent_run_ids

        linked_ids = [row.id for row in batch if row.chat_link_required]
        live_ids = await filter_live_agent_run_ids(
            session, linked_ids, owner_id, auth_session_hash or "",
            conversation_id=conversation_id,
        ) if linked_ids and auth_session_hash else frozenset()
        for row in batch:
            scanned += 1
            scan_anchor = (row.created_at, row.id)
            if row.chat_link_required and row.id not in live_ids:
                continue
            if conversation_id is not None and not row.chat_link_required:
                continue
            page.append(row)
            if len(page) > limit:
                has_more = True
                break
        if has_more or len(batch) < min(25, 100 - (scanned - len(batch))):
            break
    next_cursor: str | None
    if has_more:
        # Continue after the last returned row so the first overflow row remains on the next page.
        next_cursor = encode_cursor(page[limit - 1].created_at, page[limit - 1].id)
        page = page[:limit]
    elif scanned >= 100:
        next_cursor = encode_cursor(*scan_anchor) if scan_anchor else None
    else:
        next_cursor = None
    return AgentRunPage(
        items=[
            await _read_current_result(row, session_factory, multi_workspace_enabled=multi_workspace_enabled)
            for row in page
        ],
        next_cursor=next_cursor,
    )


async def create_run(
    session: AsyncSession,
    auth_session_hash: str,
    request: AgentRunStart,
    registry: ToolRegistry,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> AgentRunRead:
    """Persist one exact-version read-only workflow and its durable dispatch generation.

    The access fence is locked first; its revisions are stored as the run's original epoch and
    the final commit is fenced by it.

    Caller must pass the authenticated session digest and canonical app registry. Only the
    fixed workflow allowlist intersected with currently registered READ_ONLY, non-confirmed
    contracts is persisted; request fields cannot select tools or identity. A supplied conversation
    creates a chat-owned activity link and persists its lifecycle requirement in the same transaction,
    so cascade deletion cannot make an originally linked run look unlinked. Configured token budgets are persisted
    but currently unavailable without a safe prompt-token preflight, so the worker rejects them before
    remote egress. This operation
    commits the returned row before responding, leaving PostgreSQL as queue authority while the
    worker reconciler retries Redis dispatch.
    """
    fence = await admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    owner_id = actor(scope)
    definitions = {
        item.name: item for item in registry.list_tools(allowed_tools=APPROVAL_WORKFLOW_TOOLS)
        if not (registry.hides_tool and registry.hides_tool(item.name, scope.workspace_id))
        and (item.risk == ToolRisk.READ_ONLY and not item.confirmation_required
            or item.name == "webhook.send" and item.risk == ToolRisk.EXTERNAL_WRITE
            and item.confirmation_required)
    }
    allowed = sorted(definitions)
    if not allowed:
        raise HTTPException(status_code=503, detail="Agent tools are unavailable")
    run = AgentRun(
        id=uuid4(), workspace_id=scope.workspace_id, owner_id=owner_id,
        membership_revision=fence.membership_revision,
        configuration_revision=fence.configuration_revision,
        auth_session_hash=auth_session_hash,
        agent_id="assistant", workflow_version=APPROVAL_WORKFLOW_VERSION,
        prompt_version=APPROVAL_PROMPT_VERSION, checkpoint_schema_version=CHECKPOINT_SCHEMA_VERSION,
        checkpoint_thread_id=str(uuid4()), prompt=request.prompt,
        allowed_tools=allowed,
        tool_contracts={name: {"version": item.version, "fingerprint": item.schema_fingerprint}
                        for name, item in definitions.items()},
        chat_link_required=request.conversation_id is not None,
        status="queued", dispatch_generation=1, token_budget=request.token_budget,
        activities=[{"kind": "status", "status": "queued", "created_at": datetime.now(UTC).isoformat()}],
    )
    session.add(run)
    if request.conversation_id is not None:
        from modules.chat.public import link_agent_run

        await link_agent_run(session, request.conversation_id, run.id, owner_id, auth_session_hash)
    await commit_with_replay(
        session, (), scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence,
    )
    await session.refresh(run)
    return _read(run)


async def get_run(
    session: AsyncSession,
    run_id: UUID,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    *, scope: Scope, multi_workspace_enabled: bool,
) -> AgentRunRead:
    """Return owner-visible run state and suppress answers with stale source evidence."""
    await admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    row = await session.scalar(select(AgentRun).where(
        AgentRun.id == run_id, AgentRun.workspace_id == scope.workspace_id,
        AgentRun.owner_id == actor(scope),
    ))
    if row is None:
        raise HTTPException(status_code=404, detail="Agent run not found")
    return await _read_current_result(row, session_factory, multi_workspace_enabled=multi_workspace_enabled)


async def get_run_for_owner(
    session: AsyncSession,
    run_id: UUID,
    auth_session_hash: str,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    *, scope: Scope, multi_workspace_enabled: bool,
) -> AgentRunRead:
    """Read a run only after Chat confirms its linked conversation is retained for this owner.

    The frozen ``get_run`` API remains the result projector. Linked runs use Chat's lifecycle
    contract; persistent links permit the current owner, while ephemeral links remain session-bound.
    Unlinked assistant runs are still readable by the authenticated owner.
    """
    await admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    owner_id = actor(scope)
    row = await session.scalar(select(AgentRun).where(
        AgentRun.id == run_id, AgentRun.workspace_id == scope.workspace_id,
        AgentRun.owner_id == owner_id,
    ))
    if row is None:
        raise HTTPException(status_code=404, detail="Agent run not found")
    if row.chat_link_required:
        from modules.chat.public import authorize_agent_run_access

        allowed = await authorize_agent_run_access(
            session, run_id, owner_id, auth_session_hash, lock_conversation=True,
        )
        if not allowed:
            raise HTTPException(status_code=404, detail="Agent run not found")
    return await get_run(
        session, run_id, session_factory, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )


async def _read_current_result(
    row: AgentRun,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] | None,
    *, multi_workspace_enabled: bool,
) -> AgentRunRead:
    """Project one durable result only while its captured native evidence fences remain current.

    Malformed fences and unavailable owner checks suppress the answer while preserving status,
    counters, and safe activity metadata. The caller owns any enclosing transaction; fence reads
    use short independent sessions and never hold domain locks across delivery.
    """
    result = _read(row)
    if row.source_fences and result.answer is not None and session_factory is not None:
        principal = _result_principal(row)
        try:
            sink: dict[str, Any] | None = _restore_fences(row.source_fences)
        except (TypeError, ValueError, KeyError):
            sink = None
        current = bool(sink is not None and principal and await revalidate_native_output_fences(
            session_factory, sink, principal, destination_kind="remote",
            multi_workspace_enabled=multi_workspace_enabled,
        ))
        if not current:
            return result.model_copy(update={"answer": None})
    elif row.source_fences and result.answer is not None:
        return result.model_copy(update={"answer": None})
    return result


def _restore_fences(value: dict[str, Any]) -> dict[str, Any]:
    """Rebuild strict UUID-keyed source evidence identities from JSONB before authorization checks."""
    generations = value.get("source_generations")
    records = value.get("records")
    if not isinstance(generations, dict) or len(generations) > 100 or not isinstance(records, list) or len(records) > 100:
        raise ValueError("Invalid source evidence fence")
    restored: dict[UUID, int] = {}
    for source_id, generation in generations.items():
        if not isinstance(source_id, str) or type(generation) is not int or generation < 1:
            raise ValueError("Invalid source generation")
        restored[UUID(source_id)] = generation
    restored_records = []
    for record in records:
        if not isinstance(record, dict) or set(record) != {
            "document_id", "document_version_id", "source_id", "source_generation", "chunk_id",
        }:
            raise ValueError("Invalid result fence")
        restored_records.append(ToolOutputFence(
            document_id=UUID(record["document_id"]),
            document_version_id=UUID(record["document_version_id"]),
            source_id=UUID(record["source_id"]),
            source_generation=record["source_generation"],
            chunk_id=UUID(record["chunk_id"]) if record["chunk_id"] is not None else None,
        ))
    return {
        "source_generations": restored,
        "records": restored_records,
    }


def _agent_cleanup_records(scope: DocumentCleanupEvidenceScope) -> list[dict[str, object]]:
    """Validate a detached Documents page and project exact identities for JSONB containment queries."""
    if (
        not isinstance(scope.operation_id, UUID) or not isinstance(scope.source_id, UUID)
        or not isinstance(scope.document_id, UUID) or len(scope.references) > 100
    ):
        raise ValueError("Invalid Agent cleanup evidence scope")
    records: list[dict[str, object]] = []
    seen: set[tuple[str, UUID, UUID | None]] = set()
    for reference in scope.references:
        kind, version_id, chunk_id = (
            reference.reference_kind, reference.document_version_id, reference.chunk_id,
        )
        if (
            kind not in {"version", "chunk"} or not isinstance(version_id, UUID)
            or (kind == "version" and chunk_id is not None)
            or (kind == "chunk" and not isinstance(chunk_id, UUID))
        ):
            raise ValueError("Invalid Agent cleanup evidence identity")
        identity = (kind, version_id, chunk_id)
        if identity in seen:
            raise ValueError("Duplicate Agent cleanup evidence identity")
        seen.add(identity)
        records.append({
            "document_id": str(scope.document_id),
            "document_version_id": str(version_id),
            "source_id": str(scope.source_id),
            "chunk_id": str(chunk_id) if chunk_id is not None else None,
        })
    return records


def _agent_cleanup_fingerprint(scope: DocumentCleanupEvidenceScope, records: list[dict[str, object]]) -> str:
    """Bind a cleanup cursor to the immutable operation, document, source, and reference page."""
    payload = {
        "operation": str(scope.operation_id), "source": str(scope.source_id),
        "document": str(scope.document_id), "records": records,
    }
    return hashlib.sha256(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


def _agent_cleanup_scope_fingerprint(scope: DocumentCleanupEvidenceScope) -> str:
    """Bind the durable Agent receipt to operation/source/document, independent of reference pages."""
    payload = {
        "operation": str(scope.operation_id), "source": str(scope.source_id),
        "document": str(scope.document_id),
    }
    return hashlib.sha256(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


def _encode_agent_cleanup_cursor(
    scope: DocumentCleanupEvidenceScope, fingerprint: str, after: UUID | None, unavailable: bool,
) -> str:
    """Encode a canonical operation-and-page-bound Agent keyset continuation."""
    payload = {
        "v": 1, "operation": str(scope.operation_id), "fingerprint": fingerprint,
        "after": str(after) if after else None, "unavailable": unavailable,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_agent_cleanup_cursor(
    cursor: str, scope: DocumentCleanupEvidenceScope, fingerprint: str,
) -> tuple[UUID | None, bool]:
    """Reject malformed, noncanonical, or cross-operation/reference-page cleanup cursors."""
    try:
        if not cursor or len(cursor) > 1024 or "=" in cursor:
            raise ValueError("Invalid Agent copied-evidence cursor")
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        payload = json.loads(raw)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"v", "operation", "fingerprint", "after", "unavailable"}
            or payload["v"] != 1 or payload["operation"] != str(scope.operation_id)
            or payload["fingerprint"] != fingerprint or type(payload["unavailable"]) is not bool
            or (payload["after"] is not None and not isinstance(payload["after"], str))
        ):
            raise ValueError("Agent copied-evidence cursor belongs to another scope")
        after = UUID(payload["after"]) if payload["after"] is not None else None
        if _encode_agent_cleanup_cursor(scope, fingerprint, after, payload["unavailable"]) != cursor:
            raise ValueError("Agent copied-evidence cursor is not canonical")
        return after, payload["unavailable"]
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError) as exc:
        raise ValueError("Invalid Agent copied-evidence cursor") from exc


def _fences_match_agent_scope(value: object, records: list[dict[str, object]]) -> bool:
    """Match only strict document/version/chunk records from a captured native Agent fence."""
    if not isinstance(value, dict):
        return False
    try:
        restored = _restore_fences(value)
    except (TypeError, ValueError, KeyError):
        return False
    for fence in restored["records"]:
        for record in records:
            if (
                str(fence.document_id) == record["document_id"]
                and str(fence.document_version_id) == record["document_version_id"]
                and str(fence.source_id) == record["source_id"]
                and (str(fence.chunk_id) if fence.chunk_id is not None else None) == record["chunk_id"]
            ):
                return True
    return False


def _agent_scope_contains(value: object, record: dict[str, object]) -> bool:
    """Check one exact immutable identity in a strict bounded native fence."""
    return _fences_match_agent_scope(value, [record])


def _strict_agent_fences(value: object) -> dict[str, object] | None:
    """Return a decoded classified fence, including a valid empty fence, or None for legacy/invalid."""
    if not isinstance(value, dict):
        return None
    try:
        return _restore_fences(value)
    except (TypeError, ValueError, KeyError):
        return None


async def _agent_cleanup_candidate_ids(
    session: AsyncSession,
    scope: DocumentCleanupEvidenceScope,
    records: list[dict[str, object]],
    after: UUID | None,
    limit: int,
    *,
    workspace_id: UUID,
    owner_id: int,
) -> list[UUID]:
    """Read a bounded UUID keyset page using exact JSONB containment and durable operation receipts."""
    run_clauses = []
    for record in records:
        identity = {key: record[key] for key in (
            "document_id", "document_version_id", "source_id", "chunk_id",
        )}
        run_clauses.append(AgentRun.source_fences.contains({"records": [identity]}))
    related_call = exists(select(AgentToolCall.id).where(
        AgentToolCall.run_id == AgentRun.id,
        or_(*[AgentToolCall.input_source_fences.contains({"records": [
            {key: record[key] for key in ("document_id", "document_version_id", "source_id", "chunk_id")}
        ]}) for record in records]),
    )) if records else False
    related_approval = exists(select(AgentApproval.id).where(
        AgentApproval.run_id == AgentRun.id,
        AgentApproval.workspace_id == workspace_id,
        or_(*[AgentApproval.source_fences.contains({"records": [
            {key: record[key] for key in ("document_id", "document_version_id", "source_id", "chunk_id")}
        ]}) for record in records]),
    )) if records else False
    durable_call_count = select(func.count(AgentToolCall.id)).where(
        AgentToolCall.run_id == AgentRun.id,
    ).correlate(AgentRun).scalar_subquery()
    legacy_active = and_(
        AgentRun.status.in_({"queued", "running", "waiting_approval"}),
        exists(select(AgentToolCall.id).where(
            AgentToolCall.run_id == AgentRun.id,
            AgentToolCall.input_provenance_version.is_(None),
        )),
    )
    # Supported old read-only checkpoints can reserve a durable tool counter without an
    # AgentToolCall row. Keep the active slot unavailable until exact owner evidence is found.
    legacy_unreconciled_active = and_(
        AgentRun.status.in_({"queued", "running", "waiting_approval"}),
        AgentRun.tool_calls > durable_call_count,
    )
    marker_exists = exists(select(AgentEvidenceCleanup.id).where(
        AgentEvidenceCleanup.run_id == AgentRun.id,
        AgentEvidenceCleanup.workspace_id == workspace_id,
        AgentEvidenceCleanup.operation_id == scope.operation_id,
    ))
    cleanup_clauses: list[Any] = [
        *run_clauses, related_call, related_approval, legacy_active,
        legacy_unreconciled_active, marker_exists,
    ]
    query = select(AgentRun.id).where(
        AgentRun.workspace_id == workspace_id, AgentRun.owner_id == owner_id, or_(*cleanup_clauses),
    )
    if after is not None:
        query = query.where(AgentRun.id > after)
    return list((await session.scalars(query.order_by(AgentRun.id).limit(limit + 1))).all())


async def _admit_cleanup_evidence(
    session: AsyncSession, evidence: DocumentCleanupEvidenceScope, *, scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Admit the owner (members denied before SQL) and bind the detached page to this workspace."""
    await admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if evidence.workspace_id != scope.workspace_id or evidence.actor_user_id != actor(scope):
        raise HTTPException(status_code=409, detail="Document cleanup evidence belongs to another workspace")


async def preflight_document_copied_evidence_lease(
    session: AsyncSession,
    evidence: DocumentCleanupEvidenceScope,
    *,
    cursor: str | None = None,
    limit: int = AGENT_CLEANUP_LIMIT,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> AgentCleanupLeasePreflight:
    """Acquire a pending run's nonblocking saver lease before Documents/Memory locks.

    This reads only detached evidence identities, candidate IDs and an operation receipt. The
    transaction advisory lease, when required, stays on this session for the caller's commit.
    """
    if type(limit) is not int or not 1 <= limit <= AGENT_CLEANUP_LIMIT:
        raise ValueError(f"Agent cleanup page size must be between 1 and {AGENT_CLEANUP_LIMIT}")
    await _admit_cleanup_evidence(
        session, evidence, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    workspace_id, owner_id = scope.workspace_id, actor(scope)
    records = _agent_cleanup_records(evidence)
    fingerprint = _agent_cleanup_fingerprint(evidence, records)
    after, _ = _decode_agent_cleanup_cursor(cursor, evidence, fingerprint) if cursor else (None, False)
    candidate_ids = await _agent_cleanup_candidate_ids(
        session, evidence, records, after, 1, workspace_id=workspace_id, owner_id=owner_id,
    )
    run_id = candidate_ids[0] if candidate_ids else None
    marker = None
    if run_id is not None:
        marker = await session.scalar(select(AgentEvidenceCleanup).where(
            AgentEvidenceCleanup.operation_id == evidence.operation_id,
            AgentEvidenceCleanup.workspace_id == workspace_id,
            AgentEvidenceCleanup.run_id == run_id,
        ))
    marker_state = "none" if run_id is None else (
        "finalized" if marker is not None and marker.finalized_at is not None
        else marker.state if marker is not None else "absent"
    )
    lease_required = marker_state == "pending"
    lease_acquired = (
        await try_agent_run_lease_in_uow(session, run_id)
        if lease_required and run_id is not None else False
    )
    return AgentCleanupLeasePreflight(
        evidence.operation_id, fingerprint, run_id, cursor,
        marker_state, lease_required, lease_acquired, lease_required and not lease_acquired,
    )


async def _agent_cleanup_ledgers(
    session: AsyncSession, run_id: UUID, *, workspace_id: UUID,
) -> tuple[list[AgentApproval], list[AgentEffect], list[AgentToolCall]]:
    """Lock the Agent action ledger in run→approval→effect→call order, inside one workspace."""
    approvals = list((await session.scalars(select(AgentApproval).where(
        AgentApproval.run_id == run_id, AgentApproval.workspace_id == workspace_id,
    ).order_by(AgentApproval.id).with_for_update())).all())
    effects = list((await session.scalars(select(AgentEffect).where(
        AgentEffect.run_id == run_id, AgentEffect.workspace_id == workspace_id,
    ).order_by(AgentEffect.action_id).with_for_update())).all())
    calls = list((await session.scalars(select(AgentToolCall).where(
        AgentToolCall.run_id == run_id,
        # AgentToolCall carries no workspace column; bind it through its workspace-scoped run.
        AgentToolCall.run_id.in_(select(AgentRun.id).where(
            AgentRun.id == run_id, AgentRun.workspace_id == workspace_id,
        )),
    ).order_by(AgentToolCall.ordinal).with_for_update())).all())
    return approvals, effects, calls


def _revoke_agent_approval(approval: AgentApproval, effect: AgentEffect | None, now: datetime) -> None:
    """Fence a proven dependent action while retaining its durable external-effect outcome."""
    if effect is not None:
        if effect.state == "in_flight":
            effect.state = "requires_review"
        elif effect.state == "reserved":
            effect.state = "failed"
        effect.payload = None
    if approval.status in {"pending", "approved"}:
        approval.status = "requires_review" if effect is not None and effect.state == "requires_review" else "cancelled"
    approval.arguments = None
    approval.resolved_at = approval.resolved_at or now


def _scrub_agent_scope_payloads(
    approvals: list[AgentApproval], effects: list[AgentEffect], calls: list[AgentToolCall],
    records: list[dict[str, object]], now: datetime,
) -> bool:
    """Clear exact action payloads and report retained calls lacking usable per-slot lineage.

    A strict approval snapshot joined to the same run/ordinal can classify a legacy call. Otherwise
    a nonempty legacy call payload remains untouched and prevents a clean completion claim.
    """
    effects_by_action = {effect.action_id: effect for effect in effects}
    approvals_by_ordinal = {approval.ordinal: approval for approval in approvals}
    unavailable = False
    for call in calls:
        matched = False
        classified = False
        if call.input_provenance_version == AGENT_INPUT_PROVENANCE_VERSION:
            fences = _strict_agent_fences(call.input_source_fences)
            classified = fences is not None
            matched = classified and _fences_match_agent_scope(call.input_source_fences, records)
        elif call.input_provenance_version is None:
            approval = approvals_by_ordinal.get(call.ordinal)
            approval_fences = _strict_agent_fences(approval.source_fences) if approval is not None else None
            if approval_fences is not None:
                classified = True
                assert approval is not None
                matched = _fences_match_agent_scope(approval.source_fences, records)
        if matched:
            # Input provenance justifies clearing derived arguments. Output references may identify
            # independent native evidence (such as BrowserPageEvidence), so retain those links.
            call.arguments = {}
            if call.status == "started":
                call.status, call.error_code, call.completed_at = "denied", "evidence_revoked", now
        elif not classified and (bool(call.arguments) or bool(call.evidence_refs)):
            unavailable = True
    for approval in approvals:
        if _fences_match_agent_scope(approval.source_fences, records):
            _revoke_agent_approval(approval, effects_by_action.get(approval.action_id), now)
    return unavailable


async def _agent_unavailable_run_count(
    session: AsyncSession, operation_id: UUID, workspace_id: UUID,
) -> int:
    """Count distinct per-operation/run unavailable receipts through their unique lookup index."""
    return int(await session.scalar(select(func.count(AgentEvidenceCleanup.id)).where(
        AgentEvidenceCleanup.operation_id == operation_id,
        AgentEvidenceCleanup.workspace_id == workspace_id,
        AgentEvidenceCleanup.state == "unavailable",
    )) or 0)


def _find_agent_scope_match(
    run: AgentRun,
    approvals: list[AgentApproval],
    calls: list[AgentToolCall],
    records: list[dict[str, object]],
) -> dict[str, object] | None:
    """Find the first strict exact run, classified call-input, or approval fence identity."""
    for record in records:
        if _agent_scope_contains(run.source_fences, record):
            return record
    for call in calls:
        if call.input_provenance_version == AGENT_INPUT_PROVENANCE_VERSION:
            for record in records:
                if _agent_scope_contains(call.input_source_fences, record):
                    return record
    for approval in approvals:
        for record in records:
            if _agent_scope_contains(approval.source_fences, record):
                return record
    return None


def _agent_cleanup_marker_state(marker: AgentEvidenceCleanup | None) -> str:
    """Return effective state, with a durable finalization timestamp taking precedence."""
    if marker is None:
        return "absent"
    return "finalized" if marker.finalized_at is not None else marker.state


async def purge_document_copied_evidence_page(
    session: AsyncSession,
    evidence: DocumentCleanupEvidenceScope,
    *,
    cursor: str | None = None,
    limit: int = AGENT_CLEANUP_LIMIT,
    scope: Scope,
    multi_workspace_enabled: bool,
    preflight: AgentCleanupLeasePreflight,
) -> AgentCopiedEvidenceCleanupProgress:
    """Revoke and selectively scrub one bounded Agent page for a detached Documents cleanup scope.

    The caller owns commit. First encounter durably marks the run revoked and returns a cursor before
    it; a later delivery must acquire the worker's nonblocking run lease before privacy/run/ledger
    locks and deleting opaque saver state. Lock order is lease, Memory privacy, AgentRun, approvals,
    effects, then calls. Only strict source/document/version/chunk fence matches scrub action payloads.
    """
    if type(limit) is not int or not 1 <= limit <= AGENT_CLEANUP_LIMIT:
        raise ValueError(f"Agent cleanup page size must be between 1 and {AGENT_CLEANUP_LIMIT}")
    await _admit_cleanup_evidence(
        session, evidence, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    workspace_id, owner_id = scope.workspace_id, actor(scope)
    records = _agent_cleanup_records(evidence)
    page_fingerprint = _agent_cleanup_fingerprint(evidence, records)
    scope_fingerprint = _agent_cleanup_scope_fingerprint(evidence)
    after, unavailable = (
        _decode_agent_cleanup_cursor(cursor, evidence, page_fingerprint) if cursor else (None, False)
    )
    async def progress(
        next_cursor: str | None, complete: bool, rows_processed: int, unavailable_result: bool,
        lease_pending: bool = False,
        preflight_stale: bool = False,
    ) -> AgentCopiedEvidenceCleanupProgress:
        """Count operation-wide unique unavailable receipts; leave flush and commit to the caller's UoW."""
        return AgentCopiedEvidenceCleanupProgress(
            next_cursor, complete, rows_processed, unavailable_result,
            await _agent_unavailable_run_count(session, evidence.operation_id, workspace_id), lease_pending,
            preflight_stale,
        )

    # One run per transaction keeps the caller's detached lease preflight aligned with the
    # exact candidate whose Agent rows this hook may lock. Later runs use this opaque cursor.
    if preflight.operation_id != evidence.operation_id or preflight.agent_cursor != cursor:
        raise ValueError("Agent cleanup lease preflight does not match the Documents cursor")
    if preflight.scope_fingerprint != page_fingerprint:
        raise ValueError("Agent cleanup lease preflight does not match the Documents evidence scope")
    if preflight.blocked:
        return await progress(cursor, False, 0, unavailable)
    candidate_ids = await _agent_cleanup_candidate_ids(
        session, evidence, records, after, 1, workspace_id=workspace_id, owner_id=owner_id,
    )
    more_candidates = len(candidate_ids) > 1
    page = candidate_ids[:1]
    if (page[0] if page else None) != preflight.candidate_run_id:
        return await progress(cursor, False, 0, unavailable, preflight_stale=True)
    last_processed = after
    processed = 0
    from modules.memory.public import lock_export_privacy

    for run_id in page:
        # Marker read is intentionally unlocked. Its durable state is checked again after lease
        # acquisition and before locking mutable Agent rows.
        marker = await session.scalar(select(AgentEvidenceCleanup).where(
            AgentEvidenceCleanup.operation_id == evidence.operation_id,
            AgentEvidenceCleanup.workspace_id == workspace_id,
            AgentEvidenceCleanup.run_id == run_id,
        ))
        current_marker_state = _agent_cleanup_marker_state(marker)
        # A pending marker always needs an affirmative lease for this exact run before any
        # privacy/receipt locks. If the detached read is stale, the caller must roll back.
        if (current_marker_state != preflight.marker_state
                or (current_marker_state == "pending" and (
                    not preflight.lease_required or not preflight.lease_acquired
                ))):
            return await progress(cursor, False, 0, unavailable, preflight_stale=True)
        if marker is not None and (
            marker.source_id != evidence.source_id or marker.document_id != evidence.document_id
            or marker.scope_fingerprint != scope_fingerprint
        ):
            return await progress(
                _encode_agent_cleanup_cursor(evidence, page_fingerprint, last_processed, True),
                False, processed, True,
            )
        if marker is not None and (marker.state == "finalized" or marker.finalized_at is not None):
            # Documents may deliver later reference pages for the same operation/run. The opaque
            # checkpoint is already gone, but each page still needs its own selective ledger scrub.
            await lock_export_privacy(session)
            run = await session.scalar(select(AgentRun).where(
                AgentRun.id == run_id,
                AgentRun.workspace_id == workspace_id, AgentRun.owner_id == owner_id,
            ).with_for_update())
            marker = await session.scalar(select(AgentEvidenceCleanup).where(
                AgentEvidenceCleanup.operation_id == evidence.operation_id,
                AgentEvidenceCleanup.workspace_id == workspace_id,
                AgentEvidenceCleanup.run_id == run_id,
            ).with_for_update().execution_options(populate_existing=True))
            if _agent_cleanup_marker_state(marker) != preflight.marker_state:
                return await progress(cursor, False, processed, unavailable, preflight_stale=True)
            if run is not None:
                approvals, effects, calls = await _agent_cleanup_ledgers(session, run_id, workspace_id=workspace_id)
                unresolved = _scrub_agent_scope_payloads(
                    approvals, effects, calls, records, datetime.now(UTC),
                )
                assert marker is not None
                marker.state = "unavailable" if unresolved else "finalized"
                unavailable = unavailable or unresolved or marker.state == "unavailable"
            last_processed, processed = run_id, processed + 1
            continue
        if marker is not None and marker.state == "unavailable":
            # An unassociated legacy active row is a coverage gate, not permission to cancel or
            # erase an unrelated run. Recheck exact identities so a later reference page can prove
            # a real match and promote this receipt into the ordinary revoke/finalize sequence.
            await lock_export_privacy(session)
            run = await session.scalar(select(AgentRun).where(
                AgentRun.id == run_id,
                AgentRun.workspace_id == workspace_id, AgentRun.owner_id == owner_id,
            ).with_for_update())
            marker = await session.scalar(select(AgentEvidenceCleanup).where(
                AgentEvidenceCleanup.operation_id == evidence.operation_id,
                AgentEvidenceCleanup.workspace_id == workspace_id,
                AgentEvidenceCleanup.run_id == run_id,
            ).with_for_update().execution_options(populate_existing=True))
            if _agent_cleanup_marker_state(marker) != preflight.marker_state:
                return await progress(cursor, False, processed, unavailable, preflight_stale=True)
            if run is None:
                last_processed, processed = run_id, processed + 1
                unavailable = True
                continue
            approvals, effects, calls = await _agent_cleanup_ledgers(session, run_id, workspace_id=workspace_id)
            matching = _find_agent_scope_match(run, approvals, calls, records)
            if matching is None:
                last_processed, processed = run_id, processed + 1
                unavailable = True
                continue
            assert marker is not None
            marker.matched_identity = matching
            marker.state = "pending"
            run.evidence_revoked = True
            run.cancel_requested = True
            run.dispatch_generation += 1
            run.answer = None
            if run.status in {"queued", "waiting_approval"}:
                run.status, run.completed_at = "cancelled", datetime.now(UTC)
            run.updated_at = datetime.now(UTC)
            await session.flush()
            return await progress(
                _encode_agent_cleanup_cursor(evidence, page_fingerprint, last_processed, True),
                False, processed + 1, True, lease_pending=True,
            )

        if marker is None:
            # Phase A: commit an operation-scoped denial before attempting physical saver erasure.
            # No lease is awaited or held while the transaction takes owner rows.
            await lock_export_privacy(session)
            run = await session.scalar(select(AgentRun).where(
                AgentRun.id == run_id,
                AgentRun.workspace_id == workspace_id, AgentRun.owner_id == owner_id,
            ).with_for_update())
            if run is None:
                last_processed, processed = run_id, processed + 1
                continue
            marker = await session.scalar(select(AgentEvidenceCleanup).where(
                AgentEvidenceCleanup.operation_id == evidence.operation_id,
                AgentEvidenceCleanup.workspace_id == workspace_id,
                AgentEvidenceCleanup.run_id == run_id,
            ).with_for_update())
            if _agent_cleanup_marker_state(marker) != preflight.marker_state:
                return await progress(cursor, False, processed, unavailable, preflight_stale=True)
            if marker is not None and marker.scope_fingerprint != scope_fingerprint:
                return await progress(
                        _encode_agent_cleanup_cursor(evidence, page_fingerprint, last_processed, True),
                        False, processed, True,
                )
            if marker is not None and marker.state == "pending":
                return await progress(
                        _encode_agent_cleanup_cursor(evidence, page_fingerprint, last_processed, unavailable),
                        False, processed, unavailable,
                )
            if marker is not None:
                approvals, effects, calls = await _agent_cleanup_ledgers(session, run_id, workspace_id=workspace_id)
                if marker.state == "unavailable" and marker.finalized_at is None:
                    matching = _find_agent_scope_match(run, approvals, calls, records)
                    if matching is None:
                        last_processed, processed = run_id, processed + 1
                        unavailable = True
                        continue
                    marker.matched_identity, marker.state = matching, "pending"
                    run.evidence_revoked = True
                    run.cancel_requested = True
                    run.dispatch_generation += 1
                    run.answer = None
                    if run.status in {"queued", "waiting_approval"}:
                        run.status, run.completed_at = "cancelled", datetime.now(UTC)
                    run.updated_at = datetime.now(UTC)
                    await session.flush()
                    return await progress(
                        _encode_agent_cleanup_cursor(evidence, page_fingerprint, last_processed, True),
                        False, processed + 1, True,
                    )
                unresolved = _scrub_agent_scope_payloads(
                    approvals, effects, calls, records, datetime.now(UTC),
                )
                if unresolved:
                    marker.state = "unavailable"
                unavailable = unavailable or unresolved or marker.state == "unavailable"
                last_processed, processed = run_id, processed + 1
                continue
            approvals, effects, calls = await _agent_cleanup_ledgers(session, run_id, workspace_id=workspace_id)
            matching = _find_agent_scope_match(run, approvals, calls, records)
            if matching is None:
                # An unclassified active legacy row is only a coverage gate. Persist it once so
                # keyset replay is finite, but never revoke, erase, or otherwise mutate that run.
                marker = AgentEvidenceCleanup(
                    workspace_id=workspace_id, operation_id=evidence.operation_id, run_id=run_id,
                    source_id=evidence.source_id, document_id=evidence.document_id,
                    scope_fingerprint=scope_fingerprint,
                    matched_identity=None, state="unavailable",
                )
                session.add(marker)
                await session.flush()
                last_processed, processed = run_id, processed + 1
                unavailable = True
                continue
            marker = AgentEvidenceCleanup(
                workspace_id=workspace_id, operation_id=evidence.operation_id, run_id=run_id,
                source_id=evidence.source_id, document_id=evidence.document_id,
                scope_fingerprint=scope_fingerprint, matched_identity=matching, state="pending",
            )
            session.add(marker)
            run.evidence_revoked = True
            run.cancel_requested = True
            run.dispatch_generation += 1
            run.answer = None
            if run.status in {"queued", "waiting_approval"}:
                run.status = "cancelled"
                run.completed_at = datetime.now(UTC)
            run.updated_at = datetime.now(UTC)
            await session.flush()
            return await progress(
                _encode_agent_cleanup_cursor(evidence, page_fingerprint, last_processed, unavailable),
                False, processed + 1, unavailable, lease_pending=True,
            )

        # Phase B: this try-lock conflicts with the worker's session lease and is acquired before
        # Memory privacy or Agent rows. Contention leaves this run at the current cursor position.
        if preflight.blocked or (preflight.lease_required and not preflight.lease_acquired):
            return await progress(
                _encode_agent_cleanup_cursor(evidence, page_fingerprint, last_processed, unavailable),
                False, processed, unavailable,
            )
        await lock_export_privacy(session)
        run = await session.scalar(select(AgentRun).where(
            AgentRun.id == run_id,
            AgentRun.workspace_id == workspace_id, AgentRun.owner_id == owner_id,
        ).with_for_update())
        marker = await session.scalar(select(AgentEvidenceCleanup).where(
            AgentEvidenceCleanup.operation_id == evidence.operation_id,
            AgentEvidenceCleanup.workspace_id == workspace_id,
            AgentEvidenceCleanup.run_id == run_id,
        ).with_for_update())
        if (_agent_cleanup_marker_state(marker) != preflight.marker_state
                or (_agent_cleanup_marker_state(marker) == "pending" and (
                    not preflight.lease_required or not preflight.lease_acquired
                ))):
            return await progress(cursor, False, processed, unavailable, preflight_stale=True)
        if run is None or marker is None or marker.scope_fingerprint != scope_fingerprint:
            return await progress(
                _encode_agent_cleanup_cursor(evidence, page_fingerprint, last_processed, True),
                False, processed, True,
            )
        approvals, effects, calls = await _agent_cleanup_ledgers(session, run_id, workspace_id=workspace_id)
        now = datetime.now(UTC)
        unresolved = _scrub_agent_scope_payloads(approvals, effects, calls, records, now)
        if marker.state == "finalized" or marker.finalized_at is not None:
            marker.state = "unavailable" if unresolved else "finalized"
            unavailable = unavailable or unresolved or marker.state == "unavailable"
            last_processed, processed = run_id, processed + 1
            continue
        if marker.state != "pending":
            return await progress(
                _encode_agent_cleanup_cursor(evidence, page_fingerprint, last_processed, True),
                False, processed, True,
            )
        run.answer = None
        run.evidence_revoked = True
        run.cancel_requested = True
        if run.status in {"queued", "waiting_approval"}:
            run.status = "cancelled"
            run.completed_at = run.completed_at or now
        run.updated_at = now
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            await session.execute(
                text(f"DELETE FROM {table} WHERE thread_id = :thread_id"),
                {"thread_id": run.checkpoint_thread_id},
            )
        marker.state = "unavailable" if unresolved else "finalized"
        marker.finalized_at = now
        unavailable = unavailable or unresolved
        last_processed, processed = run_id, processed + 1

    next_cursor = (
        _encode_agent_cleanup_cursor(evidence, page_fingerprint, last_processed, unavailable)
        if more_candidates else None
    )
    return await progress(next_cursor, not more_candidates, processed, unavailable)


async def list_conversation_approvals(
    session: AsyncSession,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    conversation_id: UUID, auth_session_hash: str,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> list[ApprovalRead]:
    """List bounded actions only while the owner session, Chat link and current output fences remain valid."""
    from core.auth.public import revalidate_owner_session
    from modules.chat.public import list_agent_run_ids_for_owner

    await admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    owner_id = actor(scope)
    if not await revalidate_owner_session(session, auth_session_hash, owner_id):
        return []

    run_ids = await list_agent_run_ids_for_owner(session, conversation_id, owner_id, auth_session_hash)
    if not run_ids:
        return []
    rows = list((await session.scalars(select(AgentApproval).where(
        AgentApproval.run_id.in_(run_ids), AgentApproval.workspace_id == scope.workspace_id,
        AgentApproval.owner_id == owner_id,
        AgentApproval.arguments.is_not(None),
        AgentApproval.status.in_({"pending", "approved", "denied", "expired", "cancelled", "requires_review"}),
    ).order_by(AgentApproval.created_at.desc()).limit(50))).all())
    effects = {item[0]: (item[1], item[2]) for item in (await session.execute(select(
        AgentEffect.action_id, AgentEffect.result_reference, AgentEffect.state,
    ).where(
        AgentEffect.action_id.in_([item.action_id for item in rows]),
        AgentEffect.workspace_id == scope.workspace_id,
    ))).all()} if rows else {}
    reads: list[ApprovalRead] = []
    for item in rows:
        run_revoked = await _approval_run_is_revoked(session_factory, item.run_id, scope.workspace_id)
        fences_current = (
            not run_revoked and item.auth_session_hash == auth_session_hash
            and await _approval_fences_current(
                session_factory, item, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
        )
        from modules.chat.public import has_live_agent_run_link
        session_current = await revalidate_owner_session(session, auth_session_hash, owner_id)
        link_current = session_current and await has_live_agent_run_link(
            session, item.run_id, owner_id, auth_session_hash,
        )
        if not link_current:
            continue
        if not fences_current and not run_revoked:
            await _invalidate_stale_approval(session, item)
            current_effect = await session.execute(select(
                AgentEffect.result_reference, AgentEffect.state,
            ).where(
                AgentEffect.action_id == item.action_id, AgentEffect.workspace_id == scope.workspace_id,
            ))
            effect_row = current_effect.one_or_none()
            if effect_row is not None:
                effects[item.action_id] = (effect_row[0], effect_row[1])
        reads.append(ApprovalRead(
            id=item.id, action_id=item.action_id, run_id=item.run_id, conversation_id=conversation_id,
            tool_name=item.tool_name, tool_version=item.tool_version,
            arguments=item.arguments if fences_current else None,
            argument_hash=item.argument_hash, destination_id=item.destination_id,
            destination_revision=item.destination_revision, status=item.status,
            effect_status=effects.get(item.action_id, (None, None))[1],
            result_reference=effects.get(item.action_id, (None, None))[0],
            created_at=item.created_at, expires_at=item.expires_at,
        ))
    return reads


async def get_approval(
    session: AsyncSession,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    approval_id: UUID, auth_session_hash: str,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> ApprovalRead:
    """Return exact bounded action detail only while its original Chat link and owner session are live."""
    from core.auth.public import revalidate_owner_session
    from modules.chat.public import live_agent_conversation_id

    await admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    owner_id = actor(scope)
    if not await revalidate_owner_session(session, auth_session_hash, owner_id):
        raise HTTPException(status_code=404, detail="Approval not found")
    item = await session.scalar(select(AgentApproval).where(
        AgentApproval.id == approval_id, AgentApproval.workspace_id == scope.workspace_id,
        AgentApproval.owner_id == owner_id,
        AgentApproval.auth_session_hash == auth_session_hash,
        AgentApproval.arguments.is_not(None),
    ))
    conversation_id = (
        await live_agent_conversation_id(session, item.run_id, owner_id, auth_session_hash)
        if item is not None else None
    )
    if item is None or conversation_id is None or item.arguments is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    run_revoked = await _approval_run_is_revoked(session_factory, item.run_id, scope.workspace_id)
    fences_current = not run_revoked and await _approval_fences_current(
        session_factory, item, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if (not await revalidate_owner_session(session, auth_session_hash, owner_id)
            or not await live_agent_conversation_id(
                session, item.run_id, owner_id, auth_session_hash,
            )):
        raise HTTPException(status_code=404, detail="Approval not found")
    if not fences_current and not run_revoked:
        await _invalidate_stale_approval(session, item)
    effect = await session.scalar(select(AgentEffect).where(
        AgentEffect.action_id == item.action_id, AgentEffect.workspace_id == scope.workspace_id,
    ))
    return ApprovalRead(
        id=item.id, action_id=item.action_id, run_id=item.run_id, conversation_id=conversation_id,
        tool_name=item.tool_name, tool_version=item.tool_version,
        arguments=item.arguments if fences_current else None,
        argument_hash=item.argument_hash, destination_id=item.destination_id,
        destination_revision=item.destination_revision, status=item.status,
        effect_status=effect.state if effect is not None else None,
        result_reference=effect.result_reference if effect is not None else None,
        created_at=item.created_at, expires_at=item.expires_at,
    )


async def _approval_fences_current(
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    item: AgentApproval,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Revalidate this action's persisted workspace-scoped evidence snapshot only."""
    principal = ToolExecutionPrincipal(
        actor_id=f"owner:{actor(scope)}", scope=scope, is_owner=True,
        allowed_tools=frozenset({item.tool_name}), owner_all_sources=True,
        destinations=frozenset(), capabilities=frozenset({"source.read"}),
    )
    try:
        sink = _restore_fences(item.source_fences)
    except (TypeError, ValueError, KeyError):
        return False
    return await revalidate_native_output_fences(
        session_factory, sink, principal,
        destination_kind="remote", multi_workspace_enabled=multi_workspace_enabled,
    )


async def _approval_run_is_revoked(
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]], run_id: UUID,
    workspace_id: UUID,
) -> bool:
    """Hide action arguments for a revoked/missing run of this workspace without mutating its action ledger."""
    async with session_factory() as session:
        revoked = await session.scalar(select(AgentRun.evidence_revoked).where(
            AgentRun.id == run_id, AgentRun.workspace_id == workspace_id,
        ))
    return revoked is None or revoked


async def _invalidate_stale_approval(session: AsyncSession, item: AgentApproval) -> None:
    """Redact stale action evidence and durably deny an unconsumed pending slot under owner locks."""
    run = await session.scalar(select(AgentRun).where(
        AgentRun.id == item.run_id, AgentRun.workspace_id == item.workspace_id,
    ).with_for_update())
    if run is None or run.evidence_revoked:
        # The run-wide read/replay fence must not destructively scrub an independent approval.
        return
    row = await session.scalar(select(AgentApproval).where(
        AgentApproval.id == item.id, AgentApproval.workspace_id == item.workspace_id,
    ).with_for_update())
    if row is None:
        return
    effect = await session.scalar(select(AgentEffect).where(
        AgentEffect.action_id == row.action_id, AgentEffect.workspace_id == item.workspace_id,
    ).with_for_update())
    if row.status in {"pending", "approved"}:
        now = datetime.now(UTC)
        if effect is not None and effect.state in {"in_flight", "requires_review"}:
            effect.state, effect.payload = "requires_review", None
            row.status = "requires_review"
        else:
            if effect is not None and effect.state == "reserved":
                effect.state, effect.payload = "failed", None
            row.status = "cancelled"
            call = await session.scalar(select(AgentToolCall).where(
                AgentToolCall.run_id == run.id,
                AgentToolCall.ordinal == row.ordinal,
            ).with_for_update())
            if call is not None:
                call.status, call.error_code, call.completed_at = "denied", "source_permissions_changed", now
            if run.status == "waiting_approval" and not run.cancel_requested:
                run.status, run.dispatch_generation = "queued", run.dispatch_generation + 1
                run.updated_at = now
        row.resolved_at = now
    row.arguments = None
    row.source_fences = {}
    await session.commit()


async def purge_conversation_actions(
    session: AsyncSession, conversation_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> int:
    """Cancel linked runs and redact action payloads before Chat deletes their live activity link.

    Effect tombstones survive independently. An in-flight effect is retained as review-only because
    conversation deletion cannot prove whether the remote receiver accepted the request.
    """
    from modules.chat.public import list_agent_run_ids_for_delete

    purged = 0
    cursor: UUID | None = None
    while True:
        run_ids = await list_agent_run_ids_for_delete(session, conversation_id, actor(scope), cursor)
        if not run_ids:
            return purged
        purged += await purge_agent_runs(
            session, run_ids, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        cursor = run_ids[-1]


async def purge_agent_runs(
    session: AsyncSession, run_ids: list[UUID], *, scope: Scope, multi_workspace_enabled: bool,
) -> int:
    """Cancel and redact a bounded set of Chat-owned runs while retaining independent effect tombstones.

    The caller holds the admission fence and Chat locks; this admits ``scope`` without locking and
    selects only runs of its workspace and actor before the row locks.
    """
    await admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    runs = list((await session.scalars(select(AgentRun).where(
        AgentRun.id.in_(run_ids), AgentRun.workspace_id == scope.workspace_id,
        AgentRun.owner_id == actor(scope),
    ).order_by(AgentRun.id).with_for_update())).all()) if run_ids else []
    for run in runs:
        approvals = list((await session.scalars(select(AgentApproval).where(
            AgentApproval.run_id == run.id,
        ).order_by(AgentApproval.id).with_for_update())).all())
        effects = list((await session.scalars(select(AgentEffect).where(
            AgentEffect.run_id == run.id,
        ).order_by(AgentEffect.action_id).with_for_update())).all())
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
                approval.status = "requires_review" if approval_effect and approval_effect.state == "requires_review" else "cancelled"
                approval.resolved_at = datetime.now(UTC)
            approval.arguments = None
            approval.source_fences = {}
        await session.execute(update(AgentToolCall).where(
            AgentToolCall.run_id == run.id,
        ).values(arguments={}))
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
                text(f"DELETE FROM {table} WHERE thread_id = :thread_id"),
                {"thread_id": run.checkpoint_thread_id},
            )
    await purge_browser_results_in_uow(
        session, run_ids=[run.id for run in runs], scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    return len(runs)


async def request_cancel(
    session: AsyncSession,
    run_id: UUID,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    *, scope: Scope, multi_workspace_enabled: bool,
) -> AgentRunRead:
    """Commit PostgreSQL cancellation intent, terminalizing unclaimed work immediately.

    The access fence is locked first and validates the cancellation commit.

    Running executions observe the committed flag before every gateway retry, tool call and
    output publication. A completed remote read cannot be undone; its late result is fenced.
    Linked chat status is an optional bounded side effect and is retried from durable state.
    """
    fence = await admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    row = await session.scalar(
        select(AgentRun).where(
            AgentRun.id == run_id, AgentRun.workspace_id == scope.workspace_id,
            AgentRun.owner_id == actor(scope),
        ).with_for_update()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Agent run not found")
    if row.status not in TERMINAL_STATUSES:
        row.cancel_requested = True
        approvals = list((await session.scalars(select(AgentApproval).where(
            AgentApproval.run_id == run_id,
            AgentApproval.status.in_({"pending", "approved"}),
        ).order_by(AgentApproval.id).with_for_update())).all())
        effects = list((await session.scalars(select(AgentEffect).where(
            AgentEffect.run_id == run_id,
        ).order_by(AgentEffect.action_id).with_for_update())).all())
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
            session, run_ids=[run_id], scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        if row.status in {"queued", "waiting_approval"}:
            row.status = "cancelled"
            row.completed_at = datetime.now(UTC)
            row.error_code = None
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                await session.execute(
                    text(f"DELETE FROM {table} WHERE thread_id = :thread_id"),
                    {"thread_id": row.checkpoint_thread_id},
                )
        await commit_with_replay(
            session, (), scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence,
        )
        await session.refresh(row)
        publication = (row.owner_id, row.auth_session_hash, row.status)
        await publish_agent_activity_safely(
            session_factory, run_id=run_id, owner_id=publication[0],
            auth_session_hash=publication[1], status=publication[2],
        )
    result = _read(row)
    if result.answer is not None and row.source_fences:
        # Reuse the same fresh source authorization before returning an answer from any route.
        principal = _result_principal(row)
        try:
            restored: dict[str, Any] | None = _restore_fences(row.source_fences)
        except (TypeError, ValueError, KeyError):
            restored = None
        current = bool(restored is not None and principal and await revalidate_native_output_fences(
            session_factory, restored, principal, destination_kind="remote",
            multi_workspace_enabled=multi_workspace_enabled,
        ))
        if not current:
            result = result.model_copy(update={"answer": None})
    return result


async def request_cancel_for_owner(
    session: AsyncSession,
    run_id: UUID,
    auth_session_hash: str,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    *, scope: Scope, multi_workspace_enabled: bool,
) -> AgentRunRead:
    """Cancel only a run whose original session and live Chat parent authorize the action.

    Chat authorization and its conversation lock precede ``request_cancel``'s run and approval
    locks, matching the current Chat-delete order. Since ``request_cancel`` commits before returning,
    the live Chat link is reauthorized under the conversation lock afterward; a concurrent delete
    therefore suppresses the result. The legacy cancellation signature stays frozen.
    """
    await admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    owner_id = actor(scope)
    row = await session.scalar(select(AgentRun).where(
        AgentRun.id == run_id, AgentRun.workspace_id == scope.workspace_id,
        AgentRun.owner_id == owner_id,
    ))
    if row is None or row.auth_session_hash != auth_session_hash:
        raise HTTPException(status_code=404, detail="Agent run not found")
    if row.chat_link_required:
        from modules.chat.public import authorize_agent_run_access

        allowed = await authorize_agent_run_access(
            session, run_id, owner_id, auth_session_hash,
            require_original_session=True, lock_conversation=True,
        )
        if not allowed:
            raise HTTPException(status_code=404, detail="Agent run not found")
    result = await request_cancel(
        session, run_id, session_factory, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if row.chat_link_required:
        # Cancellation commits internally; reacquire the parent lock before returning linked data.
        allowed = await authorize_agent_run_access(
            session, run_id, owner_id, auth_session_hash,
            require_original_session=True, lock_conversation=True,
        )
        if not allowed:
            raise HTTPException(status_code=404, detail="Agent run not found")
    return result


async def current_profile_revision(
    session: AsyncSession, profile_id: str, registry: ToolRegistry, config: Any,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> int:
    """Return the selected profile's current revision (for a caller that pins ``expected_profile_revision``).

    Raises ``HTTPException`` 404 for an unknown profile, exactly like the profile read route.
    """
    from modules.agents.specialists import get_profile

    return (await get_profile(
        session, profile_id, registry, config,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )).revision


__all__ = [
    "APPROVAL_PROMPT_VERSION",
    "APPROVAL_WORKFLOW_TOOLS",
    "APPROVAL_WORKFLOW_VERSION",
    "CHECKPOINT_SCHEMA_VERSION",
    "PROMPT_VERSION",
    "SPECIALIST_CHECKPOINT_SCHEMA_VERSION",
    "SPECIALIST_PROMPT_VERSION",
    "SPECIALIST_WORKFLOW_VERSION",
    "WORKFLOW_TOOLS",
    "WORKFLOW_VERSION",
    "AgentCleanupLeasePreflight",
    "AgentCopiedEvidenceCleanupProgress",
    "BrowserRunAuthorization",
    "ProfileRunStart",
    "create_profile_run_in_uow",
    "create_run",
    "current_profile_revision",
    "get_approval",
    "get_run",
    "get_run_for_owner",
    "get_run_meta_by_id",
    "list_agent_trace_workspace_ids",
    "list_conversation_approvals",
    "list_run_meta",
    "list_runs",
    "lock_write_admission",
    "preflight_document_copied_evidence_lease",
    "purge_agent_runs",
    "purge_conversation_actions",
    "purge_document_copied_evidence_page",
    "redact_expired_agent_traces",
    "request_cancel",
    "request_cancel_for_owner",
    "reserve_browser_run_budget_in_uow",
    "revalidate_browser_run_authority",
]


async def list_run_meta(session: AsyncSession, limit: int) -> list[_RunMeta]:
    """Return at most ``limit`` (<=100) newest agent runs as metadata only; unknown token usage is None, never 0."""
    rows = await session.scalars(select(AgentRun).order_by(AgentRun.created_at.desc()).limit(min(limit, 100)))
    return [_RunMeta(kind="agent", id=str(r.id), status=r.status, error_code=r.error_code,
                     created_at=r.created_at, updated_at=r.updated_at, finished_at=r.completed_at,
                     token_usage=None if r.token_usage_unknown else r.token_usage)
            for r in rows]


async def get_run_meta_by_id(session: AsyncSession, run_id: UUID) -> _RunMeta | None:
    """Return one metadata-only run projection by its indexed primary key."""
    row = await session.get(AgentRun, run_id)
    if row is None:
        return None
    return _RunMeta(kind="agent", id=str(row.id), status=row.status, error_code=row.error_code,
                    created_at=row.created_at, updated_at=row.updated_at, finished_at=row.completed_at,
                    token_usage=None if row.token_usage_unknown else row.token_usage)


async def list_agent_trace_workspace_ids(
    session: AsyncSession, *, after: UUID | None = None, limit: int = 100,
) -> tuple[UUID, ...]:
    """Return at most ``limit`` (<=100) distinct run workspace IDs after ``after``, in ID order.

    Identity-only discovery for the retention maintenance pass: it reads no content and grants no
    authority. The caller derives a per-workspace job scope from the workspace owner, then calls
    ``redact_expired_agent_traces`` under that scope, one workspace per transaction.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Workspace discovery page size must be between 1 and 100")
    statement = select(AgentRun.workspace_id).distinct()
    if after is not None:
        statement = statement.where(AgentRun.workspace_id > after)
    return tuple((await session.scalars(statement.order_by(AgentRun.workspace_id).limit(limit))).all())


async def redact_expired_agent_traces(
    session: AsyncSession, *, cutoff: datetime, limit: int = 100,
    scope: Scope, multi_workspace_enabled: bool,
) -> int:
    """Redact a bounded batch of old terminal run payloads while preserving canonical outcomes and action ledgers.

    Admission precedes the query and only this workspace's runs are candidates (predicate before LIMIT).

    Runs with unresolved approvals or reserved, in-flight, or review-required effects are excluded
    before LIMIT so they cannot starve later eligible runs, then their ledgers are locked and
    rechecked. The caller owns transaction commit.
    """
    if not 1 <= limit <= 500:
        raise ValueError("Retention batch size must be between 1 and 500")
    await admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    unresolved_approvals = select(AgentApproval.run_id).where(
        AgentApproval.run_id == AgentRun.id,
        AgentApproval.status.in_({"pending", "approved", "requires_review"}),
    ).exists()
    unresolved_effects = select(AgentEffect.run_id).where(
        AgentEffect.run_id == AgentRun.id,
        AgentEffect.state.in_({"reserved", "in_flight", "requires_review"}),
    ).exists()
    candidates = list((await session.scalars(select(AgentRun).where(
        AgentRun.workspace_id == scope.workspace_id,
        AgentRun.status.in_(TERMINAL_STATUSES), AgentRun.completed_at < cutoff,
        AgentRun.trace_redacted_at.is_(None), ~unresolved_approvals, ~unresolved_effects,
    ).order_by(AgentRun.completed_at, AgentRun.id).limit(limit).with_for_update(skip_locked=True))).all())
    if not candidates:
        return 0
    run_ids = [row.id for row in candidates]
    # Lock and recheck child ledgers after the run locks; writers serialize their action changes
    # through the same AgentRun row, so an unresolved effect cannot race trace redaction.
    unresolved_approval_runs = set((await session.scalars(select(AgentApproval.run_id).where(
        AgentApproval.run_id.in_(run_ids),
        AgentApproval.status.in_({"pending", "approved", "requires_review"}),
    ).with_for_update())).all())
    unresolved_effect_runs = set((await session.scalars(select(AgentEffect.run_id).where(
        AgentEffect.run_id.in_(run_ids),
        AgentEffect.state.in_({"reserved", "in_flight", "requires_review"}),
    ).with_for_update())).all())
    now = datetime.now(UTC)
    redacted = 0
    eligible_ids: list[UUID] = []
    for row in candidates:
        if row.id in unresolved_approval_runs or row.id in unresolved_effect_runs:
            continue
        row.prompt = ""
        row.answer = None
        row.activities = []
        row.profile_snapshot = None
        row.source_fences = {}
        row.trace_redacted_at = now
        eligible_ids.append(row.id)
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            await session.execute(text(f"DELETE FROM {table} WHERE thread_id = :thread_id"),
                                  {"thread_id": row.checkpoint_thread_id})
        redacted += 1
    if eligible_ids:
        # Keep action identity, hashes, state, outcome and provider idempotency keys;
        # remove only resolved argument/effect payloads and their source trace details.
        await session.execute(update(AgentToolCall).where(
            AgentToolCall.run_id.in_(eligible_ids),
        ).values(arguments={}, evidence_refs=[]))
        await session.execute(update(AgentApproval).where(
            AgentApproval.run_id.in_(eligible_ids),
        ).values(arguments=None, source_fences={}))
        await session.execute(update(AgentEffect).where(
            AgentEffect.run_id.in_(eligible_ids),
        ).values(payload=None))
    await session.flush()
    return redacted
