from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from functools import partial
from typing import Any, cast
from uuid import UUID

from arq.connections import ArqRedis
from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.errors import ConflictError, DomainError, error_status
from core.ids import uuid7
from core.job_denial import (
    PERMISSION_LOST,
    STALE_SCOPE,
    admit_retry_stale,
    denial_code,
    terminalize,
)
from core.model_gateway.client import (
    CapabilityUnsupported,
    ModelGateway,
    ModelGatewayError,
    PrivacyPolicyDenied,
)
from core.model_gateway.policy import may_send
from core.model_gateway.schemas import (
    AIExecutionConfig,
    ModelMapping,
    RequestPolicy,
)
from core.realtime import commit_with_replay, make_graph_change
from core.worker_context import resolve_owner_job_scope as _workspace_job_scope
from core.worker_context import worker_session_factory as _factory
from core.worker_cursors import STATE_KEY, read_cursor, write_cursor
from core.worker_queues import queue_for
from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope
from modules.ingestion import public as ingestion
from modules.knowledge.documents import public as documents
from modules.knowledge.entities import public as entities
from modules.knowledge.entities.extraction import (
    EXTRACTOR_VERSION,
    PROMPT_VERSION,
    ExtractedEntity,
    ExtractedRelationship,
    ExtractionOutput,
    extraction_messages,
    response_content,
    response_schema,
)
from modules.knowledge.entities.models import EntityExtractionWork
from modules.knowledge.entities.resolution import candidate_match_fingerprint, resolve_candidate
from modules.knowledge.entities.schemas import canonicalize_name
from modules.knowledge.relationships import public as relationships
from modules.settings import public as settings_public
from modules.settings.prompts import resolve_prompt
from modules.sources import public as sources

logger = logging.getLogger(__name__)
EXTRACTION_ROLE = "background"


async def _dependency_snapshot(config: AIExecutionConfig, redis: Redis, *, scope: Scope) -> tuple[str, bool]:
    """Fingerprint non-secret extraction policy; the capability flag is always True.

    Capability evidence no longer gates extraction: the gateway tries native structured output and falls back to
    prompted JSON, so work is never parked merely because no probe ran. A provider rejection still parks the item
    (``structured_unsupported``) with this fingerprint, and it is requeued only when the fingerprint changes
    (model/gateway/policy edits), so permanent failures cannot hot-loop. Excludes endpoint and credential values.
    """
    # AIExecutionConfig is returned by the settings owner. Hash only non-secret
    # policy/model/capability metadata; never persist endpoint or credential data.
    role = EXTRACTION_ROLE
    mapping = config.defaults.resolve(role)
    gateway_identity = config.gateway_identity
    privacy = config.privacy
    snapshot = {
        "revision": config.configuration_revision,
        "gateway": gateway_identity,
        "destination": config.endpoint_destination_id,
        "endpoint_denied": config.endpoint_policy_denied,
        "credential_configured": config.gateway_credential_configured,
        "mapping": (mapping.model, mapping.version) if mapping is not None else None,
        "remote_reasoning": privacy.allow_remote_reasoning,
        "reasoning_destinations": sorted(privacy.reasoning_destinations),
    }
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    return digest, True


def _policy_allows_extraction(config: AIExecutionConfig, mapping: ModelMapping | None, destination: str | None, local_only: bool) -> bool:
    """Apply destination, local-only, consent, and capability policy to extraction."""
    if mapping is None or not destination or config.endpoint_policy_denied:
        return False
    privacy = config.privacy
    policy = RequestPolicy(
        workspace_id=config.workspace_id, actor_user_id=config.actor_user_id,
        membership_revision=config.membership_revision, gateway_identity=config.gateway_identity,
        reasoning_allowed=privacy.allow_remote_reasoning,
        local_only=local_only,
        permitted_destinations=frozenset({destination}),
        reasoning_destinations=frozenset(privacy.reasoning_destinations),
        configuration_revision=config.configuration_revision,
    )
    return may_send(
        policy, EXTRACTION_ROLE, mapping, destination,
        config.gateway_credential_configured, "structured",
    )


@dataclass(frozen=True)
class _ScheduledReady:
    """Outcome of staging a ready-document event: the scheduled work ids and whether it is local-only."""

    work_id: UUID
    timeline_work_id: UUID
    local_only: bool


async def _ack_event(
    session: AsyncSession, event_uuid: UUID, scope: InternalJobScope, multi_workspace_enabled: bool,
) -> None:
    """Acknowledge the outbox event as delivered and commit (used by every stale-lineage exit)."""
    await ingestion.mark_event_delivered(
        session, event_uuid, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    await session.commit()


async def _lock_ready_lineage(
    session: AsyncSession, event_uuid: UUID, event: Any, fence: AccessFence, scope: InternalJobScope,
    multi_workspace_enabled: bool,
) -> tuple[Any, Any] | None:
    """Verify the event's lineage under Source then Document locks; returns ``(ready, source)`` or ``None``.

    Lock order: ready ref -> Source -> ready ref re-read -> Document -> ready ref re-read. Any mismatch with the
    event payload (version gone, source/generation drift, document lock refused) acknowledges the event
    (committing) and returns ``None``. On success the locks remain held for the caller's single commit.
    """
    version_id = UUID(str(event.payload["document_version_id"]))
    ready = await documents.get_ready_version_ref(
        session, version_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if ready is None or ready.document_id != UUID(str(event.payload["document_id"])):
        await _ack_event(session, event_uuid, scope, multi_workspace_enabled)
        return None
    source = await sources.lock_source(
        session, ready.source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=fence,
    )
    ready = await documents.get_ready_version_ref(
        session, version_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if (
        source is None or source.status != "active" or ready is None
        or ready.source_id != UUID(str(event.payload["source_id"]))
        or source.generation != ready.source_generation
        or ready.source_generation != int(event.payload["source_generation"])
    ):
        await _ack_event(session, event_uuid, scope, multi_workspace_enabled)
        return None
    # Keep both owners behind the source/document fence until their durable rows
    # and the single outbox acknowledgement commit together.
    if not await documents.lock_document_for_extraction(
        session, ready.document_id, ready.source_id, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=fence,
        source_fence=source, expected_raw_uri=ready.raw_uri,
        expected_mime_type=ready.mime_type,
    ):
        await _ack_event(session, event_uuid, scope, multi_workspace_enabled)
        return None
    ready = await documents.get_ready_version_ref(
        session, version_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if ready is None or ready.source_generation != source.generation:
        await _ack_event(session, event_uuid, scope, multi_workspace_enabled)
        return None
    return ready, source


async def _schedule_ready_work(
    session: AsyncSession, event_uuid: UUID, version_id: UUID, ready: Any, source: Any, scope: InternalJobScope,
    multi_workspace_enabled: bool,
) -> _ScheduledReady | None:
    """Schedule entity, timeline, temporal and GitHub-mapping work and acknowledge the event in one commit.

    A graph-disabled source schedules no model work (only the deterministic GitHub mapper runs) and returns
    ``None``. Local-only versions get their entity/timeline work parked as blocked. Runs on the caller's
    transaction under the lineage locks and commits exactly once.
    """
    from modules.connectors import public as connectors
    if not source.graph_enabled:
        # Owner switched the knowledge graph off for this Source: schedule no entity/timeline/
        # temporal work. The deterministic GitHub mapper needs no model, so it still runs.
        await connectors.map_github_version(
            session, ready, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        await _ack_event(session, event_uuid, scope, multi_workspace_enabled)
        return None
    work = await entities.schedule_extraction_work(
        session, version_id, ready.source_generation, EXTRACTOR_VERSION, PROMPT_VERSION,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if ready.local_only:
        work.status = "blocked"
        work.error_code = "local_only_source"
        work.next_attempt_at = datetime.max.replace(tzinfo=UTC)
        work.dependency_fingerprint = None
    work_id = work.id
    from modules.timeline import public as timeline
    timeline_work_id = await timeline.schedule_extraction_work(
        session, ready, "events-v1", "events-prompt-v1", scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    from modules.knowledge.temporal import public as temporal
    await temporal.schedule_version(
        session, ready, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    # Deterministic provider mapping (no model egress, so also valid for local-only
    # sources) shares this transaction and the source/document fences held above.
    await connectors.map_github_version(
        session, ready, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if ready.local_only:
        await timeline.block_local_only_extraction_work(
            session, timeline_work_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    await _ack_event(session, event_uuid, scope, multi_workspace_enabled)
    return _ScheduledReady(work_id=work_id, timeline_work_id=timeline_work_id, local_only=bool(ready.local_only))


async def _stage_ready_event(
    session: AsyncSession, event_uuid: UUID, scope: InternalJobScope, multi_workspace_enabled: bool,
) -> _ScheduledReady | None:
    """Validate the delivery row and lineage, then schedule work; ``None`` means nothing to enqueue.

    Reads the access fence before the event row so the fence is the first lock-ordered read. A delivered or
    missing event is a no-op; an unsupported type/version is marked ``failed`` and committed.
    """
    fence = await workspaces.read_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    event = await ingestion.get_event_delivery(
        session, event_uuid, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if event is None or event.status == "delivered":
        return None
    if event.type != "document.version.ready" or event.version != 1:
        await ingestion.set_event_delivery(
            session, event_uuid, "failed", scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        await session.commit()
        return None
    lineage = await _lock_ready_lineage(session, event_uuid, event, fence, scope, multi_workspace_enabled)
    if lineage is None:
        return None
    ready, source = lineage
    version_id = UUID(str(event.payload["document_version_id"]))
    return await _schedule_ready_work(session, event_uuid, version_id, ready, source, scope, multi_workspace_enabled)


async def process_document_ready(ctx: dict[str, object], event_id: str) -> None:
    """Create or schedule extraction work for a durable ready-document event.

    Resolves the retained principal scope (a denied principal rolls back and ends without retry), stages the
    work inside one transaction, and only after commit enqueues the entity and timeline jobs on the model
    queue (enqueue failures are suppressed; recovery re-enqueues with matching job ids). Local-only versions
    are never enqueued.
    """
    factory = _factory(ctx)
    event_uuid = UUID(event_id)
    multi_workspace_enabled = cast(Settings, ctx["settings"]).multi_workspace_enabled
    async with factory() as session:
        try:
            scope = await ingestion.resolve_ingestion_event_scope(
                session, event_uuid, multi_workspace_enabled=multi_workspace_enabled,
            )
        except (HTTPException, DomainError):
            # Denied retained principal: no mutation and no retry; the dispatcher quarantines the event.
            await session.rollback()
            return
        if scope is None or not await settings_public.module_is_enabled(
            session, "knowledge.entities", scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        ):
            # ponytail: unacked ready event is re-dispatched every DISPATCH_STALE_AFTER while the module is disabled;
            # upgrade = ack+requeue the events from the module-enable path.
            return  # unavailable lineage or disabled module: leave the event unacknowledged
        scheduled = await _stage_ready_event(session, event_uuid, scope, multi_workspace_enabled)
    if scheduled is None or scheduled.local_only:
        return
    # Remote model calls run on the dedicated model worker, never inline on this (main) worker; the
    # job ids match recovery's, so a lost enqueue is re-enqueued by recover_*_extraction_work.
    redis = cast(ArqRedis, ctx["redis"])
    with contextlib.suppress(Exception):
        await redis.enqueue_job(
            "process_entity_extraction_work", str(scheduled.work_id), _job_id=f"entity-extraction:{scheduled.work_id}",
            _queue_name=queue_for("process_entity_extraction_work"),
        )
    with contextlib.suppress(Exception):
        await redis.enqueue_job(
            "process_timeline_extraction_work", str(scheduled.timeline_work_id),
            _job_id=f"timeline-extraction:{scheduled.timeline_work_id}",
            _queue_name=queue_for("process_timeline_extraction_work"),
        )


_WORKSPACE_CURSOR_KEY = "bbd:entity-extraction:workspace-cursor"
_WORKSPACE_PAGE = 100


async def _recovery_workspace_ids(ctx: dict[str, object], factory: async_sessionmaker[AsyncSession]) -> list[UUID]:
    """One keyset page of workspaces with work rows or ready documents (cursor via core.worker_cursors)."""
    ctx.setdefault(STATE_KEY, {})
    keys = (_WORKSPACE_CURSOR_KEY,)
    after = await read_cursor(ctx, _WORKSPACE_CURSOR_KEY, keys)
    page: list[UUID] = []
    for lower in ((after, None) if after is not None else (None,)):
        async with factory() as session:
            query = select(EntityExtractionWork.workspace_id).distinct().order_by(EntityExtractionWork.workspace_id).limit(_WORKSPACE_PAGE)
            if lower is not None:
                query = query.where(EntityExtractionWork.workspace_id > lower)
            found = set((await session.scalars(query)).all())
            found.update(await documents.list_ready_document_workspace_ids(session, after=lower, limit=_WORKSPACE_PAGE))
            await session.rollback()
        page = sorted(found)[:_WORKSPACE_PAGE]
        if page:
            break
    await write_cursor(ctx, _WORKSPACE_CURSOR_KEY, page[-1] if page else None, keys)
    return page


@dataclass(frozen=True)
class _ReadyPage:
    """One workspace's recovery page: its scope, cursor key, ready-version refs and the next cursor."""

    scope: InternalJobScope
    cursor_key: str
    refs: list[Any]
    next_cursor: str | None


async def _scan_ready_page(
    factory: async_sessionmaker[AsyncSession], redis: ArqRedis, workspace_id: UUID, multi_workspace_enabled: bool,
) -> _ReadyPage | None:
    """Resolve the workspace owner scope and read one bounded page of ready versions (committed).

    Returns ``None`` when the owner is gone or the module disabled so recovery skips the workspace. A stored
    cursor that is oversized (>512 chars) or malformed is ignored and the scan restarts from the beginning.
    """
    async with factory() as session:
        scope = await _workspace_job_scope(
            session, workspace_id, multi_workspace_enabled=multi_workspace_enabled,
        )
        if scope is None or not await settings_public.module_is_enabled(
            session, "knowledge.entities", scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        ):
            return None
        cursor_key = f"bbd:entity-extraction:ready-cursor:{workspace_id}"
        raw_cursor = await redis.get(cursor_key)
        cursor = raw_cursor.decode() if isinstance(raw_cursor, bytes) else raw_cursor if isinstance(raw_cursor, str) else None
        if cursor is not None and len(cursor) > 512:
            cursor = None
        refs, next_cursor = await documents.list_ready_version_refs(
            session, limit=25, cursor=cursor, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        await session.commit()
    return _ReadyPage(scope=scope, cursor_key=cursor_key, refs=refs, next_cursor=next_cursor)


async def _recover_ready_ref(
    factory: async_sessionmaker[AsyncSession], scope: InternalJobScope, multi_workspace_enabled: bool, ref: Any,
) -> None:
    """Re-schedule extraction work for one ready version under the source/document fence (one commit).

    Stale refs (source gone/inactive or ready lineage drifted) commit and skip. Graph-disabled sources get no
    work. Local-only work that is not succeeded and not under a live lease is parked as blocked. The GitHub
    mapper runs in a savepoint so its failure cannot abort recovery of other sources.
    """
    async with factory() as session:
        access_fence = await workspaces.read_access_fence(
            session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        source = await sources.lock_source(
            session, ref.source_id, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            expected_access_fence=access_fence,
        )
        current = await documents.get_ready_version_ref(
            session, ref.document_version_id, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if (
            source is None or source.status != "active" or current is None
            or current.document_id != ref.document_id or current.source_id != source.id
            or current.source_generation != source.generation
            or current.source_generation != ref.source_generation
        ):
            await session.commit()
            return
        work = None if not source.graph_enabled else await entities.schedule_extraction_work(
            session, ref.document_version_id, source.generation, EXTRACTOR_VERSION,
            PROMPT_VERSION, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        if work is not None and current.local_only and work.status != "succeeded" and not (
            work.status == "running" and work.lease_expires_at is not None
            and work.lease_expires_at > datetime.now(UTC)
        ):
            work.status = "blocked"
            work.error_code = "local_only_source"
            work.next_attempt_at = datetime.max.replace(tzinfo=UTC)
            work.lease_owner = None
            work.lease_expires_at = None
            work.dependency_fingerprint = None
        from modules.connectors import public as connectors
        try:
            async with session.begin_nested():
                await connectors.map_github_version(
                    session, current, scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled,
                )
        except Exception as exc:  # noqa: BLE001  # recovery proceeds for other sources
            logger.warning(
                "github mapping recovery failed for version %s (%s)",
                ref.document_version_id, type(exc).__name__,
            )
        await session.commit()


async def _requeue_blocked_item(
    factory: async_sessionmaker[AsyncSession], settings: Settings, redis: Redis, scope: InternalJobScope,
    item: tuple[UUID, UUID, int, str | None, str | None], work_ids: list[UUID],
) -> None:
    """Requeue one policy/capability-blocked work item when its dependency fingerprint changed.

    Only ``ai_policy_denied``/``structured_unsupported`` rows with a saved fingerprint are considered. If the
    version/source is no longer eligible, or policy still denies or the requeue loses its compare-and-set, the
    recheck is deferred instead. A successful requeue appends the id to ``work_ids``. One commit.
    """
    multi_workspace_enabled = settings.multi_workspace_enabled
    blocked_id, version_id, captured_generation, error_code, old_fingerprint = item
    if error_code not in {"ai_policy_denied", "structured_unsupported"} or old_fingerprint is None:
        return
    async with factory() as session:
        ready = await documents.get_ready_version_ref(
            session, version_id, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        source = await sources.lock_source(
            session, ready.source_id, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        ) if ready is not None else None
        current = await documents.get_ready_version_ref(
            session, version_id, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        ) if ready is not None else None
        if (
            ready is None or source is None or source.status != "active" or source.local_only
            or source.generation != captured_generation or current is None
            or current.document_id != ready.document_id or current.source_id != source.id
            or current.source_generation != captured_generation
        ):
            await entities.defer_blocked_extraction_recheck(
                session, blocked_id, old_fingerprint, scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
            await session.commit()
            return
        config = settings_public.derive_for_role(await settings_public.get_ai_execution_config(
            session, settings, redis, scope=scope,
        ), EXTRACTION_ROLE)
        fingerprint, capability_supported = await _dependency_snapshot(config, redis, scope=scope)
        mapping = config.defaults.resolve(EXTRACTION_ROLE)
        if (
            capability_supported
            and _policy_allows_extraction(config, mapping, config.endpoint_destination_id, source.local_only)
            and await entities.requeue_blocked_extraction_work(
                session, blocked_id, old_fingerprint, fingerprint, scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        ):
            work_ids.append(blocked_id)
        else:
            await entities.defer_blocked_extraction_recheck(
                session, blocked_id, old_fingerprint, scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        await session.commit()


async def _recover_workspace(
    factory: async_sessionmaker[AsyncSession], settings: Settings, redis: ArqRedis, workspace_id: UUID,
) -> int:
    """Recover one workspace: re-schedule its ready page, requeue changed blocks, enqueue recoverable work.

    Order: page scan -> per-ref recovery -> blocked/exhausted/recoverable listing (one commit) -> dependency
    requeue -> cursor persist -> enqueue (deduplicated, model queue). Returns the number of enqueued jobs.
    """
    multi_workspace_enabled = settings.multi_workspace_enabled
    page = await _scan_ready_page(factory, redis, workspace_id, multi_workspace_enabled)
    if page is None:
        return 0
    scope = page.scope
    for ref in page.refs:
        await _recover_ready_ref(factory, scope, multi_workspace_enabled, ref)
    async with factory() as session:
        blocked_items = await entities.list_blocked_extraction_work(
            session, limit=25, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        await entities.terminalize_exhausted_extraction_work(
            session, limit=25, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        work_ids = await entities.list_recoverable_extraction_work(
            session, limit=25, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        await session.commit()
    for item in blocked_items:
        await _requeue_blocked_item(factory, settings, cast(Redis, redis), scope, item, work_ids)
    if page.next_cursor:
        await redis.set(page.cursor_key, page.next_cursor)
    else:
        await redis.delete(page.cursor_key)
    enqueued = 0
    for work_id in dict.fromkeys(work_ids):
        await redis.enqueue_job(
            "process_entity_extraction_work", str(work_id),
            _job_id=f"entity-extraction:{work_id}",
            _queue_name=queue_for("process_entity_extraction_work"),
        )
        enqueued += 1
    return enqueued


async def recover_entity_extraction_work(ctx: dict[str, object]) -> int:
    """Recover bounded extraction work under each durable workspace binding.

    Visits one keyset page of workspaces per run and recovers each independently (see ``_recover_workspace``).
    Returns the total number of jobs enqueued.
    """
    factory = _factory(ctx)
    redis = cast(ArqRedis, ctx["redis"])
    settings = cast(Settings, ctx["settings"])
    enqueued = 0
    for workspace_id in await _recovery_workspace_ids(ctx, factory):
        enqueued += await _recover_workspace(factory, settings, redis, workspace_id)
    return enqueued


@dataclass
class _RunState:
    """Mutable cross-phase state read by the failure arms of ``process_entity_extraction_work``."""

    dependency_fingerprint: str | None = None


@dataclass(frozen=True)
class _Claim:
    """Identity of one claimed extraction work row, fixed after the claim transaction commits."""

    work_id: UUID
    lease_owner: str
    scope: InternalJobScope
    multi_workspace_enabled: bool
    version_id: UUID
    captured_generation: int


@dataclass(frozen=True)
class _Locked:
    """Plain DTOs captured while Source/Document locks were held (the locks are released before inference)."""

    access_fence: AccessFence
    ready: documents.ReadyVersionRef
    source_local_only: bool
    source_generation: int


@dataclass(frozen=True)
class _Snapshot:
    """Everything the provider call and the later re-lock-and-compare phases need, as plain values."""

    claim: _Claim
    access_fence: AccessFence
    ready: documents.ReadyVersionRef
    data: documents.ExtractionInput
    config: AIExecutionConfig
    mapping: ModelMapping | None
    destination: str | None
    policy: RequestPolicy


@dataclass(frozen=True)
class _Skip:
    """Marker: the work was parked (error recorded and committed) so the run ends without inference."""


_Plan = tuple[str, str | None, list[str], str | None]
_OwnerDecision = tuple[UUID, str, UUID | None]
_KnownCandidates = dict[str, tuple[list[dict[str, object]], bool]]


@dataclass
class _Published:
    """Entity publication results shared with relationship publication and the final acknowledgement."""

    key_to_entity: dict[str, UUID]
    membership_for: dict[tuple[str, UUID], UUID]
    review: list[dict[str, object]]
    facts: list[dict[str, object]]


def _candidate_key(candidate_type: str, key: str) -> str:
    """Stable digest identifying one model-local candidate key within its entity type."""
    return hashlib.sha256(f"{candidate_type}:{key}".encode()).hexdigest()


def _with_digest(snapshot: dict[str, object]) -> dict[str, object]:
    """Add the canonical-JSON ``snapshot_digest`` to a review snapshot (in place) and return it."""
    snapshot["snapshot_digest"] = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return snapshot


async def _admit_claim(
    session: AsyncSession, workspace_id: UUID, work_id: UUID, lease_owner: str, settings: Settings,
) -> tuple[InternalJobScope, EntityExtractionWork | None] | None:
    """Admission attempt: release stale locks, resolve the owner scope, and claim the work row.

    Starts with ``session.rollback()`` so a 409 retry (see ``admit_retry_stale``) re-reads fresh state. Returns
    ``None`` when the workspace owner is gone or the module is disabled; otherwise the scope and the claimed row
    (``None`` when another worker holds the lease). Does not commit.
    """
    multi_workspace_enabled = settings.multi_workspace_enabled
    await session.rollback()
    job_scope = await _workspace_job_scope(
        session, workspace_id, multi_workspace_enabled=multi_workspace_enabled,
    )
    if job_scope is None or not await settings_public.module_is_enabled(
        session, "knowledge.entities", scope=job_scope, multi_workspace_enabled=multi_workspace_enabled,
    ):
        return None
    return job_scope, await entities.claim_extraction_work(
        session, work_id, lease_owner, datetime.now(UTC), scope=job_scope,
        multi_workspace_enabled=multi_workspace_enabled,
        lease_seconds=max(110, settings.background_llm_timeout_seconds + 75),
    )


async def _claim(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession], settings: Settings,
    work_id: UUID, lease_owner: str,
) -> _Claim | None:
    """Claim ``work_id`` on ``session`` and commit the claim; ``None`` means there is nothing to run.

    A denied/stale scope (HTTP error from admission) terminalizes the row on its own session. A claim that
    lost the lease race commits the (empty) transaction and returns ``None``. On success the lease is
    committed before returning so inference never runs inside the claim transaction.
    """
    workspace_id = await session.scalar(select(EntityExtractionWork.workspace_id).where(
        EntityExtractionWork.id == work_id,
    ))
    if workspace_id is None:
        return None
    try:
        admitted = await admit_retry_stale(
            lambda: _admit_claim(session, workspace_id, work_id, lease_owner, settings)
        )
    except (HTTPException, DomainError) as exc:
        await session.rollback()
        await terminalize(factory, EntityExtractionWork, work_id, workspace_id, denial_code(exc) or STALE_SCOPE)
        return None
    if admitted is None:
        return None
    scope, work = admitted
    if work is None:
        await session.commit()
        return None
    claim = _Claim(
        work_id=work_id, lease_owner=lease_owner, scope=scope,
        multi_workspace_enabled=settings.multi_workspace_enabled,
        version_id=work.document_version_id, captured_generation=work.source_generation,
    )
    await session.commit()
    return claim


async def _block_work(
    session: AsyncSession, claim: _Claim, error_code: str, *, dependency_fingerprint: str | None = None,
) -> None:
    """Park the lease-owned work as blocked with ``error_code`` and commit on ``session``."""
    await entities.set_extraction_work_error(
        session, claim.work_id, claim.lease_owner, error_code, blocked=True,
        dependency_fingerprint=dependency_fingerprint,
        scope=claim.scope, multi_workspace_enabled=claim.multi_workspace_enabled,
    )
    await session.commit()


async def _lock_and_check_source(session: AsyncSession, claim: _Claim) -> _Locked | _Skip:
    """Lock Source then Document, compare against the claimed generation, and capture plain DTOs.

    Lock order is access fence read -> ready ref -> Source -> Document -> ready ref re-read. Every mismatch
    (document gone, generation drift, local-only, graph disabled) parks the work via ``_block_work`` (which
    commits) and returns ``_Skip``. Locks stay held on ``session`` until the caller's rollback.
    """
    scope, mwe = claim.scope, claim.multi_workspace_enabled
    access_fence = await workspaces.read_access_fence(session, scope=scope, multi_workspace_enabled=mwe)
    ready = await documents.get_ready_version_ref(session, claim.version_id, scope=scope, multi_workspace_enabled=mwe)
    if ready is None:
        await _block_work(session, claim, "document_unavailable")
        return _Skip()
    expected_document_id = ready.document_id
    source = await sources.lock_source(
        session, ready.source_id, scope=scope, multi_workspace_enabled=mwe, expected_access_fence=access_fence,
    )
    if source is None or source.status != "active" or source.generation != claim.captured_generation:
        await _block_work(session, claim, "source_generation_changed")
        return _Skip()
    if not await documents.lock_document_for_extraction(
        session, ready.document_id, ready.source_id, scope=scope, multi_workspace_enabled=mwe,
        access_fence=access_fence, source_fence=source,
        expected_raw_uri=ready.raw_uri, expected_mime_type=ready.mime_type,
    ):
        await _block_work(session, claim, "document_unavailable")
        return _Skip()
    # Locks are released before inference and reacquired for publication.
    ready = await documents.get_ready_version_ref(session, claim.version_id, scope=scope, multi_workspace_enabled=mwe)
    if (
        ready is None or ready.document_id != expected_document_id
        or ready.source_id != source.id or ready.source_generation != claim.captured_generation
    ):
        await _block_work(session, claim, "document_unavailable")
        return _Skip()
    if source.local_only:
        await _block_work(session, claim, "local_only_source")
        return _Skip()
    if not source.graph_enabled:
        # Switch flipped after scheduling: park quietly (blocked, no fingerprint so dependency
        # recovery ignores it); intentional owner choice, not an error to log.
        await _block_work(session, claim, "graph_disabled")
        return _Skip()
    return _Locked(
        access_fence=access_fence, ready=ready, source_local_only=source.local_only,
        source_generation=source.generation,
    )


async def _capture_extraction_snapshot(
    session: AsyncSession, claim: _Claim, settings: Settings, redis: Redis, state: _RunState,
) -> _Snapshot | _Skip:
    """Phase 1: lock, validate and capture plain DTOs plus the AI policy for the extraction.

    Returns ``_Skip`` after parking the work (error recorded and committed on ``session``) when the source,
    document, input or AI policy disqualifies it. Records the dependency fingerprint on ``state`` as soon as it
    is computed so the failure arms can attach it. Leaves the session's locks held; the caller releases them.
    """
    scope, mwe = claim.scope, claim.multi_workspace_enabled
    locked = await _lock_and_check_source(session, claim)
    if isinstance(locked, _Skip):
        return locked
    data = await documents.read_extraction_input(session, claim.version_id, scope=scope, multi_workspace_enabled=mwe)
    if (
        data is None or data.source_generation != locked.source_generation
        or data.source_generation != claim.captured_generation
    ):
        await _block_work(session, claim, "document_unavailable")
        return _Skip()
    role = EXTRACTION_ROLE
    config = settings_public.derive_for_role(
        await settings_public.get_ai_execution_config(session, settings, redis, scope=scope), role)
    mapping = config.defaults.resolve(role)
    destination = config.endpoint_destination_id
    state.dependency_fingerprint, _ = await _dependency_snapshot(config, redis, scope=scope)
    policy = RequestPolicy(
        workspace_id=config.workspace_id, actor_user_id=config.actor_user_id,
        membership_revision=config.membership_revision, gateway_identity=config.gateway_identity,
        reasoning_allowed=config.privacy.allow_remote_reasoning,
        local_only=locked.source_local_only,
        permitted_destinations=frozenset({destination}) if destination else frozenset(),
        reasoning_destinations=frozenset(config.privacy.reasoning_destinations),
        configuration_revision=config.configuration_revision,
    )
    if not _policy_allows_extraction(config, mapping, destination, locked.source_local_only):
        await _block_work(session, claim, "ai_policy_denied", dependency_fingerprint=state.dependency_fingerprint)
        return _Skip()
    return _Snapshot(
        claim=claim, access_fence=locked.access_fence, ready=locked.ready, data=data,
        config=config, mapping=mapping, destination=destination, policy=policy,
    )


async def _recheck_before_send(
    factory: async_sessionmaker[AsyncSession], settings: Settings, redis: Redis, snap: _Snapshot,
) -> None:
    """Recheck saved settings, privacy policy, access fence and source revision immediately before remote use.

    Runs on its own fresh session because the outer transaction is rolled back before inference. Raises
    ``PrivacyPolicyDenied`` (blocks the work) on policy/source drift and HTTP 409 (retried) when the workspace
    access fence changed.
    """
    claim, config, mapping, destination = snap.claim, snap.config, snap.mapping, snap.destination
    scope, mwe, role = claim.scope, claim.multi_workspace_enabled, EXTRACTION_ROLE
    async with factory() as current_session:
        current = settings_public.derive_for_role(await settings_public.get_ai_execution_config(
            current_session, settings, redis, scope=scope,
        ), role)
        current_mapping = current.defaults.resolve(role)
        current_destination = current.endpoint_destination_id
        if (
            current.configuration_revision != config.configuration_revision
            or current.gateway_identity != config.gateway_identity
            or current_destination != destination or current_mapping != mapping
            or current.endpoint_policy_denied or not current_destination
        ):
            raise PrivacyPolicyDenied("Extraction policy changed before send")
        current_policy = RequestPolicy(
            workspace_id=current.workspace_id, actor_user_id=current.actor_user_id,
            membership_revision=current.membership_revision,
            gateway_identity=current.gateway_identity,
            reasoning_allowed=current.privacy.allow_remote_reasoning,
            permitted_destinations=frozenset({current_destination}),
            reasoning_destinations=frozenset(current.privacy.reasoning_destinations),
            configuration_revision=current.configuration_revision,
        )
        if not may_send(
            current_policy, role, current_mapping, current_destination,
            current.gateway_credential_configured, "structured",
        ):
            raise PrivacyPolicyDenied("Extraction policy denied before send")
        # Fresh session: the outer transaction is rolled back before inference.
        if await workspaces.read_access_fence(
            current_session, scope=scope, multi_workspace_enabled=mwe,
        ) != snap.access_fence:
            # 409 (not PrivacyPolicyDenied) so work is retried, not blocked forever.
            raise ConflictError("Workspace access changed")
        current_ready = await documents.get_ready_version_ref(
            current_session, claim.version_id, scope=scope, multi_workspace_enabled=mwe,
        )
        if (
            current_ready is None or current_ready.document_id != snap.data.document_id
            or current_ready.source_id != snap.data.source_id
            or current_ready.source_generation != claim.captured_generation
            or current_ready.local_only
        ):
            raise PrivacyPolicyDenied("Extraction source revision changed before send")


async def _call_provider(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession], settings: Settings, redis: Redis,
    snap: _Snapshot,
) -> object:
    """Phase 2: release every lock held on ``session`` and perform the structured provider request.

    ``session.rollback()`` is deliberately issued after building the gateway (no I/O) and strictly before the
    first gateway I/O so workspace, membership, Source, Document and raw-URI locks are never held across
    inference. ``before_send`` rechecks policy on an independent session. Provider errors propagate.
    """
    config, mapping = snap.config, snap.mapping
    before_send = partial(_recheck_before_send, factory, settings, redis, snap)
    gateway = ModelGateway(
        evidence=settings_public.capability_evidence(factory, config),
        redis=redis, base_url=config.gateway_base_url, api_key=config.gateway_api_key,
        destination_id=cast(str, snap.destination), timeout_seconds=settings.background_llm_timeout_seconds,
        scope=snap.claim.scope, gateway_identity=config.gateway_identity, model_identity=config.model_identity,
        configuration_revision=config.configuration_revision, before_send=before_send,
        approved_endpoint_cidrs=config.endpoint_allowed_cidrs,
        allow_public_endpoints=config.allow_public_providers, kind=config.gateway_kind,
    )
    # Release workspace, membership, Source, Document and raw-URI locks before provider I/O.
    await session.rollback()
    return await gateway.structured(
        EXTRACTION_ROLE, mapping, snap.policy,
        extraction_messages([(chunk.id, chunk.content) for chunk in snap.data.chunks], resolve_prompt(config.system_prompts, "entity_extraction")),
        response_schema(), probe=False, before_send=before_send,
    )


def _validate_extraction_response(
    response: object, data: documents.ExtractionInput,
) -> tuple[ExtractionOutput, str | None, dict[str, object] | None]:
    """Parse and check the model response without touching the database (runs before any lock is reacquired).

    Raises ``ValueError`` with a stable code: ``invalid_model_response`` (not an object),
    ``unknown_evidence_chunk`` (evidence outside the input chunks) or ``duplicate_candidate_key``. Edges that
    are self-loops or name an unlisted entity are dropped (in place on the returned output) so they cannot
    discard the valid entities extracted alongside them.
    """
    if not isinstance(response, dict):
        raise ValueError("invalid_model_response")
    extracted, actual_model, usage = response_content(response)
    allowed_chunks = {chunk.id for chunk in data.chunks}
    if any(not set(item.chunk_ids) <= allowed_chunks for item in extracted.entities):
        raise ValueError("unknown_evidence_chunk")
    if any(item.chunk_id not in allowed_chunks for item in extracted.relationships):
        raise ValueError("unknown_evidence_chunk")
    if len({item.key for item in extracted.entities}) != len(extracted.entities):
        raise ValueError("duplicate_candidate_key")
    # Models routinely emit self-loops or edges naming an entity they did not list. Such an
    # edge has no valid endpoints to store, but it must not discard the valid entities
    # extracted alongside it, so drop only the bad edges.
    entity_keys = {e.key for e in extracted.entities}
    extracted.relationships = [
        item for item in extracted.relationships
        if item.source_key != item.target_key
        and item.source_key in entity_keys and item.target_key in entity_keys
    ]
    return extracted, actual_model, usage


async def _relock_and_compare(session: AsyncSession, snap: _Snapshot) -> None:
    """Reacquire Source then Document locks released before inference and compare with the phase-1 snapshot.

    Raises ``ValueError("document_unavailable")`` (retryable) on any drift of source status, generation,
    local-only flag, raw URI/MIME type, document identity or ready version. Locks stay held until the
    caller's single final commit/rollback.
    """
    claim, data = snap.claim, snap.data
    scope, mwe = claim.scope, claim.multi_workspace_enabled
    relocked = await sources.lock_source(
        session, data.source_id, scope=scope, multi_workspace_enabled=mwe, expected_access_fence=snap.access_fence,
    )
    if (
        relocked is None or relocked.status != "active" or relocked.generation != claim.captured_generation
        or relocked.local_only
        or not await documents.lock_document_for_extraction(
            session, data.document_id, relocked.id, scope=scope, multi_workspace_enabled=mwe,
            access_fence=snap.access_fence, source_fence=relocked,
            expected_raw_uri=snap.ready.raw_uri, expected_mime_type=snap.ready.mime_type,
        )
    ):
        raise ValueError("document_unavailable")
    current_ready = await documents.get_ready_version_ref(
        session, claim.version_id, scope=scope, multi_workspace_enabled=mwe,
    )
    if (
        current_ready is None or current_ready.document_id != data.document_id
        or current_ready.source_id != data.source_id
        or current_ready.source_generation != claim.captured_generation
    ):
        raise ValueError("document_unavailable")


async def _read_evidence_by_chunk(
    session: AsyncSession, snap: _Snapshot, extracted: ExtractionOutput,
) -> dict[UUID, Any]:
    """Read evidence refs for every chunk cited by an extracted entity, keyed by chunk id.

    Raises ``ValueError("document_unavailable")`` when the documents owner reports the evidence is gone.
    """
    claim, data = snap.claim, snap.data
    membership_chunk_ids = sorted({chunk_id for item in extracted.entities for chunk_id in item.chunk_ids}, key=str)
    evidence_refs = await documents.read_extraction_evidence_refs(
        session, document_id=data.document_id, document_version_id=claim.version_id,
        source_id=data.source_id, source_generation=claim.captured_generation,
        chunk_ids=membership_chunk_ids,
        scope=claim.scope, multi_workspace_enabled=claim.multi_workspace_enabled,
    ) if membership_chunk_ids else []
    if evidence_refs is None:
        raise ValueError("document_unavailable")
    return {ref.chunk_id: ref for ref in evidence_refs}


async def _list_known_candidates(
    session: AsyncSession, claim: _Claim, names_by_type: dict[str, list[str]],
) -> _KnownCandidates:
    """List bounded resolution candidates per entity type (types iterate in first-seen order)."""
    known: _KnownCandidates = {}
    for entity_type, names in names_by_type.items():
        known[entity_type] = await entities.list_resolution_candidates(
            session, entity_type, names, scope=claim.scope,
            multi_workspace_enabled=claim.multi_workspace_enabled,
        )
    return known


def _initial_plan(candidates: list[ExtractedEntity], known_cache: _KnownCandidates) -> dict[str, _Plan]:
    """Resolve every candidate against its type's known entities; bounded contexts become review-only."""
    plan: dict[str, _Plan] = {}
    for candidate in candidates:
        known, overflow = known_cache[candidate.type]
        if overflow:
            plan[candidate.key] = ("review", None, [], "resolution_context_bounded")
            continue
        resolution, match_id, possible = resolve_candidate(candidate.name, candidate.type, known)
        plan[candidate.key] = (
            resolution, match_id, possible,
            "ambiguous_identity" if resolution == "review" else None,
        )
    return plan


def _pre_resolve_near_duplicates(candidates: list[ExtractedEntity], plan: dict[str, _Plan]) -> None:
    """Pre-resolve intra-response near-duplicates (in place) so resolve-then-lock cannot create two new rows.

    Same-type candidates that are both ``new``/``matched`` and whose canonical names are >= 0.78 similar
    become review-only; the ``new`` side of a new/matched pair cites the matched entity as a possible match.
    Pure: no I/O, iteration order is the response order.
    """
    for index, left in enumerate(candidates):
        left_resolution, left_id, _left_possible, _left_reason = plan[left.key]
        for right in candidates[index + 1:]:
            if left.type != right.type:
                continue
            right_resolution, right_id, _right_possible, _right_reason = plan[right.key]
            if left_resolution not in {"new", "matched"} or right_resolution not in {"new", "matched"}:
                continue
            similarity = SequenceMatcher(
                None, canonicalize_name(left.name), canonicalize_name(right.name)
            ).ratio()
            if similarity < 0.78:
                continue
            if left_resolution == "new" and right_resolution == "new":
                plan[left.key] = ("review", None, [], "ambiguous_identity")
                plan[right.key] = ("review", None, [], "ambiguous_identity")
            elif left_resolution == "new" and right_resolution == "matched":
                plan[left.key] = ("review", None, [right_id] if right_id else [], "ambiguous_identity")
            elif left_resolution == "matched" and right_resolution == "new":
                plan[right.key] = ("review", None, [left_id] if left_id else [], "ambiguous_identity")


async def _apply_owner_decisions(
    session: AsyncSession, claim: _Claim, candidates: list[ExtractedEntity], plan: dict[str, _Plan],
    initial_decisions: dict[str, tuple[UUID, str, UUID | None] | None],
) -> dict[str, _OwnerDecision | None]:
    """Canonicalize owner correction decisions and let them override ``plan`` (in place).

    A target that no longer resolves becomes a ``conflict``. ``suppress`` and ``conflict`` force review;
    ``assign`` forces a match on the canonical target. Returns the canonical decision per candidate key so the
    refresh pass can detect later changes.
    """
    canonical_decisions: dict[str, _OwnerDecision | None] = {}
    for candidate in candidates:
        decision = initial_decisions.get(candidate.key)
        if decision is None:
            canonical_decisions[candidate.key] = None
            continue
        decision_id, action, target_id = decision
        if target_id is not None:
            try:
                target_id = await entities.resolve_canonical_entity_id(
                    session, target_id, scope=claim.scope,
                    multi_workspace_enabled=claim.multi_workspace_enabled,
                )
            except (LookupError, ValueError):
                action, target_id = "conflict", None
        canonical_decisions[candidate.key] = (decision_id, action, target_id)
        if action == "suppress":
            plan[candidate.key] = ("review", None, [], "owner_suppressed_candidate")
        elif action == "assign" and target_id is not None:
            plan[candidate.key] = ("matched", str(target_id), [], None)
        elif action == "conflict":
            plan[candidate.key] = ("review", None, [], "conflicting_owner_corrections")
    return canonical_decisions


async def _lock_matched_entities(session: AsyncSession, claim: _Claim, plan: dict[str, _Plan]) -> dict[UUID, Any]:
    """Lock every matched entity FOR UPDATE in one call, in ``str``-sorted id order (deadlock-free).

    If an entity vanished or changed identity, every matched plan entry degrades (in place) to review with
    ``identity_changed_during_resolution``. Returns the locked refs keyed by requested id.
    """
    existing_ids = sorted({
        UUID(match_id) for resolution, match_id, _, _ in plan.values()
        if resolution == "matched" and match_id is not None
    }, key=str)
    locked_refs: dict[UUID, Any] = {}
    if existing_ids:
        try:
            refs = await entities.get_entity_refs(
                session, existing_ids, for_write=True, scope=claim.scope,
                multi_workspace_enabled=claim.multi_workspace_enabled,
            )
            locked_refs = {ref.requested_id: ref for ref in refs}
        except (LookupError, ValueError):
            for key, (resolution, match_id, possible, _reason) in list(plan.items()):
                if resolution == "matched":
                    plan[key] = ("review", None, possible + ([match_id] if match_id else []), "identity_changed_during_resolution")
    return locked_refs


async def _current_owner_decision(
    session: AsyncSession, claim: _Claim, decision: tuple[UUID, str, UUID | None] | None,
) -> _OwnerDecision | None:
    """Canonicalize a freshly re-read owner decision; an unresolvable target becomes ``None`` (not conflict)."""
    if decision is None:
        return None
    decision_id, action, target_id = decision
    if target_id is not None:
        try:
            target_id = await entities.resolve_canonical_entity_id(
                session, target_id, scope=claim.scope,
                multi_workspace_enabled=claim.multi_workspace_enabled,
            )
        except (LookupError, ValueError):
            target_id = None
    return decision_id, action, target_id


def _owner_override(
    owner_decision: _OwnerDecision, current_resolution: str, current_id: str | None, possible: list[str],
) -> tuple[_Plan | None, str, str | None]:
    """Apply a recorded owner decision to a refreshed resolution.

    Returns ``(final_plan, resolution, id)``: a non-``None`` plan ends the candidate's refresh (suppressed or
    conflicting corrections); otherwise the resolution is forced to the assigned entity.
    """
    _, action, assigned_id = owner_decision
    if action == "suppress":
        return ("review", None, [], "owner_suppressed_candidate"), current_resolution, current_id
    if action != "assign" or assigned_id is None or (
        current_resolution == "matched" and current_id != str(assigned_id)
    ):
        return ("review", None, possible, "conflicting_owner_corrections"), current_resolution, current_id
    return None, "matched", str(assigned_id)


def _recompute_plan_entry(
    candidate: ExtractedEntity, known: list[dict[str, object]], old: _Plan,
    owner_decision: _OwnerDecision | None, locked_refs: dict[UUID, Any],
) -> _Plan:
    """Re-resolve one candidate after the entity lock closure and return its final plan entry (pure).

    Any change from the pre-lock outcome, or a matched entity whose locked type/revision no longer agrees with
    the refreshed listing, becomes review-only; a late entity lock is never acquired out of order.
    """
    old_resolution, old_id, _old_possible, old_reason = old
    current_resolution, current_id, possible = resolve_candidate(candidate.name, candidate.type, known)
    if owner_decision is not None:
        final, current_resolution, current_id = _owner_override(
            owner_decision, current_resolution, current_id, possible,
        )
        if final is not None:
            return final
    if old_resolution == "review":
        return "review", None, possible, old_reason or "ambiguous_identity"
    if current_resolution != old_resolution or current_id != old_id:
        ids = possible + ([current_id] if current_id else []) + ([old_id] if old_id else [])
        return "review", None, sorted(set(ids)), "identity_changed_during_resolution"
    if current_resolution == "matched":
        identifier = UUID(current_id) if current_id else None
        current = next((item for item in known if str(item["id"]) == current_id), None)
        locked = locked_refs.get(identifier) if identifier else None
        if (
            current is None or locked is None or locked.type != candidate.type
            or locked.revision != current.get("revision")
        ):
            return "review", None, [current_id] if current_id else [], "identity_changed_during_resolution"
    return current_resolution, current_id, possible, None


async def _refresh_plan(
    session: AsyncSession, snap: _Snapshot, candidates: list[ExtractedEntity], plan: dict[str, _Plan],
    names_by_type: dict[str, list[str]], bindings: dict[str, tuple[str, set[UUID]]],
    canonical_decisions: dict[str, _OwnerDecision | None], locked_refs: dict[UUID, Any],
) -> None:
    """Refresh names, types, revisions and exact confirmed aliases after the stable sorted entity lock closure.

    A changed outcome becomes review-only (updates ``plan`` in place). Re-reads owner decisions FOR UPDATE so
    a concurrent correction cannot slip between resolution and publication. Never locks entities here.
    """
    claim = snap.claim
    refreshed = await _list_known_candidates(session, claim, names_by_type)
    current_decisions = await entities.get_document_correction_decisions(
        session, snap.data.document_id, claim.version_id, bindings, for_update=True,
        scope=claim.scope, multi_workspace_enabled=claim.multi_workspace_enabled,
    )
    for candidate in candidates:
        old = plan[candidate.key]
        if old[3] == "resolution_context_bounded":
            continue
        known, overflow = refreshed[candidate.type]
        if overflow:
            plan[candidate.key] = ("review", None, [], "resolution_context_bounded")
            continue
        current_owner = await _current_owner_decision(session, claim, current_decisions.get(candidate.key))
        if current_owner != canonical_decisions[candidate.key]:
            plan[candidate.key] = ("review", None, [], "identity_changed_during_resolution")
            continue
        plan[candidate.key] = _recompute_plan_entry(
            candidate, known, old, canonical_decisions[candidate.key], locked_refs,
        )


async def _resolve_entities(
    session: AsyncSession, snap: _Snapshot, extracted: ExtractionOutput,
) -> tuple[dict[str, _Plan], dict[str, str]]:
    """Resolve every extracted candidate to new / matched / review; returns ``(plan, match_fingerprints)``.

    Ordering: list candidates -> pre-resolve near-duplicates -> owner decisions -> lock matched entities in
    sorted id order (the only entity locks) -> refresh and compare. All work shares the caller's transaction.
    """
    claim, candidates = snap.claim, extracted.entities
    names_by_type: dict[str, list[str]] = {}
    for candidate in candidates:
        names_by_type.setdefault(candidate.type, []).append(candidate.name)
    plan = _initial_plan(candidates, await _list_known_candidates(session, claim, names_by_type))
    _pre_resolve_near_duplicates(candidates, plan)
    match_fingerprints = {c.key: candidate_match_fingerprint(c.name, c.type) for c in candidates}
    bindings = {c.key: (match_fingerprints[c.key], set(c.chunk_ids)) for c in candidates}
    initial_decisions = await entities.get_document_correction_decisions(
        session, snap.data.document_id, claim.version_id, bindings,
        scope=claim.scope, multi_workspace_enabled=claim.multi_workspace_enabled,
    )
    canonical_decisions = await _apply_owner_decisions(session, claim, candidates, plan, initial_decisions)
    locked_refs = await _lock_matched_entities(session, claim, plan)
    await _refresh_plan(session, snap, candidates, plan, names_by_type, bindings, canonical_decisions, locked_refs)
    return plan, match_fingerprints


def _entity_review_item(
    candidate: ExtractedEntity, fingerprint: str, reason: str | None, possible: list[str],
) -> dict[str, object]:
    """Build the owner-review record (with snapshot digest) for an unresolved entity candidate."""
    snapshot: dict[str, object] = {
        "kind": "entity", "candidate_id": str(uuid7()),
        "candidate_type": candidate.type, "candidate_name": candidate.name,
        "candidate_key": _candidate_key(candidate.type, candidate.key), "match_fingerprint": fingerprint,
        "chunk_ids": [str(identifier) for identifier in candidate.chunk_ids],
        "confidence": candidate.confidence,
    }
    _with_digest(snapshot)
    return {**snapshot, "reason": reason or "ambiguous_identity", "possible_entity_ids": possible}


async def _publish_entity(
    session: AsyncSession, snap: _Snapshot, candidate: ExtractedEntity, plan_entry: _Plan, fingerprint: str,
    evidence_by_chunk: dict[UUID, Any], published: _Published,
) -> None:
    """Create or reuse the entity for one resolved candidate and record its per-chunk memberships.

    A new entity also gets derived ``name`` (and ``description``) fields for each membership. Mutates
    ``published``; writes only inside the caller's transaction.
    """
    claim = snap.claim
    _resolution, match_id, _possible, _reason = plan_entry
    new_entity = plan_entry[0] == "new"
    entity_id = UUID(match_id) if match_id else await entities.create_extracted_entity(
        session, candidate.type, scope=claim.scope,
        multi_workspace_enabled=claim.multi_workspace_enabled,
    )
    published.key_to_entity[candidate.key] = entity_id
    candidate_key = _candidate_key(candidate.type, candidate.key)
    for chunk_id in candidate.chunk_ids:
        membership_id = await entities.record_extraction_membership(
            session, entity_id=entity_id, evidence_ref=evidence_by_chunk[chunk_id],
            source_generation=claim.captured_generation,
            extraction_identity=str(claim.work_id), candidate_key=candidate_key,
            match_fingerprint=fingerprint,
            observed_at=snap.data.observed_at, confidence=candidate.confidence,
            scope=claim.scope, multi_workspace_enabled=claim.multi_workspace_enabled,
        )
        published.membership_for[(candidate.key, chunk_id)] = membership_id
        if new_entity:
            await entities.publish_derived_field(
                session, entity_id=entity_id, membership_id=membership_id,
                field_name="name", value=candidate.name, scope=claim.scope,
                multi_workspace_enabled=claim.multi_workspace_enabled,
            )
            if candidate.description:
                await entities.publish_derived_field(
                    session, entity_id=entity_id, membership_id=membership_id,
                    field_name="description", value=candidate.description, scope=claim.scope,
                    multi_workspace_enabled=claim.multi_workspace_enabled,
                )
    published.facts.append({"entity_id": str(entity_id), "candidate_key": candidate.key, "confidence": candidate.confidence})


async def _publish_entities(
    session: AsyncSession, snap: _Snapshot, extracted: ExtractionOutput, plan: dict[str, _Plan],
    match_fingerprints: dict[str, str], evidence_by_chunk: dict[UUID, Any],
) -> _Published:
    """Publish resolved candidates in response order; review candidates are only snapshotted."""
    published = _Published(key_to_entity={}, membership_for={}, review=[], facts=[])
    for candidate in extracted.entities:
        entry = plan[candidate.key]
        if entry[0] == "review":
            published.review.append(_entity_review_item(
                candidate, match_fingerprints[candidate.key], entry[3], entry[2],
            ))
            continue
        await _publish_entity(
            session, snap, candidate, entry, match_fingerprints[candidate.key], evidence_by_chunk, published,
        )
    return published


def _retain_relationship_review(
    published: _Published, relation: ExtractedRelationship, candidate_by_key: dict[str, ExtractedEntity],
    reason: str, possible: list[str],
) -> None:
    """Snapshot an unresolved relationship's endpoints for later owner review (appends to ``published.review``)."""
    source_candidate = candidate_by_key[relation.source_key]
    target_candidate = candidate_by_key[relation.target_key]
    snapshot: dict[str, object] = {
        "kind": "relationship", "candidate_id": str(uuid7()),
        "candidate_name": relation.type, "relationship_type": relation.type,
        "source_candidate_key": _candidate_key(source_candidate.type, source_candidate.key),
        "target_candidate_key": _candidate_key(target_candidate.type, target_candidate.key),
        "chunk_id": str(relation.chunk_id), "confidence": relation.confidence,
    }
    _with_digest(snapshot)
    published.review.append({**snapshot, "reason": reason, "possible_entity_ids": possible})


async def _publish_relationships(
    session: AsyncSession, snap: _Snapshot, extracted: ExtractionOutput, published: _Published,
) -> None:
    """Publish edges whose endpoints both resolved to entities with same-chunk evidence; retain the rest for review."""
    claim = snap.claim
    candidate_by_key = {candidate.key: candidate for candidate in extracted.entities}
    for relation in extracted.relationships:
        source_id = published.key_to_entity.get(relation.source_key)
        target_id = published.key_to_entity.get(relation.target_key)
        if source_id is None or target_id is None:
            _retain_relationship_review(
                published, relation, candidate_by_key, "relationship_endpoint_requires_review", [])
            continue
        source_membership = published.membership_for.get((relation.source_key, relation.chunk_id))
        target_membership = published.membership_for.get((relation.target_key, relation.chunk_id))
        if source_membership is None or target_membership is None or source_id == target_id:
            _retain_relationship_review(
                published, relation, candidate_by_key, "relationship_identity_ambiguous",
                [str(source_id), str(target_id)])
            continue
        relationship_id = await relationships.publish_extracted_relationship(
            session, source_entity_id=source_id, target_entity_id=target_id,
            relationship_type=relation.type, document_version_id=claim.version_id,
            chunk_id=relation.chunk_id, source_membership_id=source_membership,
            target_membership_id=target_membership, confidence=relation.confidence,
            scope=claim.scope, multi_workspace_enabled=claim.multi_workspace_enabled,
        )
        if relationship_id is not None:
            published.facts.append({"relationship_id": str(relationship_id), "relationship": relation.type, "source_entity_id": str(source_id), "target_entity_id": str(target_id), "chunk_id": str(relation.chunk_id), "confidence": relation.confidence})


async def _publish_extraction(session: AsyncSession, snap: _Snapshot, response: object) -> None:
    """Phase 3: validate, re-lock, resolve and publish the graph, then acknowledge the lease.

    One transaction on ``session``: pure checks run before any lock is reacquired, ``finish_extraction_work``
    is the single ack, and ``commit_with_replay`` is the only commit. If the lease was lost the whole
    transaction is rolled back.
    """
    claim = snap.claim
    extracted, actual_model, usage = _validate_extraction_response(response, snap.data)
    await _relock_and_compare(session, snap)
    evidence_by_chunk = await _read_evidence_by_chunk(session, snap, extracted)
    plan, match_fingerprints = await _resolve_entities(session, snap, extracted)
    published = await _publish_entities(session, snap, extracted, plan, match_fingerprints, evidence_by_chunk)
    await _publish_relationships(session, snap, extracted, published)
    mapping = snap.mapping
    assert mapping is not None  # noqa: S101  # mypy narrowing invariant
    if await entities.finish_extraction_work(
        session, claim.work_id, claim.lease_owner, facts=published.facts, review=published.review,
        model=actual_model or mapping.model, usage=usage, scope=claim.scope,
        multi_workspace_enabled=claim.multi_workspace_enabled,
    ):
        await commit_with_replay(
            session,
            [make_graph_change(entity_id=entity_id, scope=claim.scope)
             for entity_id in sorted(set(published.key_to_entity.values()), key=str)],
            scope=claim.scope, multi_workspace_enabled=claim.multi_workspace_enabled,
            access_fence=snap.access_fence,
        )
    else:
        await session.rollback()


async def _record_failure(
    factory: async_sessionmaker[AsyncSession], claim: _Claim, error_code: str, *,
    blocked: bool, dependency_fingerprint: str | None = None,
) -> None:
    """Record ``error_code`` against the lease-owned work on a fresh session and commit.

    Used by every failure arm because the failed transaction's session is unusable; the update is a no-op
    when this worker no longer owns the lease.
    """
    async with factory() as session:
        await entities.set_extraction_work_error(
            session, claim.work_id, claim.lease_owner, error_code, blocked=blocked,
            dependency_fingerprint=dependency_fingerprint,
            scope=claim.scope, multi_workspace_enabled=claim.multi_workspace_enabled,
        )
        await session.commit()


async def process_entity_extraction_work(ctx: dict[str, object], work_id_value: str) -> None:
    """Run one lease-owned extraction job and publish only fenced graph changes.

    Commits the work claim (lease = background LLM timeout + 75 s, at least 110 s), locks source/document only to capture plain
    DTOs, then releases every lock before the provider request so chat's access
    fence is never blocked. Privacy policy and the access fence are rechecked on
    an independent session immediately before send; after the response the worker
    re-locks and compares source generation, raw URI and MIME type before
    publication (drift raises retryable ``document_unavailable``).
    Policy/capability blocks with a saved dependency fingerprint can be requeued
    after recovery detects a changed dependency. Local-only blocks have no
    fingerprint and remain parked at ``datetime.max``; bounded-input blocks are
    also blocked without dependency requeue. Transport and general failures use
    the bounded work retry policy. Graph writes roll back if final lease
    acknowledgement no longer belongs to this worker.
    """
    factory = _factory(ctx)
    settings = cast(Settings, ctx["settings"])
    redis = cast(Redis, ctx["redis"])
    work_id = UUID(work_id_value)
    lease_owner = hashlib.sha256(f"{work_id}:{datetime.now(UTC).isoformat()}".encode()).hexdigest()[:48]
    state = _RunState()
    async with factory() as session:
        claim = await _claim(session, factory, settings, work_id, lease_owner)
    if claim is None:
        return
    try:
        async with factory() as session:
            snap = await _capture_extraction_snapshot(session, claim, settings, redis, state)
            if isinstance(snap, _Skip):
                return
            response = await _call_provider(session, factory, settings, redis, snap)
            await _publish_extraction(session, snap, response)
    except (PrivacyPolicyDenied, CapabilityUnsupported) as exc:
        await _record_failure(
            factory, claim,
            "ai_policy_denied" if isinstance(exc, PrivacyPolicyDenied) else "structured_unsupported",
            blocked=True, dependency_fingerprint=state.dependency_fingerprint,
        )
    except ModelGatewayError:
        blocked = False  # no evidence gate any more: only transport/provider errors reach here, all retryable
        error_code = "model_transport_unavailable"
        await _record_failure(
            factory, claim, error_code, blocked=blocked,
            dependency_fingerprint=state.dependency_fingerprint if blocked else None,
        )
        # A blocked capability is a configuration state that repeats for every queued item until the model
        # settings change, so it stays out of the warning stream; transport failures are still warnings.
        (logger.debug if blocked else logger.warning)(
            "Entity extraction deferred work_id=%s error_code=%s", work_id, error_code)
    except Exception as exc:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
        status = error_status(exc)
        if status is not None and status != 409:
            # Lost permission mid-run is terminal; a 409 keeps the attempt-budgeted retry (re-resolved at next claim).
            await terminalize(factory, EntityExtractionWork, work_id, claim.scope.workspace_id, PERMISSION_LOST)
            return
        blocked = str(exc) in {"Extraction input exceeds its chunk or byte limit"}
        error_code = "extraction_input_limit" if blocked else str(exc) if str(exc) in {
            "invalid_model_response", "unknown_evidence_chunk", "duplicate_candidate_key",
            "invalid_relationship_endpoint", "document_unavailable",
        } else "extraction_failed"
        await _record_failure(factory, claim, error_code, blocked=blocked)
        logger.warning("Entity extraction failed work_id=%s error_code=%s", work_id, error_code)
