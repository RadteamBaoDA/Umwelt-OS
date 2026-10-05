"""Owner-scoped agent run creation, lookup, and cancellation contracts."""

from collections.abc import Callable
import asyncio
import hashlib
import json
from dataclasses import dataclass
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
import logging
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select, text, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.tools import ToolRegistry, ToolRisk
from core.tools.schemas import ToolExecutionPrincipal, ToolOutputFence
from modules.agents.models import AgentApproval, AgentEffect, AgentProfile, AgentRun, AgentToolCall
from modules.agents.schemas import ApprovalRead, AgentRunPage, AgentRunRead, AgentRunStart, ProfileRunStart
from modules.agents.specialists import resolve_profile_snapshot
from modules.agents.models import AgentProfileRevision
from core.pagination import decode_cursor, encode_cursor
from modules.tools.public import purge_browser_results_in_uow, revalidate_native_output_fences

WORKFLOW_VERSION = "assistant-readonly-v1"
APPROVAL_WORKFLOW_VERSION = "assistant-approved-v1"
PROMPT_VERSION = "assistant-prompt-v1"
APPROVAL_PROMPT_VERSION = "assistant-approval-prompt-v1"
SPECIALIST_WORKFLOW_VERSION = "specialist-approved-v1"
SPECIALIST_PROMPT_VERSION = "specialist-prompt-v1"
CHECKPOINT_SCHEMA_VERSION = 1
SPECIALIST_CHECKPOINT_SCHEMA_VERSION = 2
WORKFLOW_TOOLS = frozenset({
    "knowledge.get_document", "knowledge.list_documents", "search.query",
    "sources.list_sources", "sources.get_source",
})
APPROVAL_WORKFLOW_TOOLS = frozenset({*WORKFLOW_TOOLS, "webhook.send"})
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
logger = logging.getLogger(__name__)


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


async def reserve_browser_run_budget_in_uow(
    session: AsyncSession, owner_id: int, run_id: UUID, claim_generation: int,
    tool_slot: int, args_digest: str, requested_pages: int,
) -> BrowserRunAuthorization:
    """Reserve run-wide browser ceilings once for an exact durable tool slot.

    The run row serializes concurrent slots and survives worker handoff. An exact
    duplicate does not spend budget twice; changed arguments for a slot conflict.
    The returned object contains no ORM row or owner privilege.
    """
    from modules.chat import public as chat

    if (
        owner_id != 1 or type(claim_generation) is not int or claim_generation < 1
        or type(tool_slot) is not int or not 1 <= tool_slot <= 10
        or type(requested_pages) is not int or not 1 <= requested_pages <= 3
        or len(args_digest) != 64
    ):
        raise PermissionError("Browser run authority is invalid")
    run = await session.scalar(
        select(AgentRun).where(AgentRun.id == run_id, AgentRun.owner_id == owner_id).with_for_update()
    )
    if (
        run is None or run.status != "running" or run.cancel_requested
        or run.claim_generation != claim_generation or run.auth_session_hash is None
    ):
        raise PermissionError("Browser run claim is no longer current")
    profile_id = str((run.profile_snapshot or {}).get("id", ""))
    raw_sources = (run.profile_snapshot or {}).get("source_ids")
    try:
        source_ids = frozenset(UUID(item) for item in raw_sources) if isinstance(raw_sources, list) else frozenset()
    except (TypeError, ValueError):
        raise PermissionError("Browser profile source scope is invalid")
    profile = await session.get(AgentProfile, profile_id)
    if not _browser_profile_current(run, profile):
        raise PermissionError("Current specialist profile no longer permits browser reads")
    tool_call = await session.scalar(select(AgentToolCall).where(
        AgentToolCall.run_id == run_id, AgentToolCall.ordinal == tool_slot,
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
    session: AsyncSession, authorization: BrowserRunAuthorization
) -> bool:
    """Recheck the live run claim, session, profile, and Chat link before each browser request."""
    from modules.chat import public as chat

    run = await session.scalar(select(AgentRun).where(
        AgentRun.id == authorization.run_id,
        AgentRun.owner_id == authorization.owner_id,
    ).with_for_update())
    if (
        run is None or run.status != "running" or run.cancel_requested
        or run.claim_generation != authorization.claim_generation
        or run.auth_session_hash != authorization.auth_session_hash
        or run.profile_revision_hash != authorization.profile_revision_hash
    ):
        return False
    profile = await session.scalar(select(AgentProfile).where(
        AgentProfile.profile_id == authorization.profile_id,
        AgentProfile.owner_id == authorization.owner_id,
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
    except Exception as exc:
        logger.warning("Agent activity delivery deferred for %s (%s)", run_id, type(exc).__name__)


def _read(row: AgentRun) -> AgentRunRead:
    """Project private persisted fields into the bounded owner response."""
    return AgentRunRead(
        id=row.id, agent_id=row.agent_id, status=row.status, answer=row.answer,
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
    """Rebuild output authorization from a profile's original exact source grant, failing closed on malformed snapshots."""
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
        actor_id=f"owner:{row.owner_id}", is_owner=True,
        allowed_tools=frozenset(row.allowed_tools), source_ids=source_ids,
        owner_all_sources=owner_all_sources, destinations=frozenset(),
        capabilities=frozenset(capabilities),
    )


async def create_profile_run_in_uow(
    session: AsyncSession, owner_id: int, auth_session_hash: str, profile_id: str,
    request: ProfileRunStart, registry: ToolRegistry, config: Any,
) -> AgentRunRead:
    """Persist a replay-safe linked profile run without committing the caller's run/link transaction.

    The idempotency key is scoped to the owner session digest. A byte-identical retry returns its
    original run; reusing the key for different prompt, profile revision, or conversation is a 409.
    Token budgets remain explicitly unavailable and are rejected before worker/model egress.
    """
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
    lock_material = f"{owner_id}:{auth_session_hash}:{request.client_request_id}".encode("utf-8")
    lock_key = int.from_bytes(hashlib.sha256(lock_material).digest()[:8], "big", signed=True)
    # Serialize the absent-row case as well as ordinary reads so parallel retries cannot create duplicate work.
    await session.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key})
    existing = await session.scalar(select(AgentRun).where(
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
        return await _read_current_result(existing, retry_factory)
    snapshot, snapshot_hash = await resolve_profile_snapshot(
        session, owner_id, profile_id, request.expected_profile_revision, registry, config,
    )
    revision = snapshot["revision"]
    if revision:
        revision_row = await session.scalar(select(AgentProfileRevision).where(
            AgentProfileRevision.owner_id == owner_id,
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
        id=uuid4(), owner_id=owner_id, auth_session_hash=auth_session_hash,
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
    session: AsyncSession, owner_id: int, *, profile_id: str | None = None,
    conversation_id: UUID | None = None, auth_session_hash: str | None = None,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    limit: int = 25, cursor: str | None = None,
) -> AgentRunPage:
    """Page bounded owner run history after Chat-link and current-output authorization.

    Candidate scans stop after a bounded number of rows. Linked runs are filtered by Chat's
    session/expiry projection, and answer-bearing rows pass the same current evidence fence as
    direct run reads. The opaque cursor advances over examined candidates so expired ephemeral
    links cannot hide later live runs or leak their retained answers.
    """
    if not 1 <= limit <= 25:
        raise HTTPException(status_code=422, detail="Run history page size is outside its supported bound")
    anchor = decode_cursor(cursor) if cursor else None
    page: list[AgentRun] = []
    scanned = 0
    has_more = False
    scan_anchor = anchor
    while scanned < 100 and len(page) <= limit:
        statement = select(AgentRun).where(AgentRun.owner_id == owner_id)
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
    if has_more:
        # Continue after the last returned row so the first overflow row remains on the next page.
        next_cursor = encode_cursor(page[limit - 1].created_at, page[limit - 1].id)
        page = page[:limit]
    elif scanned >= 100:
        next_cursor = encode_cursor(*scan_anchor) if scan_anchor else None
    else:
        next_cursor = None
    return AgentRunPage(
        items=[await _read_current_result(row, session_factory) for row in page],
        next_cursor=next_cursor,
    )


async def create_run(
    session: AsyncSession,
    auth_session_hash: str,
    request: AgentRunStart,
    registry: ToolRegistry,
) -> AgentRunRead:
    """Persist one exact-version read-only workflow and its durable dispatch generation.

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
    definitions = {
        item.name: item for item in registry.list_tools(allowed_tools=APPROVAL_WORKFLOW_TOOLS)
        if (item.risk == ToolRisk.READ_ONLY and not item.confirmation_required
            or item.name == "webhook.send" and item.risk == ToolRisk.EXTERNAL_WRITE
            and item.confirmation_required)
    }
    allowed = sorted(definitions)
    if not allowed:
        raise HTTPException(status_code=503, detail="Agent tools are unavailable")
    run = AgentRun(
        id=uuid4(), owner_id=1, auth_session_hash=auth_session_hash,
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

        await link_agent_run(session, request.conversation_id, run.id, 1, auth_session_hash)
    await session.commit()
    await session.refresh(run)
    return _read(run)


async def get_run(
    session: AsyncSession,
    run_id: UUID,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
) -> AgentRunRead:
    """Return owner-visible run state and suppress answers with stale source evidence."""
    row = await session.scalar(select(AgentRun).where(AgentRun.id == run_id, AgentRun.owner_id == 1))
    if row is None:
        raise HTTPException(status_code=404, detail="Agent run not found")
    return await _read_current_result(row, session_factory)


async def get_run_for_owner(
    session: AsyncSession,
    run_id: UUID,
    owner_id: int,
    auth_session_hash: str,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
) -> AgentRunRead:
    """Read a run only after Chat confirms its linked conversation is retained for this owner.

    The frozen ``get_run`` API remains the result projector. Linked runs use Chat's lifecycle
    contract; persistent links permit the current owner, while ephemeral links remain session-bound.
    Unlinked assistant runs are still readable by the authenticated owner.
    """
    row = await session.scalar(select(AgentRun).where(
        AgentRun.id == run_id, AgentRun.owner_id == owner_id,
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
    return await get_run(session, run_id, session_factory)


async def _read_current_result(
    row: AgentRun,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] | None,
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
            sink = _restore_fences(row.source_fences)
            current = bool(principal and await revalidate_native_output_fences(
                session_factory, sink, principal, destination_kind="remote",
            ))
        except (TypeError, ValueError, KeyError):
            current = False
        if not current:
            return result.model_copy(update={"answer": None})
    elif row.source_fences and result.answer is not None:
        return result.model_copy(update={"answer": None})
    return result


def _restore_fences(value: dict[str, object]) -> dict[str, object]:
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


async def list_conversation_approvals(
    session: AsyncSession,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    conversation_id: UUID, owner_id: int, auth_session_hash: str,
) -> list[ApprovalRead]:
    """List bounded actions only while the owner session, Chat link and current output fences remain valid."""
    from modules.chat.public import list_agent_run_ids_for_owner
    from core.auth.public import revalidate_owner_session

    if not await revalidate_owner_session(session, auth_session_hash, owner_id):
        return []

    run_ids = await list_agent_run_ids_for_owner(session, conversation_id, owner_id, auth_session_hash)
    if not run_ids:
        return []
    rows = list((await session.scalars(select(AgentApproval).where(
        AgentApproval.run_id.in_(run_ids), AgentApproval.owner_id == owner_id,
        AgentApproval.arguments.is_not(None),
        AgentApproval.status.in_({"pending", "approved", "denied", "expired", "cancelled", "requires_review"}),
    ).order_by(AgentApproval.created_at.desc()).limit(50))).all())
    effects = dict((item[0], (item[1], item[2])) for item in (await session.execute(select(
        AgentEffect.action_id, AgentEffect.result_reference, AgentEffect.state,
    ).where(
        AgentEffect.action_id.in_([item.action_id for item in rows]),
    ))).all()) if rows else {}
    reads: list[ApprovalRead] = []
    for item in rows:
        fences_current = item.auth_session_hash == auth_session_hash and await _approval_fences_current(
            session_factory, item, owner_id,
        )
        from modules.chat.public import has_live_agent_run_link
        session_current = await revalidate_owner_session(session, auth_session_hash, owner_id)
        link_current = session_current and await has_live_agent_run_link(
            session, item.run_id, owner_id, auth_session_hash,
        )
        if not link_current:
            continue
        if not fences_current:
            await _invalidate_stale_approval(session, item)
            current_effect = await session.execute(select(
                AgentEffect.result_reference, AgentEffect.state,
            ).where(AgentEffect.action_id == item.action_id))
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
    approval_id: UUID, owner_id: int, auth_session_hash: str,
) -> ApprovalRead:
    """Return exact bounded action detail only while its original Chat link and owner session are live."""
    from modules.chat.public import live_agent_conversation_id
    from core.auth.public import revalidate_owner_session

    if not await revalidate_owner_session(session, auth_session_hash, owner_id):
        raise HTTPException(status_code=404, detail="Approval not found")
    item = await session.scalar(select(AgentApproval).where(
        AgentApproval.id == approval_id, AgentApproval.owner_id == owner_id,
        AgentApproval.auth_session_hash == auth_session_hash,
        AgentApproval.arguments.is_not(None),
    ))
    conversation_id = (
        await live_agent_conversation_id(session, item.run_id, owner_id, auth_session_hash)
        if item is not None else None
    )
    if item is None or conversation_id is None or item.arguments is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    fences_current = await _approval_fences_current(session_factory, item, owner_id)
    if (not await revalidate_owner_session(session, auth_session_hash, owner_id)
            or not await live_agent_conversation_id(
                session, item.run_id, owner_id, auth_session_hash,
            )):
        raise HTTPException(status_code=404, detail="Approval not found")
    if not fences_current:
        await _invalidate_stale_approval(session, item)
    effect = await session.scalar(select(AgentEffect).where(AgentEffect.action_id == item.action_id))
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
    owner_id: int,
) -> bool:
    """Revalidate the persisted evidence snapshot using owner-scoped tool output contracts."""
    principal = ToolExecutionPrincipal(
        actor_id=f"owner:{owner_id}", is_owner=True,
        allowed_tools=frozenset({item.tool_name}), owner_all_sources=True,
        destinations=frozenset(), capabilities=frozenset({"source.read"}),
    )
    try:
        return await revalidate_native_output_fences(
            session_factory, _restore_fences(item.source_fences), principal,
            destination_kind="remote",
        )
    except (TypeError, ValueError, KeyError):
        return False


async def _invalidate_stale_approval(session: AsyncSession, item: AgentApproval) -> None:
    """Redact stale action evidence and durably deny an unconsumed pending slot under owner locks."""
    run = await session.scalar(select(AgentRun).where(
        AgentRun.id == item.run_id,
    ).with_for_update())
    row = await session.scalar(select(AgentApproval).where(
        AgentApproval.id == item.id,
    ).with_for_update())
    if run is None or row is None:
        return
    effect = await session.scalar(select(AgentEffect).where(
        AgentEffect.action_id == row.action_id,
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
    session: AsyncSession, conversation_id: UUID, owner_id: int,
) -> int:
    """Cancel linked runs and redact action payloads before Chat deletes their live activity link.

    Effect tombstones survive independently. An in-flight effect is retained as review-only because
    conversation deletion cannot prove whether the remote receiver accepted the request.
    """
    from modules.chat.public import list_agent_run_ids_for_delete

    purged = 0
    cursor: UUID | None = None
    while True:
        run_ids = await list_agent_run_ids_for_delete(session, conversation_id, owner_id, cursor)
        if not run_ids:
            return purged
        purged += await purge_agent_runs(session, run_ids, owner_id)
        cursor = run_ids[-1]


async def purge_agent_runs(session: AsyncSession, run_ids: list[UUID], owner_id: int = 1) -> int:
    """Cancel and redact a bounded set of Chat-owned runs while retaining independent effect tombstones."""
    runs = list((await session.scalars(select(AgentRun).where(
        AgentRun.id.in_(run_ids), AgentRun.owner_id == owner_id,
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
            effect = effects_by_id.get(approval.action_id)
            if approval.status in {"pending", "approved"}:
                approval.status = "requires_review" if effect and effect.state == "requires_review" else "cancelled"
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
    await purge_browser_results_in_uow(session, run_ids=[run.id for run in runs])
    return len(runs)


async def request_cancel(
    session: AsyncSession,
    run_id: UUID,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
) -> AgentRunRead:
    """Commit PostgreSQL cancellation intent, terminalizing unclaimed work immediately.

    Running executions observe the committed flag before every gateway retry, tool call and
    output publication. A completed remote read cannot be undone; its late result is fenced.
    Linked chat status is an optional bounded side effect and is retried from durable state.
    """
    row = await session.scalar(
        select(AgentRun).where(AgentRun.id == run_id, AgentRun.owner_id == 1).with_for_update()
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
            elif effect is not None and effect.state == "reserved":
                approval.status = "cancelled"
            elif approval.status == "pending":
                approval.status = "cancelled"
            approval.resolved_at = datetime.now(UTC)
        for effect in effects:
            if effect.state == "in_flight":
                effect.state = "requires_review"
            elif effect.state == "reserved":
                effect.state = "failed"
            effect.payload = None
        await purge_browser_results_in_uow(session, run_ids=[run_id])
        if row.status in {"queued", "waiting_approval"}:
            row.status = "cancelled"
            row.completed_at = datetime.now(UTC)
            row.error_code = None
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                await session.execute(
                    text(f"DELETE FROM {table} WHERE thread_id = :thread_id"),
                    {"thread_id": row.checkpoint_thread_id},
                )
        await session.commit()
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
            current = bool(principal and await revalidate_native_output_fences(
                session_factory, _restore_fences(row.source_fences), principal, destination_kind="remote",
            ))
        except (TypeError, ValueError, KeyError):
            current = False
        if not current:
            result = result.model_copy(update={"answer": None})
    return result


async def request_cancel_for_owner(
    session: AsyncSession,
    run_id: UUID,
    owner_id: int,
    auth_session_hash: str,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
) -> AgentRunRead:
    """Cancel only a run whose original session and live Chat parent authorize the action.

    Chat authorization and its conversation lock precede ``request_cancel``'s run and approval
    locks, matching the current Chat-delete order. Since ``request_cancel`` commits before returning,
    the live Chat link is reauthorized under the conversation lock afterward; a concurrent delete
    therefore suppresses the result. The legacy cancellation signature stays frozen.
    """
    row = await session.scalar(select(AgentRun).where(
        AgentRun.id == run_id, AgentRun.owner_id == owner_id,
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
    result = await request_cancel(session, run_id, session_factory)
    if row.chat_link_required:
        # Cancellation commits internally; reacquire the parent lock before returning linked data.
        allowed = await authorize_agent_run_access(
            session, run_id, owner_id, auth_session_hash,
            require_original_session=True, lock_conversation=True,
        )
        if not allowed:
            raise HTTPException(status_code=404, detail="Agent run not found")
    return result


__all__ = [
    "APPROVAL_PROMPT_VERSION", "APPROVAL_WORKFLOW_TOOLS", "APPROVAL_WORKFLOW_VERSION", "CHECKPOINT_SCHEMA_VERSION",
    "PROMPT_VERSION", "WORKFLOW_TOOLS", "WORKFLOW_VERSION",
    "SPECIALIST_CHECKPOINT_SCHEMA_VERSION", "SPECIALIST_PROMPT_VERSION", "SPECIALIST_WORKFLOW_VERSION",
    "BrowserRunAuthorization", "reserve_browser_run_budget_in_uow", "revalidate_browser_run_authority",
    "create_profile_run_in_uow", "list_runs", "create_run", "get_run", "get_run_for_owner",
    "get_approval", "list_conversation_approvals", "request_cancel", "request_cancel_for_owner",
    "purge_conversation_actions",
    "purge_agent_runs",
]
