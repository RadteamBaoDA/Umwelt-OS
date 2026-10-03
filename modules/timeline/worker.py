"""Durable document-event extraction consumer and recovery loop."""

import hashlib
import json
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from arq.connections import ArqRedis
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.heavy_work import bounded_heavy_work
from core.model_gateway.cache import capability_key
from core.model_gateway.client import CapabilityUnsupported, ModelGateway, ModelGatewayError, PrivacyPolicyDenied
from core.model_gateway.policy import may_send
from core.model_gateway.schemas import CapabilityResult, RequestPolicy
from core.realtime import commit_with_replay, make_timeline_change
from modules.knowledge.documents import public as documents
from modules.knowledge.entities import public as entities
from modules.settings import public as settings_public
from modules.sources import public as sources
from modules.timeline import public as timeline
from modules.timeline.extraction import (
    EXTRACTOR_VERSION, PROMPT_VERSION, extraction_messages, response_content, response_schema,
)
from modules.timeline.models import Event, TimelineExtractionWork

ALIAS = "reasoning-small"
TIMEOUT_SECONDS = 35


def _factory(ctx: dict[str, object]) -> async_sessionmaker[AsyncSession]:
    """Resolve the worker's configured asynchronous session factory."""
    return cast(async_sessionmaker[AsyncSession], ctx["session_factory"])


async def _dependency_snapshot(config: object, redis: Redis) -> tuple[str, bool]:
    """Fingerprint nonsecret extraction policy and verify a current structured capability result."""
    mapping = getattr(config, "aliases").get(ALIAS)
    key = capability_key(ALIAS, mapping.model, mapping.version, "structured", getattr(config, "gateway_identity")) if mapping else None
    raw = await redis.get(key) if key else None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else None
        capability = CapabilityResult.model_validate(parsed) if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, ValueError):
        capability = None
    try:
        expires_at = datetime.fromisoformat(capability.expires_at) if capability else None
        fresh = bool(expires_at and expires_at > datetime.now(UTC))
    except ValueError:
        fresh = False
    supported = bool(
        fresh and capability.result == "supported" and capability.alias == ALIAS
        and capability.capability == "structured" and capability.gateway_identity == getattr(config, "gateway_identity")
        and mapping is not None and capability.model == mapping.model
        and capability.version == mapping.version
        and capability.configuration_revision == getattr(config, "configuration_revision")
    )
    privacy = getattr(config, "privacy")
    values = {
        "revision": getattr(config, "configuration_revision"),
        "gateway": getattr(config, "gateway_identity"),
        "destination": getattr(config, "endpoint_destination_id"),
        "denied": getattr(config, "endpoint_policy_denied"),
        "credential": getattr(config, "omniroute_credential_configured"),
        "mapping": (mapping.model, mapping.version) if mapping else None,
        "remote": privacy.allow_remote_reasoning,
        "destinations": sorted(privacy.reasoning_destinations),
        "capability": capability.model_dump(mode="json") if capability else None,
    }
    fingerprint = hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    return fingerprint, supported


def _policy(config: object, mapping: object, destination: str | None, local_only: bool) -> RequestPolicy:
    """Build the current structured extraction destination/privacy policy."""
    privacy = getattr(config, "privacy")
    return RequestPolicy(
        reasoning_allowed=privacy.allow_remote_reasoning,
        local_only=local_only,
        permitted_destinations=frozenset({destination}) if destination else frozenset(),
        reasoning_destinations=frozenset(privacy.reasoning_destinations),
        configuration_revision=getattr(config, "configuration_revision"),
    )


def _allowed(config: object, mapping: object, destination: str | None, local_only: bool) -> bool:
    """Check the selected alias, credential, destination, and privacy gates."""
    if mapping is None or not destination or getattr(config, "endpoint_policy_denied"):
        return False
    return may_send(
        _policy(config, mapping, destination, local_only), ALIAS, mapping, destination,
        getattr(config, "omniroute_credential_configured"), "structured",
    )


@bounded_heavy_work
async def process_timeline_extraction_work(ctx: dict[str, object], work_id_value: str) -> None:
    """Run one durable extraction lease while holding source/document egress fences through publication.

    The model call uses the existing two-slot ModelGateway and the worker's
    shared PostgreSQL heavy slot. Work status and replay omit source text; results
    retain only bounded structured proposals and exact support IDs, never raw
    source chunks.
    """
    factory = _factory(ctx)
    redis = cast(Redis, ctx["redis"])
    settings = cast(Settings, ctx["settings"])
    work_id = UUID(work_id_value)
    lease_owner = hashlib.sha256(f"{work_id}:{datetime.now(UTC).isoformat()}".encode()).hexdigest()[:48]
    dependency_fingerprint: str | None = None
    async with factory() as session:
        work = await timeline.claim_extraction_work(session, work_id, lease_owner, datetime.now(UTC))
        if work is None:
            await session.commit()
            return
        version_id, generation = work.document_version_id, work.source_generation
        await session.commit()
    try:
        async with factory() as session:
            locator = await documents.get_ready_version_ref(session, version_id)
            if locator is None or locator.source_generation != generation:
                await timeline.set_extraction_work_error(session, work_id, lease_owner, "document_unavailable", blocked=True)
                await session.commit()
                return
            source = await sources.lock_source(session, locator.source_id)
            if source is None or source.status != "active" or source.generation != generation:
                await timeline.set_extraction_work_error(session, work_id, lease_owner, "source_generation_changed", blocked=True)
                await session.commit()
                return
            if not await documents.lock_document_for_extraction(session, locator.document_id, source.id):
                await timeline.set_extraction_work_error(session, work_id, lease_owner, "document_unavailable", blocked=True)
                await session.commit()
                return
            # Keep this transaction's source/document locks across inference and publication.
            ready = await documents.get_ready_version_ref(session, version_id)
            if ready is None or ready.source_generation != generation or ready.document_id != locator.document_id:
                await timeline.set_extraction_work_error(session, work_id, lease_owner, "document_unavailable", blocked=True)
                await session.commit()
                return
            if source.local_only:
                await timeline.set_extraction_work_error(session, work_id, lease_owner, "local_only_source", blocked=True)
                await session.commit()
                return
            data = await documents.read_extraction_input(session, version_id)
            if data is None or data.source_generation != generation:
                await timeline.set_extraction_work_error(session, work_id, lease_owner, "document_unavailable", blocked=True)
                await session.commit()
                return
            try:
                membership_refs = await entities.list_version_membership_refs(
                    session, version_id, [item.id for item in data.chunks],
                )
            except ValueError:
                await timeline.set_extraction_work_error(session, work_id, lease_owner, "membership_context_bounded", blocked=True)
                await session.commit()
                return
            config = await settings_public.get_ai_execution_config(session, settings, redis)
            mapping = config.aliases.get(ALIAS)
            destination = config.endpoint_destination_id
            fingerprint, capability_supported = await _dependency_snapshot(config, redis)
            dependency_fingerprint = fingerprint
            if not capability_supported:
                await timeline.set_extraction_work_error(
                    session, work_id, lease_owner, "structured_unsupported", blocked=True,
                    dependency_fingerprint=fingerprint,
                )
                await session.commit()
                return
            if not _allowed(config, mapping, destination, source.local_only):
                await timeline.set_extraction_work_error(
                    session, work_id, lease_owner, "ai_policy_denied", blocked=True,
                    dependency_fingerprint=fingerprint,
                )
                await session.commit()
                return

            async def before_send() -> None:
                """Recheck destination, configuration revision, capability, and policy immediately before egress."""
                async with factory() as check_session:
                    current = await settings_public.get_ai_execution_config(check_session, settings, redis)
                    current_mapping = current.aliases.get(ALIAS)
                    current_destination = current.endpoint_destination_id
                    current_fingerprint, current_supported = await _dependency_snapshot(current, redis)
                    if (
                        current.configuration_revision != config.configuration_revision
                        or current.gateway_identity != config.gateway_identity
                        or current_mapping != mapping or current_destination != destination
                        or not current_supported or current_fingerprint != fingerprint
                        or not _allowed(current, current_mapping, current_destination, source.local_only)
                    ):
                        raise PrivacyPolicyDenied("Timeline extraction policy changed before send")
                current_ready = await documents.get_ready_version_ref(session, version_id)
                if (
                    current_ready is None or current_ready.document_id != data.document_id
                    or current_ready.source_id != source.id
                    or current_ready.source_generation != generation
                ):
                    raise PrivacyPolicyDenied("Timeline extraction source revision changed before send")

            gateway = ModelGateway(
                redis=redis, base_url=config.omniroute_base_url, api_key=config.omniroute_api_key,
                destination_id=destination, timeout_seconds=min(config.request_timeout_seconds, TIMEOUT_SECONDS),
                gateway_identity=config.gateway_identity, before_send=before_send,
                approved_endpoint_cidrs=config.endpoint_allowed_cidrs,
            )
            response = await gateway.structured(
                ALIAS, mapping, _policy(config, mapping, destination, source.local_only),
                extraction_messages(
                    [(item.id, item.content) for item in data.chunks],
                    [item.model_dump(mode="json", exclude={"name", "entity_id", "entity_revision", "observed_at"}) for item in membership_refs],
                ),
                response_schema(), probe=False,
            )
            if not isinstance(response, dict):
                raise ValueError("invalid_model_response")
            proposals, model = response_content(response)
            async with factory() as check_session:
                current_config = await settings_public.get_ai_execution_config(check_session, settings, redis)
                current_mapping = current_config.aliases.get(ALIAS)
                current_destination = current_config.endpoint_destination_id
                current_fingerprint, current_supported = await _dependency_snapshot(current_config, redis)
                if (
                    current_config.configuration_revision != config.configuration_revision
                    or current_config.gateway_identity != config.gateway_identity
                    or current_mapping != mapping or current_destination != destination
                    or not current_supported or current_fingerprint != fingerprint
                    or not _allowed(current_config, current_mapping, current_destination, source.local_only)
                ):
                    # Keep the attempted-request fingerprint so recovery sees
                    # a changed, already-permitted configuration as retryable.
                    raise PrivacyPolicyDenied("Timeline extraction policy changed before publication")
            current_ready = await documents.get_ready_version_ref(session, version_id)
            if (
                current_ready is None or current_ready.document_id != data.document_id
                or current_ready.source_generation != generation or current_ready.source_id != source.id
            ):
                raise ValueError("document_unavailable")
            if not await timeline.publish_extracted_events(
                session, work_id=work_id, lease_owner=lease_owner,
                ready=data, proposals=proposals, model=model,
                membership_revisions={item.membership_id: item.entity_revision for item in membership_refs},
            ):
                await session.rollback()
                return
            from modules.timeline.models import TimelineExtractionResult
            result = await session.scalar(select(TimelineExtractionResult).where(
                TimelineExtractionResult.work_id == work_id
            ))
            event_ids = [UUID(str(item["event_id"])) for item in result.proposals_json] if result else []
            event_revisions = dict((await session.execute(select(Event.id, Event.revision).where(
                Event.id.in_(event_ids)
            ))).all()) if event_ids else {}
            await commit_with_replay(session, [
                make_timeline_change(event_id, event_revisions[event_id])
                for event_id in sorted(set(event_ids), key=str) if event_id in event_revisions
            ])
    except (CapabilityUnsupported, PrivacyPolicyDenied) as exc:
        async with factory() as session:
            await timeline.set_extraction_work_error(
                session, work_id, lease_owner,
                "structured_unsupported" if isinstance(exc, CapabilityUnsupported) else "ai_policy_denied",
                blocked=True, dependency_fingerprint=dependency_fingerprint,
            )
            await session.commit()
    except (ModelGatewayError, ValueError, LookupError) as exc:
        async with factory() as session:
            await timeline.set_extraction_work_error(
                session, work_id, lease_owner, type(exc).__name__.lower(),
            )
            await session.commit()


async def recover_timeline_extraction_work(ctx: dict[str, object]) -> int:
    """Rescan ready versions and requeue a bounded page of durable due timeline work.

    PostgreSQL uniqueness is the recovery authority; the Redis cursor only
    avoids repeatedly scanning the first ready-document page. Policy-blocked
    work is rechecked on a finite schedule and requeued only when its saved
    attempted-request fingerprint changed and current source, capability, and
    privacy fences permit another request. A stale response therefore cannot
    make its replacement configuration look already attempted.
    """
    factory = _factory(ctx)
    redis = cast(ArqRedis, ctx["redis"])
    count = 0
    raw_cursor = await redis.get("bbd:timeline-extraction:ready-cursor")
    cursor = raw_cursor.decode() if isinstance(raw_cursor, bytes) else raw_cursor if isinstance(raw_cursor, str) else None
    if cursor is not None and len(cursor) > 512:
        cursor = None
    async with factory() as session:
        refs, next_cursor = await documents.list_ready_version_refs(session, limit=25, cursor=cursor)
        await session.commit()
    for ref in refs:
        async with factory() as session:
            source = await sources.lock_source(session, ref.source_id)
            current = await documents.get_ready_version_ref(session, ref.document_version_id)
            if (
                source is None or source.status != "active" or current is None
                or current.document_id != ref.document_id or current.source_id != source.id
                or current.source_generation != source.generation
                or current.source_generation != ref.source_generation
            ):
                await session.commit()
                continue
            if not await documents.lock_document_for_extraction(session, current.document_id, source.id):
                await session.commit()
                continue
            current = await documents.get_ready_version_ref(session, ref.document_version_id)
            if current is None or current.source_generation != source.generation:
                await session.commit()
                continue
            work_id = await timeline.schedule_extraction_work(
                session, current, EXTRACTOR_VERSION, PROMPT_VERSION,
            )
            if current.local_only:
                work = await session.get(TimelineExtractionWork, work_id, with_for_update=True)
                if work is not None and work.status != "succeeded":
                    work.status, work.error_code = "blocked", "local_only_source"
                    work.next_attempt_at = datetime.max.replace(tzinfo=UTC)
                    work.lease_owner = work.lease_expires_at = None
            await session.commit()
    if next_cursor:
        await redis.set("bbd:timeline-extraction:ready-cursor", next_cursor)
    else:
        await redis.delete("bbd:timeline-extraction:ready-cursor")
    requeued_ids: list[UUID] = []
    async with factory() as session:
        blocked_items = await timeline.list_blocked_extraction_work(session, limit=25)
    for blocked_id, version_id, captured_generation, error_code, old_fingerprint in blocked_items:
        if error_code not in {"ai_policy_denied", "structured_unsupported"} or not old_fingerprint:
            continue
        async with factory() as session:
            ready = await documents.get_ready_version_ref(session, version_id)
            source = await sources.lock_source(session, ready.source_id) if ready is not None else None
            current = await documents.get_ready_version_ref(session, version_id) if ready is not None else None
            if (
                source is None or source.status != "active" or source.local_only
                or source.generation != captured_generation or current is None
                or current.document_id != ready.document_id or current.source_id != source.id
                or current.source_generation != captured_generation
            ):
                await timeline.defer_blocked_extraction_recheck(session, blocked_id, old_fingerprint)
                await session.commit()
                continue
            config = await settings_public.get_ai_execution_config(
                session, cast(Settings, ctx["settings"]), redis,
            )
            mapping = config.aliases.get(ALIAS)
            fingerprint, capability_supported = await _dependency_snapshot(config, redis)
            if (
                capability_supported and _allowed(config, mapping, config.endpoint_destination_id, source.local_only)
                and await timeline.requeue_blocked_extraction_work(
                    session, blocked_id, old_fingerprint, fingerprint,
                )
            ):
                requeued_ids.append(blocked_id)
            else:
                await timeline.defer_blocked_extraction_recheck(session, blocked_id, old_fingerprint)
            await session.commit()
    async with factory() as session:
        work_ids = await timeline.list_recoverable_extraction_work(session, 25)
        await session.commit()
    work_ids = list(dict.fromkeys([*work_ids, *requeued_ids]))
    for work_id in work_ids:
        await redis.enqueue_job(
            "process_timeline_extraction_work", str(work_id),
            _job_id=f"timeline-extraction:{work_id}",
        )
        count += 1
    return count
