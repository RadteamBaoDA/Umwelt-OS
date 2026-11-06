"""Document-ready consumption: dependency snapshot, policy gate and scheduling of entity extraction work."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from arq.connections import ArqRedis
from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from core.errors import DomainError
from core.model_gateway.policy import may_send
from core.model_gateway.schemas import (
    AIExecutionConfig,
    ModelMapping,
    RequestPolicy,
)
from core.worker_context import worker_session_factory as _factory
from core.worker_queues import queue_for
from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope
from modules.ingestion import public as ingestion
from modules.knowledge.documents import public as documents
from modules.knowledge.entities import public as entities
from modules.knowledge.entities.extraction import (
    EXTRACTOR_VERSION,
    PROMPT_VERSION,
)
from modules.settings import public as settings_public
from modules.sources import public as sources

logger = logging.getLogger("modules.knowledge.entities.worker")
EXTRACTION_ROLE = "background"


async def _dependency_snapshot(
    config: AIExecutionConfig, redis: Redis, *, scope: Scope
) -> tuple[str, bool]:
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
    digest = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    return digest, True


def _policy_allows_extraction(
    config: AIExecutionConfig,
    mapping: ModelMapping | None,
    destination: str | None,
    local_only: bool,
) -> bool:
    """Apply destination, local-only, consent, and capability policy to extraction."""
    if mapping is None or not destination or config.endpoint_policy_denied:
        return False
    privacy = config.privacy
    policy = RequestPolicy(
        workspace_id=config.workspace_id,
        actor_user_id=config.actor_user_id,
        membership_revision=config.membership_revision,
        gateway_identity=config.gateway_identity,
        reasoning_allowed=privacy.allow_remote_reasoning,
        local_only=local_only,
        permitted_destinations=frozenset({destination}),
        reasoning_destinations=frozenset(privacy.reasoning_destinations),
        configuration_revision=config.configuration_revision,
    )
    return may_send(
        policy,
        EXTRACTION_ROLE,
        mapping,
        destination,
        config.gateway_credential_configured,
        "structured",
    )


@dataclass(frozen=True)
class _ScheduledReady:
    """Outcome of staging a ready-document event: the scheduled work ids and whether it is local-only."""

    work_id: UUID
    timeline_work_id: UUID
    local_only: bool


async def _ack_event(
    session: AsyncSession,
    event_uuid: UUID,
    scope: InternalJobScope,
    multi_workspace_enabled: bool,
) -> None:
    """Acknowledge the outbox event as delivered and commit (used by every stale-lineage exit)."""
    await ingestion.mark_event_delivered(
        session,
        event_uuid,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    await session.commit()


async def _lock_ready_lineage(
    session: AsyncSession,
    event_uuid: UUID,
    event: Any,
    fence: AccessFence,
    scope: InternalJobScope,
    multi_workspace_enabled: bool,
) -> tuple[Any, Any] | None:
    """Verify the event's lineage under Source then Document locks; returns ``(ready, source)`` or ``None``.

    Lock order: ready ref -> Source -> ready ref re-read -> Document -> ready ref re-read. Any mismatch with the
    event payload (version gone, source/generation drift, document lock refused) acknowledges the event
    (committing) and returns ``None``. On success the locks remain held for the caller's single commit.
    """
    version_id = UUID(str(event.payload["document_version_id"]))
    ready = await documents.get_ready_version_ref(
        session,
        version_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if ready is None or ready.document_id != UUID(str(event.payload["document_id"])):
        await _ack_event(session, event_uuid, scope, multi_workspace_enabled)
        return None
    source = await sources.lock_source(
        session,
        ready.source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=fence,
    )
    ready = await documents.get_ready_version_ref(
        session,
        version_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if (
        source is None
        or source.status != "active"
        or ready is None
        or ready.source_id != UUID(str(event.payload["source_id"]))
        or source.generation != ready.source_generation
        or ready.source_generation != int(event.payload["source_generation"])
    ):
        await _ack_event(session, event_uuid, scope, multi_workspace_enabled)
        return None
    # Keep both owners behind the source/document fence until their durable rows
    # and the single outbox acknowledgement commit together.
    if not await documents.lock_document_for_extraction(
        session,
        ready.document_id,
        ready.source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
        source_fence=source,
        expected_raw_uri=ready.raw_uri,
        expected_mime_type=ready.mime_type,
    ):
        await _ack_event(session, event_uuid, scope, multi_workspace_enabled)
        return None
    ready = await documents.get_ready_version_ref(
        session,
        version_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if ready is None or ready.source_generation != source.generation:
        await _ack_event(session, event_uuid, scope, multi_workspace_enabled)
        return None
    return ready, source


async def _schedule_ready_work(
    session: AsyncSession,
    event_uuid: UUID,
    version_id: UUID,
    ready: Any,
    source: Any,
    scope: InternalJobScope,
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
            session,
            ready,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        await _ack_event(session, event_uuid, scope, multi_workspace_enabled)
        return None
    work = await entities.schedule_extraction_work(
        session,
        version_id,
        ready.source_generation,
        EXTRACTOR_VERSION,
        PROMPT_VERSION,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if ready.local_only:
        work.status = "blocked"
        work.error_code = "local_only_source"
        work.next_attempt_at = datetime.max.replace(tzinfo=UTC)
        work.dependency_fingerprint = None
    work_id = work.id
    from modules.timeline import public as timeline

    timeline_work_id = await timeline.schedule_extraction_work(
        session,
        ready,
        "events-v1",
        "events-prompt-v1",
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    from modules.knowledge.temporal import public as temporal

    await temporal.schedule_version(
        session,
        ready,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    # Deterministic provider mapping (no model egress, so also valid for local-only
    # sources) shares this transaction and the source/document fences held above.
    await connectors.map_github_version(
        session,
        ready,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if ready.local_only:
        await timeline.block_local_only_extraction_work(
            session, timeline_work_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
    await _ack_event(session, event_uuid, scope, multi_workspace_enabled)
    return _ScheduledReady(
        work_id=work_id, timeline_work_id=timeline_work_id, local_only=bool(ready.local_only)
    )


async def _stage_ready_event(
    session: AsyncSession,
    event_uuid: UUID,
    scope: InternalJobScope,
    multi_workspace_enabled: bool,
) -> _ScheduledReady | None:
    """Validate the delivery row and lineage, then schedule work; ``None`` means nothing to enqueue.

    Reads the access fence before the event row so the fence is the first lock-ordered read. A delivered or
    missing event is a no-op; an unsupported type/version is marked ``failed`` and committed.
    """
    fence = await workspaces.read_access_fence(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    event = await ingestion.get_event_delivery(
        session,
        event_uuid,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if event is None or event.status == "delivered":
        return None
    if event.type != "document.version.ready" or event.version != 1:
        await ingestion.set_event_delivery(
            session,
            event_uuid,
            "failed",
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        await session.commit()
        return None
    lineage = await _lock_ready_lineage(
        session, event_uuid, event, fence, scope, multi_workspace_enabled
    )
    if lineage is None:
        return None
    ready, source = lineage
    version_id = UUID(str(event.payload["document_version_id"]))
    return await _schedule_ready_work(
        session, event_uuid, version_id, ready, source, scope, multi_workspace_enabled
    )


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
                session,
                event_uuid,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        except (HTTPException, DomainError):
            # Denied retained principal: no mutation and no retry; the dispatcher quarantines the event.
            await session.rollback()
            return
        if scope is None or not await settings_public.module_is_enabled(
            session,
            "knowledge.entities",
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
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
            "process_entity_extraction_work",
            str(scheduled.work_id),
            _job_id=f"entity-extraction:{scheduled.work_id}",
            _queue_name=queue_for("process_entity_extraction_work"),
        )
    with contextlib.suppress(Exception):
        await redis.enqueue_job(
            "process_timeline_extraction_work",
            str(scheduled.timeline_work_id),
            _job_id=f"timeline-extraction:{scheduled.timeline_work_id}",
            _queue_name=queue_for("process_timeline_extraction_work"),
        )
