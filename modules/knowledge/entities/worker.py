from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import cast
from uuid import UUID, uuid4

from arq.connections import ArqRedis
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.heavy_work import bounded_heavy_work
from core.model_gateway.cache import capability_key
from core.model_gateway.client import (
    CapabilityUnsupported,
    ModelGateway,
    ModelGatewayError,
    PrivacyPolicyDenied,
)
from core.model_gateway.policy import may_send
from core.model_gateway.schemas import (
    AIExecutionConfig,
    CapabilityResult,
    ModelMapping,
    RequestPolicy,
)
from core.realtime import commit_with_replay, make_graph_change
from core.workspaces import public as workspaces
from core.workspaces.schemas import InternalJobScope, Scope
from modules.ingestion import public as ingestion
from modules.knowledge.documents import public as documents
from modules.knowledge.entities import public as entities
from modules.knowledge.entities.extraction import (
    EXTRACTOR_VERSION,
    PROMPT_VERSION,
    ExtractedRelationship,
    extraction_messages,
    response_content,
    response_schema,
)
from modules.knowledge.entities.models import EntityExtractionWork
from modules.knowledge.entities.resolution import candidate_match_fingerprint, resolve_candidate
from modules.knowledge.entities.schemas import canonicalize_name
from modules.knowledge.relationships import public as relationships
from modules.settings import public as settings_public
from modules.sources import public as sources

logger = logging.getLogger(__name__)
EXTRACTION_TIMEOUT_SECONDS = 35
EXTRACTION_ALIAS = "reasoning-small"


def _factory(ctx: dict[str, object]) -> async_sessionmaker[AsyncSession]:
    """Get the worker's configured asynchronous database session factory."""
    return cast(async_sessionmaker[AsyncSession], ctx["session_factory"])


async def _workspace_job_scope(
    session: AsyncSession, workspace_id: UUID, *, multi_workspace_enabled: bool,
) -> InternalJobScope | None:
    """Resolve the current owner for a durable entity-work workspace binding."""
    owner = await workspaces.resolve_workspace_owner_context(
        session, workspace_id, multi_workspace_enabled=multi_workspace_enabled,
    )
    if owner is None:
        return None
    return InternalJobScope(
        workspace_id=workspace_id, actor_user_id=owner.user_id,
        membership_revision=owner.membership_revision,
    )


async def _dependency_snapshot(config: AIExecutionConfig, redis: Redis, *, scope: Scope) -> tuple[str, bool]:
    """Fingerprint non-secret extraction policy and report current capability.

    Cached capability data is accepted only when model, gateway, revision, and
    expiry match; the snapshot excludes endpoint and credential values.
    """
    # AIExecutionConfig is returned by the settings owner. Hash only non-secret
    # policy/model/capability metadata; never persist endpoint or credential data.
    alias = EXTRACTION_ALIAS
    aliases = config.aliases
    mapping = aliases.get(alias)
    gateway_identity = config.gateway_identity
    capability_value: dict[str, object] | None = None
    key: str | None = None
    if mapping is not None:
        key = capability_key(
            alias, mapping.model, mapping.version, "structured", gateway_identity,
            workspace_id=scope.workspace_id,
        )
        raw = await redis.get(key)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else None
            validated = CapabilityResult.model_validate(parsed) if isinstance(parsed, dict) else None
            capability_value = validated.model_dump(mode="json") if validated is not None else None
        except (json.JSONDecodeError, ValueError):
            capability_value = None
    try:
        capability_fresh = bool(
            capability_value and isinstance(capability_value.get("expires_at"), str)
            and datetime.fromisoformat(str(capability_value["expires_at"])) > datetime.now(UTC)
        )
    except ValueError:
        capability_fresh = False
    supported = bool(
        isinstance(capability_value, dict)
        and capability_value.get("result") == "supported"
        and capability_value.get("alias") == alias
        and capability_value.get("capability") == "structured"
        and capability_value.get("gateway_identity") == gateway_identity
        and mapping is not None
        and capability_value.get("model") == mapping.model
        and capability_value.get("version") == mapping.version
        and capability_value.get("configuration_revision") == config.configuration_revision
        and capability_fresh
    )
    privacy = config.privacy
    snapshot = {
        "revision": config.configuration_revision,
        "gateway": gateway_identity,
        "destination": config.endpoint_destination_id,
        "endpoint_denied": config.endpoint_policy_denied,
        "credential_configured": config.omniroute_credential_configured,
        "mapping": (mapping.model, mapping.version) if mapping is not None else None,
        "capability_key": key,
        "remote_reasoning": privacy.allow_remote_reasoning,
        "reasoning_destinations": sorted(privacy.reasoning_destinations),
        "capability": capability_value,
    }
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    return digest, supported


def _policy_allows_extraction(config: AIExecutionConfig, mapping: ModelMapping | None, destination: str | None, local_only: bool) -> bool:
    """Apply destination, local-only, consent, and capability policy to extraction."""
    if mapping is None or not destination or config.endpoint_policy_denied:
        return False
    privacy = config.privacy
    policy = RequestPolicy(
        reasoning_allowed=privacy.allow_remote_reasoning,
        local_only=local_only,
        permitted_destinations=frozenset({destination}),
        reasoning_destinations=frozenset(privacy.reasoning_destinations),
        configuration_revision=config.configuration_revision,
    )
    return may_send(
        policy, EXTRACTION_ALIAS, mapping, destination,
        config.omniroute_credential_configured, "structured",
    )


async def process_document_ready(ctx: dict[str, object], event_id: str) -> None:
    """Create or schedule extraction work for a durable ready-document event."""
    factory = _factory(ctx)
    event_uuid = UUID(event_id)
    multi_workspace_enabled = cast(Settings, ctx["settings"]).multi_workspace_enabled
    async with factory() as session:
        scope = await ingestion.resolve_ingestion_event_scope(
            session, event_uuid, multi_workspace_enabled=multi_workspace_enabled,
        )
        if scope is None or not await settings_public.module_is_enabled(
            session, "knowledge.entities", scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        ):
            return  # unavailable lineage or disabled module: leave the event unacknowledged
        fence = await workspaces.read_access_fence(
            session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        event = await ingestion.get_event_delivery(
            session, event_uuid, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        if event is None or event.status == "delivered":
            return
        if event.type != "document.version.ready" or event.version != 1:
            await ingestion.set_event_delivery(
                session, event_uuid, "failed", scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
            await session.commit()
            return
        version_id = UUID(str(event.payload["document_version_id"]))
        ready = await documents.get_ready_version_ref(
            session, version_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        if ready is None or ready.document_id != UUID(str(event.payload["document_id"])):
            await ingestion.mark_event_delivered(
                session, event_uuid, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
            await session.commit()
            return
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
            await ingestion.mark_event_delivered(
                session, event_uuid, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
            await session.commit()
            return
        # Keep both owners behind the source/document fence until their durable rows
        # and the single outbox acknowledgement commit together.
        if not await documents.lock_document_for_extraction(
            session, ready.document_id, ready.source_id, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=fence,
            source_fence=source, expected_raw_uri=ready.raw_uri,
            expected_mime_type=ready.mime_type,
        ):
            await ingestion.mark_event_delivered(
                session, event_uuid, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
            await session.commit()
            return
        ready = await documents.get_ready_version_ref(
            session, version_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        if ready is None or ready.source_generation != source.generation:
            await ingestion.mark_event_delivered(
                session, event_uuid, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
            await session.commit()
            return
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
        from modules.connectors import public as connectors
        await connectors.map_github_version(
            session, ready, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        if ready.local_only:
            await timeline.block_local_only_extraction_work(
                session, timeline_work_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        await ingestion.mark_event_delivered(
            session, event_uuid, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        await session.commit()
    if ready.local_only:
        return
    await process_entity_extraction_work(ctx, str(work_id))
    if not ready.local_only:
        from modules.timeline.worker import process_timeline_extraction_work
        await process_timeline_extraction_work(ctx, str(timeline_work_id))


async def recover_entity_extraction_work(ctx: dict[str, object]) -> int:
    """Recover bounded extraction work under each durable workspace binding."""
    factory = _factory(ctx)
    redis = cast(ArqRedis, ctx["redis"])
    multi_workspace_enabled = cast(Settings, ctx["settings"]).multi_workspace_enabled
    enqueued = 0
    async with factory() as session:
        workspace_ids = list((await session.scalars(
            select(EntityExtractionWork.workspace_id).distinct().order_by(EntityExtractionWork.workspace_id)
        )).all())
    for workspace_id in workspace_ids:
        async with factory() as session:
            scope = await _workspace_job_scope(
                session, workspace_id, multi_workspace_enabled=multi_workspace_enabled,
            )
            if scope is None or not await settings_public.module_is_enabled(
                session, "knowledge.entities", scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            ):
                continue
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
        for ref in refs:
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
                    continue
                work = await entities.schedule_extraction_work(
                    session, ref.document_version_id, source.generation, EXTRACTOR_VERSION,
                    PROMPT_VERSION, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                )
                if current.local_only and work.status != "succeeded" and not (
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
        for blocked_id, version_id, captured_generation, error_code, old_fingerprint in blocked_items:
            if error_code not in {"ai_policy_denied", "structured_unsupported"} or old_fingerprint is None:
                continue
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
                    continue
                config = await settings_public.get_ai_execution_config(
                    session, cast(Settings, ctx["settings"]), cast(Redis, ctx["redis"]), scope=scope,
                )
                fingerprint, capability_supported = await _dependency_snapshot(
                    config, cast(Redis, ctx["redis"]), scope=scope,
                )
                mapping = config.aliases.get(EXTRACTION_ALIAS)
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
        if next_cursor:
            await redis.set(cursor_key, next_cursor)
        else:
            await redis.delete(cursor_key)
        for work_id in dict.fromkeys(work_ids):
            await redis.enqueue_job(
                "process_entity_extraction_work", str(work_id),
                _job_id=f"entity-extraction:{work_id}",
            )
            enqueued += 1
    return enqueued


@bounded_heavy_work
async def process_entity_extraction_work(ctx: dict[str, object], work_id_value: str) -> None:
    """Run one lease-owned extraction job and publish only fenced graph changes.

    Commits the 110-second work claim, then holds source/document locks across
    the provider request and rechecks privacy policy immediately before send.
    Policy/capability blocks with a saved dependency fingerprint can be requeued
    after recovery detects a changed dependency. Local-only blocks have no
    fingerprint and remain parked at ``datetime.max``; bounded-input blocks are
    also blocked without dependency requeue. Transport and general failures use
    the bounded work retry policy. Graph writes roll back if final lease
    acknowledgement no longer belongs to this worker.
    """
    factory = _factory(ctx)
    settings = cast(Settings, ctx["settings"])
    multi_workspace_enabled = settings.multi_workspace_enabled
    redis = cast(Redis, ctx["redis"])
    work_id = UUID(work_id_value)
    lease_owner = hashlib.sha256(f"{work_id}:{datetime.now(UTC).isoformat()}".encode()).hexdigest()[:48]
    dependency_fingerprint: str | None = None
    async with factory() as session:
        workspace_id = await session.scalar(select(EntityExtractionWork.workspace_id).where(
            EntityExtractionWork.id == work_id,
        ))
        if workspace_id is None:
            return
        scope = await _workspace_job_scope(
            session, workspace_id, multi_workspace_enabled=multi_workspace_enabled,
        )
        if scope is None or not await settings_public.module_is_enabled(
            session, "knowledge.entities", scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        ):
            return
        work = await entities.claim_extraction_work(
            session, work_id, lease_owner, datetime.now(UTC), scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if work is None:
            await session.commit()
            return
        version_id = work.document_version_id
        captured_generation = work.source_generation
        await session.commit()

    try:
        async with factory() as session:
            access_fence = await workspaces.read_access_fence(
                session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
            ready = await documents.get_ready_version_ref(
                session, version_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
            if ready is None:
                await entities.set_extraction_work_error(session, work_id, lease_owner, "document_unavailable", blocked=True,
                    scope=scope, multi_workspace_enabled=multi_workspace_enabled)
                await session.commit()
                return
            expected_document_id = ready.document_id
            source = await sources.lock_source(
                session, ready.source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                expected_access_fence=access_fence,
            )
            if source is None or source.status != "active" or source.generation != captured_generation:
                await entities.set_extraction_work_error(session, work_id, lease_owner, "source_generation_changed", blocked=True,
                    scope=scope, multi_workspace_enabled=multi_workspace_enabled)
                await session.commit()
                return
            if not await documents.lock_document_for_extraction(
                session, ready.document_id, ready.source_id, scope=scope,
                multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
                source_fence=source, expected_raw_uri=ready.raw_uri, expected_mime_type=ready.mime_type,
            ):
                await entities.set_extraction_work_error(session, work_id, lease_owner, "document_unavailable", blocked=True,
                    scope=scope, multi_workspace_enabled=multi_workspace_enabled)
                await session.commit()
                return
            # Keep source/document fences while remote work runs so deletion or generation changes cannot race publication.
            ready = await documents.get_ready_version_ref(
                session, version_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
            if (
                ready is None or ready.document_id != expected_document_id
                or ready.source_id != source.id or ready.source_generation != captured_generation
            ):
                await entities.set_extraction_work_error(session, work_id, lease_owner, "document_unavailable", blocked=True,
                    scope=scope, multi_workspace_enabled=multi_workspace_enabled)
                await session.commit()
                return
            if source.local_only:
                await entities.set_extraction_work_error(session, work_id, lease_owner, "local_only_source", blocked=True,
                    scope=scope, multi_workspace_enabled=multi_workspace_enabled)
                await session.commit()
                return
            data = await documents.read_extraction_input(
                session, version_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
            if data is None or data.source_generation != source.generation or data.source_generation != captured_generation:
                await entities.set_extraction_work_error(session, work_id, lease_owner, "document_unavailable", blocked=True,
                    scope=scope, multi_workspace_enabled=multi_workspace_enabled)
                await session.commit()
                return
            config = await settings_public.get_ai_execution_config(session, settings, redis, scope=scope)
            alias = EXTRACTION_ALIAS
            mapping = config.aliases.get(alias)
            destination = config.endpoint_destination_id
            dependency_fingerprint, _ = await _dependency_snapshot(config, redis, scope=scope)
            policy = RequestPolicy(
                reasoning_allowed=config.privacy.allow_remote_reasoning,
                local_only=source.local_only,
                permitted_destinations=frozenset({destination}) if destination else frozenset(),
                reasoning_destinations=frozenset(config.privacy.reasoning_destinations),
                configuration_revision=config.configuration_revision,
            )
            if not _policy_allows_extraction(config, mapping, destination, source.local_only):
                await entities.set_extraction_work_error(
                    session, work_id, lease_owner, "ai_policy_denied", blocked=True,
                    dependency_fingerprint=dependency_fingerprint, scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled,
                )
                await session.commit()
                return

            async def before_send() -> None:
                """Recheck saved settings and privacy policy immediately before remote use."""
                async with factory() as current_session:
                    current = await settings_public.get_ai_execution_config(
                        current_session, settings, redis, scope=scope,
                    )
                    current_mapping = current.aliases.get(alias)
                    current_destination = current.endpoint_destination_id
                    if (
                        current.configuration_revision != config.configuration_revision
                        or current.gateway_identity != config.gateway_identity
                        or current_destination != destination or current_mapping != mapping
                        or current.endpoint_policy_denied or not current_destination
                    ):
                        raise PrivacyPolicyDenied("Extraction policy changed before send")
                    current_policy = RequestPolicy(
                        reasoning_allowed=current.privacy.allow_remote_reasoning,
                        permitted_destinations=frozenset({current_destination}),
                        reasoning_destinations=frozenset(current.privacy.reasoning_destinations),
                        configuration_revision=current.configuration_revision,
                    )
                    if not may_send(
                        current_policy, alias, current_mapping, current_destination,
                        current.omniroute_credential_configured, "structured",
                    ):
                        raise PrivacyPolicyDenied("Extraction policy denied before send")
                current_ready = await documents.get_ready_version_ref(
                    session, version_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                )
                if (
                    current_ready is None or current_ready.document_id != data.document_id
                    or current_ready.source_id != data.source_id
                    or current_ready.source_generation != captured_generation
                ):
                    raise PrivacyPolicyDenied("Extraction source revision changed before send")

            gateway = ModelGateway(
                redis=redis, base_url=config.omniroute_base_url, api_key=config.omniroute_api_key,
                destination_id=cast(str, destination), timeout_seconds=min(config.request_timeout_seconds, EXTRACTION_TIMEOUT_SECONDS),
                gateway_identity=config.gateway_identity, before_send=before_send,
                approved_endpoint_cidrs=config.endpoint_allowed_cidrs,
            )
            response = await gateway.structured(
                alias, mapping, policy,
                extraction_messages([(chunk.id, chunk.content) for chunk in data.chunks]),
                response_schema(), probe=False,
            )
            if not isinstance(response, dict):
                raise ValueError("invalid_model_response")  # noqa: TRY004  # ValueError is part of the contract; TypeError would change behavior
            extracted, actual_model, usage = response_content(response)
            current_ready = await documents.get_ready_version_ref(
                session, version_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
            if (
                current_ready is None or current_ready.document_id != data.document_id
                or current_ready.source_id != data.source_id
                or current_ready.source_generation != captured_generation
            ):
                raise ValueError("document_unavailable")
            allowed_chunks = {chunk.id for chunk in data.chunks}
            if any(not set(item.chunk_ids) <= allowed_chunks for item in extracted.entities):
                raise ValueError("unknown_evidence_chunk")
            if any(item.chunk_id not in allowed_chunks for item in extracted.relationships):
                raise ValueError("unknown_evidence_chunk")
            if len({item.key for item in extracted.entities}) != len(extracted.entities):
                raise ValueError("duplicate_candidate_key")
            if any(item.source_key == item.target_key or item.source_key not in {e.key for e in extracted.entities} or item.target_key not in {e.key for e in extracted.entities} for item in extracted.relationships):
                raise ValueError("invalid_relationship_endpoint")
            membership_chunk_ids = sorted({chunk_id for item in extracted.entities for chunk_id in item.chunk_ids}, key=str)
            evidence_refs = await documents.read_extraction_evidence_refs(
                session, document_id=data.document_id, document_version_id=version_id,
                source_id=data.source_id, source_generation=captured_generation,
                chunk_ids=membership_chunk_ids,
                scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            ) if membership_chunk_ids else []
            if evidence_refs is None:
                raise ValueError("document_unavailable")
            evidence_by_chunk = {ref.chunk_id: ref for ref in evidence_refs}
            candidate_names_by_type: dict[str, list[str]] = {}
            for candidate in extracted.entities:
                candidate_names_by_type.setdefault(candidate.type, []).append(candidate.name)
            known_cache: dict[str, tuple[list[dict[str, object]], bool]] = {}
            for entity_type, names in candidate_names_by_type.items():
                known_cache[entity_type] = await entities.list_resolution_candidates(
                    session, entity_type, names, scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled,
                )
            resolution_plan: dict[str, tuple[str, str | None, list[str], str | None]] = {}
            candidate_by_key = {candidate.key: candidate for candidate in extracted.entities}
            for candidate in extracted.entities:
                known, overflow = known_cache[candidate.type]
                if overflow:
                    resolution_plan[candidate.key] = ("review", None, [], "resolution_context_bounded")
                    continue
                resolution, match_id, possible = resolve_candidate(candidate.name, candidate.type, known)
                resolution_plan[candidate.key] = (
                    resolution, match_id, possible,
                    "ambiguous_identity" if resolution == "review" else None,
                )

            # Pre-resolve intra-response near-duplicates so resolve-then-lock
            # cannot create two new rows for the same/ambiguous model identity.
            for index, left in enumerate(extracted.entities):
                left_resolution, left_id, _left_possible, _left_reason = resolution_plan[left.key]
                for right in extracted.entities[index + 1:]:
                    if left.type != right.type:
                        continue
                    right_resolution, right_id, _right_possible, _right_reason = resolution_plan[right.key]
                    if left_resolution not in {"new", "matched"} or right_resolution not in {"new", "matched"}:
                        continue
                    similarity = SequenceMatcher(
                        None, canonicalize_name(left.name), canonicalize_name(right.name)
                    ).ratio()
                    if similarity < 0.78:
                        continue
                    if left_resolution == "new" and right_resolution == "new":
                        resolution_plan[left.key] = ("review", None, [], "ambiguous_identity")
                        resolution_plan[right.key] = ("review", None, [], "ambiguous_identity")
                    elif left_resolution == "new" and right_resolution == "matched":
                        resolution_plan[left.key] = ("review", None, [right_id] if right_id else [], "ambiguous_identity")
                    elif left_resolution == "matched" and right_resolution == "new":
                        resolution_plan[right.key] = ("review", None, [left_id] if left_id else [], "ambiguous_identity")

            match_fingerprints = {
                candidate.key: candidate_match_fingerprint(candidate.name, candidate.type)
                for candidate in extracted.entities
            }
            candidate_bindings = {
                candidate.key: (match_fingerprints[candidate.key], set(candidate.chunk_ids))
                for candidate in extracted.entities
            }
            initial_decisions = await entities.get_document_correction_decisions(
                session, data.document_id, version_id, candidate_bindings,
                scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
            canonical_decisions: dict[str, tuple[UUID, str, UUID | None] | None] = {}
            for candidate in extracted.entities:
                fingerprint = match_fingerprints[candidate.key]
                decision = initial_decisions.get(candidate.key)
                if decision is None:
                    canonical_decisions[candidate.key] = None
                    continue
                decision_id, action, target_id = decision
                if target_id is not None:
                    try:
                        target_id = await entities.resolve_canonical_entity_id(
                            session, target_id, scope=scope,
                            multi_workspace_enabled=multi_workspace_enabled,
                        )
                    except (LookupError, ValueError):
                        action, target_id = "conflict", None
                canonical = (decision_id, action, target_id)
                canonical_decisions[candidate.key] = canonical
                if action == "suppress":
                    resolution_plan[candidate.key] = ("review", None, [], "owner_suppressed_candidate")
                elif action == "assign" and target_id is not None:
                    resolution_plan[candidate.key] = ("matched", str(target_id), [], None)
                elif action == "conflict":
                    resolution_plan[candidate.key] = ("review", None, [], "conflicting_owner_corrections")

            existing_ids = sorted({
                UUID(match_id) for resolution, match_id, _, _ in resolution_plan.values()
                if resolution == "matched" and match_id is not None
            }, key=str)
            locked_refs = {}
            if existing_ids:
                try:
                    refs = await entities.get_entity_refs(
                        session, existing_ids, for_write=True, scope=scope,
                        multi_workspace_enabled=multi_workspace_enabled,
                    )
                    locked_refs = {ref.requested_id: ref for ref in refs}
                except (LookupError, ValueError):
                    for key, (resolution, match_id, possible, reason) in list(resolution_plan.items()):
                        if resolution == "matched":
                            resolution_plan[key] = ("review", None, possible + ([match_id] if match_id else []), "identity_changed_during_resolution")

            # Refresh names, types, revisions and exact confirmed aliases after
            # the stable sorted entity lock closure. A changed outcome becomes
            # review-only; never acquire a late entity lock out of order.
            refreshed: dict[str, tuple[list[dict[str, object]], bool]] = {}
            for entity_type, names in candidate_names_by_type.items():
                refreshed[entity_type] = await entities.list_resolution_candidates(
                    session, entity_type, names, scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled,
                )
            current_decisions = await entities.get_document_correction_decisions(
                session, data.document_id, version_id, candidate_bindings, for_update=True,
                scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
            for candidate in extracted.entities:
                old_resolution, old_id, _old_possible, old_reason = resolution_plan[candidate.key]
                if old_reason == "resolution_context_bounded":
                    continue
                known, overflow = refreshed[candidate.type]
                if overflow:
                    resolution_plan[candidate.key] = ("review", None, [], "resolution_context_bounded")
                    continue
                decision = current_decisions.get(candidate.key)
                current_owner_decision: tuple[UUID, str, UUID | None] | None = None
                if decision is not None:
                    decision_id, action, target_id = decision
                    if target_id is not None:
                        try:
                            target_id = await entities.resolve_canonical_entity_id(
                                session, target_id, scope=scope,
                                multi_workspace_enabled=multi_workspace_enabled,
                            )
                        except (LookupError, ValueError):
                            target_id = None
                    current_owner_decision = (decision_id, action, target_id)
                if current_owner_decision != canonical_decisions[candidate.key]:
                    resolution_plan[candidate.key] = ("review", None, [], "identity_changed_during_resolution")
                    continue
                current_resolution, current_id, possible = resolve_candidate(candidate.name, candidate.type, known)
                owner_decision = canonical_decisions[candidate.key]
                if owner_decision is not None:
                    _, action, assigned_id = owner_decision
                    if action == "suppress":
                        resolution_plan[candidate.key] = ("review", None, [], "owner_suppressed_candidate")
                        continue
                    if action != "assign" or assigned_id is None or (
                        current_resolution == "matched" and current_id != str(assigned_id)
                    ):
                        resolution_plan[candidate.key] = ("review", None, possible, "conflicting_owner_corrections")
                        continue
                    current_resolution, current_id = "matched", str(assigned_id)
                if old_resolution == "review":
                    resolution_plan[candidate.key] = (
                        "review", None, possible, old_reason or "ambiguous_identity"
                    )
                    continue
                if current_resolution != old_resolution or current_id != old_id:
                    ids = possible + ([current_id] if current_id else []) + ([old_id] if old_id else [])
                    resolution_plan[candidate.key] = (
                        "review", None, sorted(set(ids)), "identity_changed_during_resolution"
                    )
                    continue
                if current_resolution == "matched":
                    identifier = UUID(current_id) if current_id else None
                    current = next((item for item in known if str(item["id"]) == current_id), None)
                    locked = locked_refs.get(identifier) if identifier else None
                    if (
                        current is None or locked is None or locked.type != candidate.type
                        or locked.revision != current.get("revision")
                    ):
                        resolution_plan[candidate.key] = (
                            "review", None, [current_id] if current_id else [],
                            "identity_changed_during_resolution",
                        )
                        continue
                resolution_plan[candidate.key] = (current_resolution, current_id, possible, None)

            key_to_entity: dict[str, UUID] = {}
            membership_for: dict[tuple[str, UUID], UUID] = {}
            review: list[dict[str, object]] = []
            facts: list[dict[str, object]] = []
            for candidate in extracted.entities:
                resolution, match_id, possible, reason = resolution_plan[candidate.key]
                if resolution == "review":
                    candidate_key = hashlib.sha256(f"{candidate.type}:{candidate.key}".encode()).hexdigest()
                    fingerprint = match_fingerprints[candidate.key]
                    snapshot = {
                        "kind": "entity", "candidate_id": str(uuid4()),
                        "candidate_type": candidate.type, "candidate_name": candidate.name,
                        "candidate_key": candidate_key, "match_fingerprint": fingerprint,
                        "chunk_ids": [str(identifier) for identifier in candidate.chunk_ids],
                        "confidence": candidate.confidence,
                    }
                    snapshot["snapshot_digest"] = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                    review.append({
                        **snapshot,
                        "reason": reason or "ambiguous_identity",
                        "possible_entity_ids": possible,
                    })
                    continue
                new_entity = resolution == "new"
                entity_id = UUID(match_id) if match_id else await entities.create_extracted_entity(
                    session, candidate.type, scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled,
                )
                key_to_entity[candidate.key] = entity_id
                candidate_key = hashlib.sha256(f"{candidate.type}:{candidate.key}".encode()).hexdigest()
                for chunk_id in candidate.chunk_ids:
                    membership_id = await entities.record_extraction_membership(
                        session, entity_id=entity_id, evidence_ref=evidence_by_chunk[chunk_id],
                        source_generation=captured_generation,
                        extraction_identity=str(work_id), candidate_key=candidate_key,
                        match_fingerprint=match_fingerprints[candidate.key],
                        observed_at=data.observed_at, confidence=candidate.confidence,
                        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                    )
                    membership_for[(candidate.key, chunk_id)] = membership_id
                    if new_entity:
                        await entities.publish_derived_field(
                            session, entity_id=entity_id, membership_id=membership_id,
                            field_name="name", value=candidate.name, scope=scope,
                            multi_workspace_enabled=multi_workspace_enabled,
                        )
                        if candidate.description:
                            await entities.publish_derived_field(
                                session, entity_id=entity_id, membership_id=membership_id,
                                field_name="description", value=candidate.description, scope=scope,
                                multi_workspace_enabled=multi_workspace_enabled,
                            )
                facts.append({"entity_id": str(entity_id), "candidate_key": candidate.key, "confidence": candidate.confidence})
            def retain_relationship_review(relation: ExtractedRelationship, reason: str, possible: list[str]) -> None:
                """Snapshot unresolved relationship endpoints for later owner review."""
                source_candidate = candidate_by_key[relation.source_key]
                target_candidate = candidate_by_key[relation.target_key]
                snapshot = {
                    "kind": "relationship", "candidate_id": str(uuid4()),
                    "candidate_name": relation.type, "relationship_type": relation.type,
                    "source_candidate_key": hashlib.sha256(f"{source_candidate.type}:{source_candidate.key}".encode()).hexdigest(),
                    "target_candidate_key": hashlib.sha256(f"{target_candidate.type}:{target_candidate.key}".encode()).hexdigest(),
                    "chunk_id": str(relation.chunk_id), "confidence": relation.confidence,
                }
                snapshot["snapshot_digest"] = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                review.append({**snapshot, "reason": reason, "possible_entity_ids": possible})

            for relation in extracted.relationships:
                source_id, target_id = key_to_entity.get(relation.source_key), key_to_entity.get(relation.target_key)
                if source_id is None or target_id is None:
                    retain_relationship_review(relation, "relationship_endpoint_requires_review", [])
                    continue
                source_membership = membership_for.get((relation.source_key, relation.chunk_id))
                target_membership = membership_for.get((relation.target_key, relation.chunk_id))
                if source_membership is None or target_membership is None or source_id == target_id:
                    retain_relationship_review(relation, "relationship_identity_ambiguous", [str(source_id), str(target_id)])
                    continue
                relationship_id = await relationships.publish_extracted_relationship(
                    session, source_entity_id=source_id, target_entity_id=target_id,
                    relationship_type=relation.type, document_version_id=version_id,
                    chunk_id=relation.chunk_id, source_membership_id=source_membership,
                    target_membership_id=target_membership, confidence=relation.confidence,
                    scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                )
                if relationship_id is not None:
                    facts.append({"relationship_id": str(relationship_id), "relationship": relation.type, "source_entity_id": str(source_id), "target_entity_id": str(target_id), "chunk_id": str(relation.chunk_id), "confidence": relation.confidence})
            assert mapping is not None
            if await entities.finish_extraction_work(
                session, work_id, lease_owner, facts=facts, review=review,
                model=actual_model or mapping.model, usage=usage, scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            ):
                await commit_with_replay(
                    session,
                    [make_graph_change(entity_id=entity_id, scope=scope)
                     for entity_id in sorted(set(key_to_entity.values()), key=str)],
                    scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                    access_fence=access_fence,
                )
            else:
                await session.rollback()
    except (PrivacyPolicyDenied, CapabilityUnsupported) as exc:
        async with factory() as session:
            await entities.set_extraction_work_error(
                session, work_id, lease_owner,
                "ai_policy_denied" if isinstance(exc, PrivacyPolicyDenied) else "structured_unsupported",
                blocked=True, dependency_fingerprint=dependency_fingerprint,
                scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
            await session.commit()
    except ModelGatewayError as exc:
        blocked = str(exc) == "Model capability is not supported"
        error_code = "structured_unsupported" if blocked else "model_transport_unavailable"
        async with factory() as session:
            await entities.set_extraction_work_error(
                session, work_id, lease_owner, error_code, blocked=blocked,
                dependency_fingerprint=dependency_fingerprint if blocked else None,
                scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
            await session.commit()
        logger.warning("Entity extraction deferred work_id=%s error_code=%s", work_id, error_code)
    except Exception as exc:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
        blocked = str(exc) in {"Extraction input exceeds its chunk or byte limit"}
        error_code = "extraction_input_limit" if blocked else str(exc) if str(exc) in {
            "invalid_model_response", "unknown_evidence_chunk", "duplicate_candidate_key",
            "invalid_relationship_endpoint", "document_unavailable",
        } else "extraction_failed"
        async with factory() as session:
            await entities.set_extraction_work_error(
                session, work_id, lease_owner, error_code, blocked=blocked,
                scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
            await session.commit()
        logger.warning("Entity extraction failed work_id=%s error_code=%s", work_id, error_code)
