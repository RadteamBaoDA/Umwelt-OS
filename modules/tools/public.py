"""Public registry contracts and native output-fence revalidation boundary."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from core.tools import (
    ToolDefinition,
    ToolDestination,
    ToolExecutionPrincipal,
    ToolRegistry,
    ToolResult,
    ToolRisk,
)
from core.tools.schemas import ToolOutputFence
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope, WorkspaceContext
from modules.sources.schemas import SourceFence
from modules.tools.mcp_admission import McpAdmission, McpInboundLease
from modules.tools.mcp_collection import McpCollectionRead, read_collection_capability
from modules.tools.mcp_repository import McpConflict, McpNotFound, McpUnavailable
from modules.tools.mcp_runtime import McpRuntime, derive_mcp_public_endpoint
from modules.tools.mcp_schemas import (
    CapabilityDescriptor,
    CapabilityRead,
    ConnectionDraft,
    ConnectionRead,
    ConnectionSave,
    CredentialUpdate,
    DiscoveryPersist,
    DiscoveryRead,
    ExecutionFence,
    GrantChoice,
    GrantRead,
    GrantSelection,
    InboundBinding,
    InboundClientCreate,
    InboundClientIssued,
    InboundClientRead,
    InboundPrincipal,
    McpRisk,
    McpTransport,
)


async def unresolved_backup_effects(session: AsyncSession) -> dict[str, int]:
    """Project browser owner-journal jobs explicitly marked as uncertain after dispatch.

    Queued jobs are unsent durable work; running jobs remain covered by activity drain. Only
    the browser owner’s ``uncertain`` terminal state means the external read outcome is unknown.
    """
    from modules.tools.models import BrowserReadJob

    count = int(await session.scalar(select(func.count()).select_from(BrowserReadJob).where(
        BrowserReadJob.status == "uncertain",
    )) or 0)
    return {"browser_job_uncertain": count} if count else {}
from modules.tools.mcp_server import McpServerBundle, create_inbound_mcp_bundle


async def revalidate_native_output_fences(
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    sink: object,
    principal: ToolExecutionPrincipal,
    *,
    destination_kind: str,
    multi_workspace_enabled: bool,
) -> bool:
    """Recheck bounded server-captured source and result identities before native output delivery.

    The caller supplies only server-composed context. Current source projections and owner module
    contracts are queried in short sessions; malformed or stale state denies the complete result.
    No transaction or row lock is kept across network transmission.
    """
    if type(multi_workspace_enabled) is not bool or not isinstance(principal, ToolExecutionPrincipal):
        return False
    try:
        destination = ToolDestination(destination_kind)
    except (TypeError, ValueError):
        return False
    if not isinstance(sink, dict) or set(sink) != {"records", "source_generations"}:
        return False
    records = sink.get("records")
    captured_generations = sink.get("source_generations")
    if (
        not isinstance(records, list) or len(records) > 100
        or not isinstance(captured_generations, dict) or len(captured_generations) > 100
    ):
        return False
    source_generations: dict[UUID, int] = {}
    for source_id, generation in captured_generations.items():
        if not isinstance(source_id, UUID) or type(generation) is not int or generation < 1:
            return False
        source_generations[source_id] = generation
    try:
        principal_source_ids = frozenset(UUID(value) for value in principal.source_ids)
    except (TypeError, ValueError, AttributeError):
        return False
    if (
        (principal.owner_all_sources and not principal.is_owner)
        or (not principal.is_owner and not principal_source_ids)
        or (not principal.owner_all_sources and any(
            source_id not in principal_source_ids for source_id in source_generations
        ))
    ):
        return False

    search_fences: list[ToolOutputFence] = []
    document_fences: list[ToolOutputFence] = []
    seen: set[tuple[UUID, UUID | None]] = set()
    for fence in records:
        if (
            not isinstance(fence, ToolOutputFence)
            or not isinstance(fence.document_id, UUID)
            or not isinstance(fence.document_version_id, UUID)
            or not isinstance(fence.source_id, UUID)
            or type(fence.source_generation) is not int or fence.source_generation < 1
            or (fence.chunk_id is not None and not isinstance(fence.chunk_id, UUID))
            or source_generations.get(fence.source_id) != fence.source_generation
            or (not principal.owner_all_sources and fence.source_id not in principal_source_ids)
        ):
            return False
        identity = (fence.document_id, fence.chunk_id)
        if identity in seen:
            return False
        seen.add(identity)
        (search_fences if fence.chunk_id is not None else document_fences).append(fence)

    try:
        from modules.sources import public as sources

        async with session_factory() as session:
            current_sources = await sources.list_tool_sources(
                session, limit=100, cursor=None,
                source_ids=frozenset(source_generations), owner_all=False,
                destination=destination, scope=principal.scope, multi_workspace_enabled=multi_workspace_enabled,
            )
        current_generation_map = {item.id: item.generation for item in current_sources.items}
        if current_sources.next_cursor is not None or current_generation_map != source_generations:
            return False

        if search_fences:
            from modules.search import public as search

            async with session_factory() as session:
                valid = await search.revalidate_tool_search_fences(
                    session, search_fences, source_ids=principal_source_ids,
                    owner_all=principal.owner_all_sources, destination=destination,
                    scope=principal.scope, multi_workspace_enabled=multi_workspace_enabled,
                )
            if not valid:
                return False
        if document_fences:
            from modules.knowledge.documents import public as documents

            async with session_factory() as session:
                valid = await documents.revalidate_tool_document_fences(
                    session, document_fences, source_ids=principal_source_ids,
                    owner_all=principal.owner_all_sources, destination=destination,
                    scope=principal.scope, multi_workspace_enabled=multi_workspace_enabled,
                )
            if not valid:
                return False
    except (HTTPException, SQLAlchemyError, ValueError, OSError, TimeoutError):
        # Admission/database/validation failures suppress output; TypeError/AttributeError are bugs and propagate.
        return False
    return True


# Non-null placeholders written over purged job links (the columns cannot be NULL).
PURGED_SESSION_HASH = "0" * 64
PURGED_CONVERSATION_ID = UUID(int=0)


async def _assert_source_browser_cleanup_in_uow(
    session: AsyncSession, source_id: UUID, *, scope: Scope,
    multi_workspace_enabled: bool, access_fence: AccessFence,
    source_fence: SourceFence,
) -> int:
    """Freshly prove an exact cleanup anchor through nonlocking owner-public reads.

    The caller already holds actual admission/Source locks in this transaction; detached
    fences do not establish those locks. Accept only a real owner/default-workspace subject
    and the explicit configured flag. Bound workers keep their exact Source and generation;
    this helper never derives a successor scope or rescues stale access with current epochs.
    All access fields and Source identity/workspace/status/generation/local_only must match.
    Active, paused and archived anchors authorize destruction only, not browser execution.
    Returns the admitted actor ID for private SQL predicates; malformed inputs raise TypeError,
    members raise 403, mismatched/unavailable anchors raise 409 and admission errors propagate.
    Suppressed autoflush prevents proof reads from causing incidental DML. No locks, mutation,
    commit, session registry, authority token, foreign ORM or external I/O are involved.
    """
    from core.workspaces import public as workspaces
    from modules.sources import public as sources

    if not isinstance(scope, (WorkspaceContext, InternalJobScope)):
        raise TypeError("An explicit Source browser cleanup scope is required")
    if isinstance(scope, WorkspaceContext) and scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")
    if (not isinstance(source_id, UUID) or type(multi_workspace_enabled) is not bool
            or not isinstance(access_fence, AccessFence) or not isinstance(source_fence, SourceFence)):
        raise TypeError("Source browser cleanup requires typed fences and an explicit boolean feature flag")
    actor_user_id = scope.actor_user_id if isinstance(scope, InternalJobScope) else scope.user_id
    if (access_fence.workspace_id != scope.workspace_id or access_fence.user_id != actor_user_id
            or access_fence.membership_revision != scope.membership_revision
            or source_fence.id != source_id or source_fence.workspace_id != scope.workspace_id
            or (isinstance(scope, InternalJobScope) and scope.source_id is not None and (
                scope.source_id != source_id or scope.source_generation != source_fence.generation
            ))):
        raise HTTPException(status_code=409, detail="Source browser cleanup subject changed")
    with session.no_autoflush:
        current_access = await workspaces.read_access_fence(
            session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        current_source = await sources.get_source_fence(
            session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
    if (current_access != access_fence or current_source is None
            or current_source != source_fence or current_source.status not in {"active", "paused", "archived"}):
        raise HTTPException(status_code=409, detail="Source browser cleanup anchor is unavailable or stale")
    return actor_user_id


def _source_browser_job_ids(
    source_id: UUID, *, workspace_id: UUID, actor_user_id: int,
) -> Select[tuple[UUID]]:
    """Build the complete identity-only relation for an already-proven Source cleanup.

    Private callers supply the actor/workspace from fresh owner admission, never selector
    unions or guessed identities. Every historical generation/epoch/status and scrubbed link
    is included; there is no retention cutoff, live-session filter or LIMIT. Keeping this
    one-column relation in SQL avoids materializing history or private page payload lists.
    It performs no query, authorization, locking or mutation on its own.
    """
    from modules.tools.models import BrowserReadJob

    return select(BrowserReadJob.id).where(
        BrowserReadJob.workspace_id == workspace_id,
        BrowserReadJob.owner_id == actor_user_id,
        BrowserReadJob.source_id == source_id,
    )


async def _lock_source_browser_rows_in_uow(
    session: AsyncSession, source_id: UUID, *, workspace_id: UUID, actor_user_id: int,
) -> None:
    """Lock the entire identity-scoped job set by UUID, then its pages by evidence PK UUID.

    Caller has freshly proven the held Source/workspace subject and prepared all earlier
    Connector/collector rows. Their serialization prevents supported writers from growing
    this set; this function cannot manufacture that proof or acquire an earlier parent.
    Both queries reread database identities and drain completely before the next phase.
    Streams carry only IDs, not URL/raw/text payloads, and have no history truncation or
    SKIP LOCKED. Pages are selected through proven parent jobs, never a page workspace field.
    Autoflush is suppressed: preparation performs no DML, commit, network or authority return.
    Database/lock failures propagate so the caller aborts the complete cleanup transaction.
    """
    from modules.tools.models import BrowserPageEvidence, BrowserReadJob

    job_ids = _source_browser_job_ids(source_id, workspace_id=workspace_id, actor_user_id=actor_user_id)
    with session.no_autoflush:
        jobs = await session.stream_scalars(
            job_ids.order_by(BrowserReadJob.id).with_for_update().execution_options(populate_existing=True)
        )
        try:
            async for _job_id in jobs:
                pass
        finally:
            await jobs.close()
        # Drain every earlier job lock before acquiring the first evidence row lock.
        pages = await session.stream_scalars(
            select(BrowserPageEvidence.id).where(BrowserPageEvidence.job_id.in_(job_ids))
            .order_by(BrowserPageEvidence.id).with_for_update().execution_options(populate_existing=True)
        )
        try:
            async for _page_id in pages:
                pass
        finally:
            await pages.close()


async def _purge_source_browser_rows_in_uow(
    session: AsyncSession, source_id: UUID, *, workspace_id: UUID, actor_user_id: int,
) -> int:
    """Erase all prepared Source page payloads and scrub private links, retaining job tombstones.

    Caller keeps the same prepared transaction and all earlier locks, and freshly proves
    its exact current Source/access anchor before entry. Identity-only SQL rereads that same
    complete historical set without any SELECT FOR UPDATE or earlier parent acquisition.
    Delete child URL/raw/text rows; only queued/running/uncertain jobs become cancel_requested.
    Every job loses session/conversation links, including terminal and already-cancelled jobs.
    Preserve IDs, operation/run/source identity, original epochs/generation, service identity,
    digests, counters and terminal state; present cleanup authority never upgrades old jobs.
    Flush only and return the deleted page count. Errors abort the caller's transaction; no
    provider cancellation, commit or I/O occurs. Repeated prepared cleanup remains complete.
    """
    from modules.tools.models import BrowserPageEvidence, BrowserReadJob

    job_ids = _source_browser_job_ids(source_id, workspace_id=workspace_id, actor_user_id=actor_user_id)
    result = await session.execute(
        delete(BrowserPageEvidence).where(BrowserPageEvidence.job_id.in_(job_ids))
    )
    await session.execute(
        update(BrowserReadJob).where(
            BrowserReadJob.id.in_(job_ids), BrowserReadJob.status.in_(("queued", "running", "uncertain")),
        ).values(cancel_requested=True, status="cancel_requested")
    )
    await session.execute(
        update(BrowserReadJob).where(BrowserReadJob.id.in_(job_ids)).values(
            auth_session_hash=PURGED_SESSION_HASH, conversation_id=PURGED_CONVERSATION_ID,
        )
    )
    await session.flush()
    return int(cast("CursorResult[Any]", result).rowcount or 0)


async def lock_source_browser_results_in_uow(
    session: AsyncSession, source_id: UUID, *, scope: Scope,
    multi_workspace_enabled: bool, access_fence: AccessFence,
    source_fence: SourceFence,
) -> None:
    """Prepare complete historical Source browser cleanup under caller-held lifecycle locks.

    Before entry the caller holds account/session where applicable, workspace/membership,
    exact Source, optional/required Connector provisioning and sorted managed slots/browser
    grant/native prerequisites, then Ingestion collector tokens. Call before GitHub grant,
    ingestion state/receipt, outbox, hints or capacity. Fresh nonlocking owner reads must match
    the original AccessFence and every supplied current SourceFence field with the actual
    feature flag. Only real owner/default-workspace subjects and exact active/paused/archived
    anchors are eligible; bound InternalJobScope keeps its exact Source and generation.
    Missing/stale/foreign anchors fail, including when there are no browser jobs.

    Lock all workspace+actor+Source jobs in UUID order across every historical generation,
    epoch, status and link state, then all child pages in evidence PK UUID order. No LIMIT,
    mutation, autoflush, earlier lock acquisition, commit or I/O; returns no authority object.
    Keep this transaction and locks through purge. Visibility preparation uses active G or
    paused G only in its Source owner; ordinary cleanup can prepare an exact archived anchor.
    Source may later perform its approved active G->paused G+1 transition and supply only
    its constrained successor scope/fence to purge; an unchanged paused anchor stays exact.
    Supported insert/publication writers must share the held earlier Source serialization.
    That remaining writer integration is a prerequisite for combined lifecycle acceptance.
    """
    actor_user_id = await _assert_source_browser_cleanup_in_uow(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    await _lock_source_browser_rows_in_uow(
        session, source_id, workspace_id=scope.workspace_id, actor_user_id=actor_user_id,
    )


async def purge_source_browser_results_in_uow(
    session: AsyncSession, source_id: UUID, *, scope: Scope,
    multi_workspace_enabled: bool, access_fence: AccessFence,
    source_fence: SourceFence,
) -> int:
    """Apply Source-only browser erasure after preparation in the same retained transaction.

    Caller retains actual admission/Source/Connector/collector and complete job-then-page
    locks from lock_source_browser_results_in_uow; no detached fence or function name proves
    held locks, and this mutation never reacquires them. Fresh nonlocking owner-public proof
    must match original access and the now-current complete active/paused/archived Source
    anchor using the actual flag. Only Source's verified lifecycle transition may supply a
    successor cleanup subject; stale bound workers are never readmitted against later G.
    Missing/stale/foreign proof fails even for an empty set, while a proven empty set returns 0.

    Delete all historical owned Source page bytes/text/URLs; scrub session/conversation
    links on every matched job and request cancellation only for queued/running/uncertain.
    Keep terminal statuses, tombstones, IDs, original epochs/generation, digests, counters and
    service identity. Old-job authority is never refreshed. Repeated paused/archived cleanup
    repeats erasure/link scrubbing without inventing a new Source transition. Flush only,
    return deleted page count, and leave commit/provider cancellation to the caller. This
    mandatory-scoped path never delegates to legacy mixed-selector purge. Run/conversation
    cleanup, browser writer/retention conversion and HTTP publication remain separate gates.
    """
    actor_user_id = await _assert_source_browser_cleanup_in_uow(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    return await _purge_source_browser_rows_in_uow(
        session, source_id, workspace_id=scope.workspace_id, actor_user_id=actor_user_id,
    )


async def purge_browser_results_in_uow(
    session: AsyncSession,
    *,
    run_ids: tuple[UUID, ...] | list[UUID] = (),
    source_ids: tuple[UUID, ...] | list[UUID] = (),
    conversation_ids: tuple[UUID, ...] | list[UUID] = (),
    scope: Scope,
    multi_workspace_enabled: bool,
) -> int:
    """Erase private page bytes and text inside the owning run/source privacy transaction.

    Page rows (bytes, text and URLs) are deleted. Job IDs, result digests, counters and
    terminal statuses remain as tombstones; only non-terminal jobs are flagged for
    cancellation, and the session/Chat links are blanked. Callers own lock ordering and
    commit; this contract acquires no Sources, Agents, or Chat ORM objects.
    """
    from sqlalchemy import or_

    from modules.tools.models import BrowserPageEvidence, BrowserReadJob

    if not isinstance(scope, (WorkspaceContext, InternalJobScope)):
        raise TypeError("An explicit workspace scope is required")
    if isinstance(scope, WorkspaceContext) and scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")
    if type(multi_workspace_enabled) is not bool:
        raise TypeError("The configured multi-workspace feature flag must be a boolean")
    selectors = []
    if run_ids:
        selectors.append(BrowserReadJob.run_id.in_(tuple(run_ids)))
    if source_ids:
        selectors.append(BrowserReadJob.source_id.in_(tuple(source_ids)))
    if conversation_ids:
        selectors.append(BrowserReadJob.conversation_id.in_(tuple(conversation_ids)))
    if not selectors:
        return 0
    job_ids = tuple((await session.scalars(select(BrowserReadJob.id).where(
        BrowserReadJob.workspace_id == scope.workspace_id, or_(*selectors),
    ))).all())
    if not job_ids:
        return 0
    from sqlalchemy import delete

    result = await session.execute(
        delete(BrowserPageEvidence).where(BrowserPageEvidence.job_id.in_(job_ids))
    )
    live = ("queued", "running", "uncertain")
    await session.execute(
        update(BrowserReadJob).where(
            BrowserReadJob.id.in_(job_ids), BrowserReadJob.status.in_(live),
        ).values(cancel_requested=True, status="cancel_requested")
    )
    # Unlink the owner session and conversation from every matched job.
    await session.execute(
        update(BrowserReadJob).where(BrowserReadJob.id.in_(job_ids)).values(
            auth_session_hash=PURGED_SESSION_HASH, conversation_id=PURGED_CONVERSATION_ID,
        )
    )
    await session.flush()
    return int(cast("CursorResult[Any]", result).rowcount or 0)


async def purge_expired_browser_evidence(session: AsyncSession, *, limit: int = 200) -> int:
    """Delete bounded expired terminal payloads only while evidence exists; retain job tombstones.

    Filtering to jobs with child evidence prevents already-cleaned tombstones from consuming every
    later batch. The evidence unique index begins with ``job_id`` and the job retention index
    orders the eligibility scan by terminal status and expiry.
    """
    from sqlalchemy import delete, exists

    from modules.tools.models import BrowserPageEvidence, BrowserReadJob

    if not 1 <= limit <= 1000:
        raise ValueError("Browser evidence cleanup limit must be between 1 and 1000")
    now = datetime.now(UTC)
    terminal = ("succeeded", "cancelled", "failed", "expired")
    ids = tuple((await session.scalars(select(BrowserReadJob.id).where(
        BrowserReadJob.expires_at <= now, BrowserReadJob.status.in_(terminal),
        exists(select(BrowserPageEvidence.id).where(BrowserPageEvidence.job_id == BrowserReadJob.id)),
    ).order_by(BrowserReadJob.expires_at, BrowserReadJob.id).limit(limit).with_for_update(skip_locked=True))).all())
    if not ids:
        return 0
    result = await session.execute(delete(BrowserPageEvidence).where(BrowserPageEvidence.job_id.in_(ids)))
    await session.flush()
    return int(cast("CursorResult[Any]", result).rowcount or 0)


def webhook_aliases(settings: Any) -> set[str]:
    """Return the deployment-allowlisted, enabled HTTPS webhook aliases (never URLs or CIDRs).

    Raises:
        ValueError: If the deployment WEBHOOK_PROFILES manifest is malformed.
    """
    from modules.tools.webhook import load_webhook_profiles

    return set(load_webhook_profiles(settings))


def webhook_profile_revision(settings: Any, alias: str) -> str | None:
    """Return the digest of an alias's enabled deployment profile (URL, origin, CIDRs), or None.

    Approvals bind this value so a changed destination invalidates them (like P07's
    ``destination_revision``).

    Raises:
        ValueError: If the deployment WEBHOOK_PROFILES manifest is malformed.
    """
    from modules.tools.webhook import load_webhook_profiles

    profile = load_webhook_profiles(settings).get(alias)
    return profile.revision if profile is not None else None


async def send_webhook_once(
    settings: Any, alias: str, payload: dict[str, Any], *, idempotency_key: str,
    headers: dict[str, str], before_send: Any,
) -> str:
    """Send one allowlisted webhook for a caller-owned no-replay ledger (see ``webhook.send_once``)."""
    from modules.tools.webhook import send_once

    return await send_once(
        settings, alias, payload, idempotency_key=idempotency_key, headers=headers, before_send=before_send)


__all__ = [
    "CapabilityDescriptor",
    "CapabilityRead",
    "ConnectionDraft",
    "ConnectionRead",
    "ConnectionSave",
    "CredentialUpdate",
    "DiscoveryPersist",
    "DiscoveryRead",
    "ExecutionFence",
    "GrantChoice",
    "GrantRead",
    "GrantSelection",
    "InboundBinding",
    "InboundClientCreate",
    "InboundClientIssued",
    "InboundClientRead",
    "InboundPrincipal",
    "McpAdmission",
    "McpCollectionRead",
    "McpConflict",
    "McpInboundLease",
    "McpNotFound",
    "McpRisk",
    "McpRuntime",
    "McpServerBundle",
    "McpTransport",
    "McpUnavailable",
    "ToolDefinition",
    "ToolDestination",
    "ToolExecutionPrincipal",
    "ToolOutputFence",
    "ToolRegistry",
    "ToolResult",
    "ToolRisk",
    "create_inbound_mcp_bundle",
    "derive_mcp_public_endpoint",
    "lock_source_browser_results_in_uow",
    "purge_browser_results_in_uow",
    "purge_expired_browser_evidence",
    "purge_source_browser_results_in_uow",
    "read_collection_capability",
    "revalidate_native_output_fences",
    "send_webhook_once",
    "webhook_aliases",
    "webhook_profile_revision",
]
