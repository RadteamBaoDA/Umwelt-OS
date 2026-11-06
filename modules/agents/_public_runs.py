"""Agent run creation, listing, owner lookup and read-model projection helpers."""

import hashlib
import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.errors import (
    ConflictError,
    NotFoundError,
    UnavailableError,
    ValidationFailedError,
)
from core.ids import uuid7
from core.pagination import decode_cursor, encode_cursor
from core.realtime import commit_with_replay
from core.tools import ToolRegistry, ToolRisk
from core.tools.schemas import ToolExecutionPrincipal, ToolOutputFence
from core.workspaces.schemas import Scope
from modules.agents._public_types import (
    APPROVAL_PROMPT_VERSION,
    APPROVAL_WORKFLOW_TOOLS,
    APPROVAL_WORKFLOW_VERSION,
    CHECKPOINT_SCHEMA_VERSION,
    SPECIALIST_CHECKPOINT_SCHEMA_VERSION,
    SPECIALIST_PROMPT_VERSION,
    SPECIALIST_WORKFLOW_VERSION,
)
from modules.agents.access import actor, admit, read_workspace_modules, run_epoch
from modules.agents.models import (
    AgentProfileRevision,
    AgentRun,
)
from modules.agents.schemas import (
    AgentRunPage,
    AgentRunRead,
    AgentRunStart,
    ProfileRunStart,
)
from modules.agents.specialists import resolve_profile_snapshot
from modules.tools.public import (
    revalidate_native_output_fences,
    revalidate_native_output_fences_many,
)


def _read(row: AgentRun) -> AgentRunRead:
    """Project private persisted fields into the bounded owner response."""
    return AgentRunRead(
        id=row.id,
        agent_id=row.agent_id,
        status=row.status,
        answer=None if row.evidence_revoked else row.answer,
        error_code=row.error_code,
        steps=row.steps,
        tool_calls=row.tool_calls,
        active_seconds=row.active_seconds,
        token_usage=row.token_usage,
        token_budget=row.token_budget,
        token_budget_available=False,
        token_usage_unknown=row.token_usage_unknown,
        activities=row.activities,
        profile_id=(str(row.profile_snapshot.get("id")) if row.profile_snapshot else None),
        profile_revision_hash=row.profile_revision_hash,
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
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
        actor_id=f"owner:{row.owner_id}",
        scope=epoch[0],
        is_owner=True,
        allowed_tools=frozenset(row.allowed_tools),
        source_ids=source_ids,
        owner_all_sources=owner_all_sources,
        destinations=frozenset(),
        capabilities=frozenset(capabilities),
    )


async def create_profile_run_in_uow(
    session: AsyncSession,
    auth_session_hash: str,
    profile_id: str,
    request: ProfileRunStart,
    registry: ToolRegistry,
    config: Any,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
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
    fence = await admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
    )
    owner_id = actor(scope)
    if request.token_budget is not None:
        raise ValidationFailedError("token_budget_unavailable")
    request_body = {
        "profile_id": profile_id,
        "prompt": request.prompt,
        "expected_profile_revision": request.expected_profile_revision,
        "conversation_id": str(request.conversation_id),
        "token_budget": request.token_budget,
    }
    request_hash = hashlib.sha256(
        json.dumps(
            request_body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    lock_material = (
        f"{scope.workspace_id}:{owner_id}:{auth_session_hash}:{request.client_request_id}".encode()
    )
    lock_key = int.from_bytes(hashlib.sha256(lock_material).digest()[:8], "big", signed=True)
    # Serialize the absent-row case as well as ordinary reads so parallel retries cannot create duplicate work.
    await session.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key})
    existing = await session.scalar(
        select(AgentRun).where(
            AgentRun.workspace_id == scope.workspace_id,
            AgentRun.owner_id == owner_id,
            AgentRun.auth_session_hash == auth_session_hash,
            AgentRun.client_request_id == request.client_request_id,
        )
    )
    if existing is not None:
        if existing.request_hash != request_hash:
            raise ConflictError("Client request ID was already used for another run")
        from modules.chat.public import has_live_agent_run_link

        if not await has_live_agent_run_link(session, existing.id, owner_id, auth_session_hash):
            raise NotFoundError("Agent run not found")
        retry_factory = async_sessionmaker(session.bind) if session.bind is not None else None
        return await _read_current_result(
            existing,
            retry_factory,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    snapshot, snapshot_hash = await resolve_profile_snapshot(
        session,
        profile_id,
        request.expected_profile_revision,
        registry,
        config,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    revision = snapshot["revision"]
    if revision:
        revision_row = await session.scalar(
            select(AgentProfileRevision).where(
                AgentProfileRevision.workspace_id == scope.workspace_id,
                AgentProfileRevision.profile_id == profile_id,
                AgentProfileRevision.revision == revision,
            )
        )
        if revision_row is None:
            raise ConflictError("Agent profile revision is unavailable")
        if revision_row.snapshot_hash != snapshot_hash:
            raise ConflictError("Agent profile revision no longer matches its stored snapshot")
    allowed = [item["name"] for item in snapshot["allowed_tools"]]
    contracts = {
        item["name"]: {"version": item["version"], "fingerprint": item["fingerprint"]}
        for item in snapshot["allowed_tools"]
    }
    profile_id_value = snapshot["id"]
    run = AgentRun(
        id=uuid7(),
        workspace_id=scope.workspace_id,
        owner_id=owner_id,
        membership_revision=fence.membership_revision,
        configuration_revision=fence.configuration_revision,
        auth_session_hash=auth_session_hash,
        agent_id=profile_id_value,
        workflow_version=SPECIALIST_WORKFLOW_VERSION,
        prompt_version=SPECIALIST_PROMPT_VERSION,
        checkpoint_schema_version=SPECIALIST_CHECKPOINT_SCHEMA_VERSION,
        checkpoint_thread_id=str(uuid7()),
        prompt=request.prompt,
        profile_snapshot=snapshot,
        profile_revision_hash=snapshot_hash,
        client_request_id=request.client_request_id,
        request_hash=request_hash,
        allowed_tools=allowed,
        tool_contracts=contracts,
        chat_link_required=True,
        status="queued",
        dispatch_generation=1,
        activities=[
            {"kind": "status", "status": "queued", "created_at": datetime.now(UTC).isoformat()}
        ],
    )
    session.add(run)
    from modules.chat.public import link_agent_run

    await link_agent_run(session, request.conversation_id, run.id, owner_id, auth_session_hash)
    await session.flush()
    return _read(run)


def _run_history_statement(
    scope: Scope,
    owner_id: Any,
    profile_id: str | None,
    conversation_id: UUID | None,
    scan_anchor: tuple[datetime, UUID] | None,
) -> Select[AgentRun]:
    """Build the owner-scoped run-history candidate query, narrowed by profile, conversation link and scan anchor."""
    statement = select(AgentRun).where(
        AgentRun.workspace_id == scope.workspace_id,
        AgentRun.owner_id == owner_id,
    )
    if profile_id is not None:
        statement = statement.where(AgentRun.agent_id == profile_id)
    if conversation_id is not None:
        # Unlinked runs are never visible in a conversation view; filtering in SQL avoids
        # loading whole rows (prompt/answer/activities) only to drop them in Python.
        statement = statement.where(AgentRun.chat_link_required.is_(True))
    if scan_anchor is not None:
        statement = statement.where(tuple_(AgentRun.created_at, AgentRun.id) < scan_anchor)
    return statement


async def _live_linked_run_ids(
    session: AsyncSession,
    batch: list[AgentRun],
    owner_id: Any,
    auth_session_hash: str | None,
    conversation_id: UUID | None,
) -> Any:
    """Return the ids of Chat-linked runs in ``batch`` that Chat still reports live for this session (empty without a session hash)."""
    from modules.chat.public import filter_live_agent_run_ids

    linked_ids = [row.id for row in batch if row.chat_link_required]
    if linked_ids and auth_session_hash:
        return await filter_live_agent_run_ids(
            session,
            linked_ids,
            owner_id,
            auth_session_hash or "",
            conversation_id=conversation_id,
        )
    return frozenset()


async def list_runs(
    session: AsyncSession,
    *,
    profile_id: str | None = None,
    conversation_id: UUID | None = None,
    auth_session_hash: str | None = None,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    limit: int = 25,
    cursor: str | None = None,
    scope: Scope,
    multi_workspace_enabled: bool,
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
        raise ValidationFailedError("Run history page size is outside its supported bound")
    from modules.memory.public import bind_page_cursor, page_cursor_binding, unbind_page_cursor

    binding = page_cursor_binding(
        scope,
        kind="agent_runs",
        profile_id=profile_id,
        conversation_id=str(conversation_id) if conversation_id else None,
    )
    anchor = decode_cursor(unbind_page_cursor(cursor, binding)) if cursor else None
    page: list[AgentRun] = []
    scanned = 0
    has_more = False
    scan_anchor = anchor
    while scanned < 100 and len(page) <= limit:
        statement = _run_history_statement(
            scope, owner_id, profile_id, conversation_id, scan_anchor
        )
        batch = list(
            (
                await session.scalars(
                    statement.order_by(
                        AgentRun.created_at.desc(),
                        AgentRun.id.desc(),
                    ).limit(min(25, 100 - scanned))
                )
            ).all()
        )
        if not batch:
            break
        live_ids = await _live_linked_run_ids(
            session, batch, owner_id, auth_session_hash, conversation_id
        )
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
        next_cursor = bind_page_cursor(
            encode_cursor(page[limit - 1].created_at, page[limit - 1].id), binding
        )
        page = page[:limit]
    elif scanned >= 100:
        next_cursor = (
            bind_page_cursor(encode_cursor(*scan_anchor), binding) if scan_anchor else None
        )
    else:
        next_cursor = None
    return AgentRunPage(
        items=await _read_current_results(
            page, session_factory, multi_workspace_enabled=multi_workspace_enabled
        ),
        next_cursor=next_cursor,
    )


async def create_run(
    session: AsyncSession,
    auth_session_hash: str,
    request: AgentRunStart,
    registry: ToolRegistry,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
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
    fence = await admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
    )
    owner_id = actor(scope)
    modules = await read_workspace_modules(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    definitions = {
        item.name: item
        for item in registry.list_tools(allowed_tools=APPROVAL_WORKFLOW_TOOLS, modules=modules)
        if not (registry.hides_tool and registry.hides_tool(item.name, scope.workspace_id))
        and (
            item.risk == ToolRisk.READ_ONLY
            and not item.confirmation_required
            or item.name == "webhook.send"
            and item.risk == ToolRisk.EXTERNAL_WRITE
            and item.confirmation_required
        )
    }
    allowed = sorted(definitions)
    if not allowed:
        raise UnavailableError("Agent tools are unavailable")
    run = AgentRun(
        id=uuid7(),
        workspace_id=scope.workspace_id,
        owner_id=owner_id,
        membership_revision=fence.membership_revision,
        configuration_revision=fence.configuration_revision,
        auth_session_hash=auth_session_hash,
        agent_id="assistant",
        workflow_version=APPROVAL_WORKFLOW_VERSION,
        prompt_version=APPROVAL_PROMPT_VERSION,
        checkpoint_schema_version=CHECKPOINT_SCHEMA_VERSION,
        checkpoint_thread_id=str(uuid7()),
        prompt=request.prompt,
        allowed_tools=allowed,
        tool_contracts={
            name: {"version": item.version, "fingerprint": item.schema_fingerprint}
            for name, item in definitions.items()
        },
        chat_link_required=request.conversation_id is not None,
        status="queued",
        dispatch_generation=1,
        token_budget=request.token_budget,
        activities=[
            {"kind": "status", "status": "queued", "created_at": datetime.now(UTC).isoformat()}
        ],
    )
    session.add(run)
    if request.conversation_id is not None:
        from modules.chat.public import link_agent_run

        await link_agent_run(session, request.conversation_id, run.id, owner_id, auth_session_hash)
    await commit_with_replay(
        session,
        (),
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )
    await session.refresh(run)
    return _read(run)


async def get_run(
    session: AsyncSession,
    run_id: UUID,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> AgentRunRead:
    """Return owner-visible run state and suppress answers with stale source evidence."""
    await admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    row = await session.scalar(
        select(AgentRun).where(
            AgentRun.id == run_id,
            AgentRun.workspace_id == scope.workspace_id,
            AgentRun.owner_id == actor(scope),
        )
    )
    if row is None:
        raise NotFoundError("Agent run not found")
    return await _read_current_result(
        row, session_factory, multi_workspace_enabled=multi_workspace_enabled
    )


async def get_run_for_owner(
    session: AsyncSession,
    run_id: UUID,
    auth_session_hash: str,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> AgentRunRead:
    """Read a run only after Chat confirms its linked conversation is retained for this owner.

    The frozen ``get_run`` API remains the result projector. Linked runs use Chat's lifecycle
    contract; persistent links permit the current owner, while ephemeral links remain session-bound.
    Unlinked assistant runs are still readable by the authenticated owner.
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
    if row is None:
        raise NotFoundError("Agent run not found")
    if row.chat_link_required:
        from modules.chat.public import authorize_agent_run_access

        allowed = await authorize_agent_run_access(
            session,
            run_id,
            owner_id,
            auth_session_hash,
            lock_conversation=True,
        )
        if not allowed:
            raise NotFoundError("Agent run not found")
    return await get_run(
        session,
        run_id,
        session_factory,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )


async def _read_current_results(
    rows: list[AgentRun],
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] | None,
    *,
    multi_workspace_enabled: bool,
) -> list[AgentRunRead]:
    """Project a page of results with one batched evidence-fence check instead of one per row.

    Produces exactly what ``_read_current_result`` yields per row: a row with a malformed or stale
    fence (or no usable session factory) keeps its status but has its answer suppressed.
    """
    results = [_read(row) for row in rows]
    pending: list[int] = []
    items: list[tuple[object, ToolExecutionPrincipal | None]] = []
    for index, (row, result) in enumerate(zip(rows, results, strict=True)):
        if not row.source_fences or result.answer is None:
            continue
        if session_factory is None:
            results[index] = result.model_copy(update={"answer": None})
            continue
        principal = _result_principal(row)
        try:
            sink: dict[str, Any] | None = _restore_fences(row.source_fences)
        except (TypeError, ValueError, KeyError):
            sink = None
        if sink is None or principal is None:
            results[index] = result.model_copy(update={"answer": None})
            continue
        pending.append(index)
        items.append((sink, principal))
    if pending and session_factory is not None:
        verdicts = await revalidate_native_output_fences_many(
            session_factory,
            items,
            destination_kind="remote",
            multi_workspace_enabled=multi_workspace_enabled,
        )
        for index, current in zip(pending, verdicts, strict=True):
            if not current:
                results[index] = results[index].model_copy(update={"answer": None})
    return results


async def _read_current_result(
    row: AgentRun,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] | None,
    *,
    multi_workspace_enabled: bool,
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
        current = bool(
            sink is not None
            and principal
            and await revalidate_native_output_fences(
                session_factory,
                sink,
                principal,
                destination_kind="remote",
                multi_workspace_enabled=multi_workspace_enabled,
            )
        )
        if not current:
            return result.model_copy(update={"answer": None})
    elif row.source_fences and result.answer is not None:
        return result.model_copy(update={"answer": None})
    return result


def _restore_fences(value: dict[str, Any]) -> dict[str, Any]:
    """Rebuild strict UUID-keyed source evidence identities from JSONB before authorization checks."""
    generations = value.get("source_generations")
    records = value.get("records")
    if (
        not isinstance(generations, dict)
        or len(generations) > 100
        or not isinstance(records, list)
        or len(records) > 100
    ):
        raise ValueError("Invalid source evidence fence")
    restored: dict[UUID, int] = {}
    for source_id, generation in generations.items():
        if not isinstance(source_id, str) or type(generation) is not int or generation < 1:
            raise ValueError("Invalid source generation")
        restored[UUID(source_id)] = generation
    restored_records = []
    for record in records:
        if not isinstance(record, dict) or set(record) != {
            "document_id",
            "document_version_id",
            "source_id",
            "source_generation",
            "chunk_id",
        }:
            raise ValueError("Invalid result fence")
        restored_records.append(
            ToolOutputFence(
                document_id=UUID(record["document_id"]),
                document_version_id=UUID(record["document_version_id"]),
                source_id=UUID(record["source_id"]),
                source_generation=record["source_generation"],
                chunk_id=UUID(record["chunk_id"]) if record["chunk_id"] is not None else None,
            )
        )
    return {
        "source_generations": restored,
        "records": restored_records,
    }
