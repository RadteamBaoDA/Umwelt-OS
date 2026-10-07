"""Public registry contracts and native output-fence revalidation boundary."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
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
) -> bool:
    """Recheck bounded server-captured source and result identities before native output delivery.

    The caller supplies only server-composed context. Current source projections and owner module
    contracts are queried in short sessions; malformed or stale state denies the complete result.
    No transaction or row lock is kept across network transmission.
    """
    if not isinstance(principal, ToolExecutionPrincipal):
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
                destination=destination,
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
                )
            if not valid:
                return False
        if document_fences:
            from modules.knowledge.documents import public as documents

            async with session_factory() as session:
                valid = await documents.revalidate_tool_document_fences(
                    session, document_fences, source_ids=principal_source_ids,
                    owner_all=principal.owner_all_sources, destination=destination,
                )
            if not valid:
                return False
    except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
        # Database/owner validation failures suppress output instead of bypassing the fence.
        return False
    return True


# Non-null placeholders written over purged job links (the columns cannot be NULL).
PURGED_SESSION_HASH = "0" * 64
PURGED_CONVERSATION_ID = UUID(int=0)


async def purge_browser_results_in_uow(
    session: AsyncSession,
    *,
    run_ids: tuple[UUID, ...] | list[UUID] = (),
    source_ids: tuple[UUID, ...] | list[UUID] = (),
    conversation_ids: tuple[UUID, ...] | list[UUID] = (),
) -> int:
    """Erase private page bytes and text inside the owning run/source privacy transaction.

    Page rows (bytes, text and URLs) are deleted. Job IDs, result digests, counters and
    terminal statuses remain as tombstones; only non-terminal jobs are flagged for
    cancellation, and the session/Chat links are blanked. Callers own lock ordering and
    commit; this contract acquires no Sources, Agents, or Chat ORM objects.
    """
    from sqlalchemy import or_

    from modules.tools.models import BrowserPageEvidence, BrowserReadJob

    selectors = []
    if run_ids:
        selectors.append(BrowserReadJob.run_id.in_(tuple(run_ids)))
    if source_ids:
        selectors.append(BrowserReadJob.source_id.in_(tuple(source_ids)))
    if conversation_ids:
        selectors.append(BrowserReadJob.conversation_id.in_(tuple(conversation_ids)))
    if not selectors:
        return 0
    job_ids = tuple((await session.scalars(select(BrowserReadJob.id).where(or_(*selectors)))).all())
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
    "purge_browser_results_in_uow",
    "purge_expired_browser_evidence",
    "read_collection_capability",
    "revalidate_native_output_fences",
    "send_webhook_once",
    "webhook_aliases",
    "webhook_profile_revision",
]
