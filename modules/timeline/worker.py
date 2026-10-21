"""Durable document-event extraction consumer and recovery loop."""

import hashlib
import json
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from arq.connections import ArqRedis
from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
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
    structured_evidence,
)
from core.model_gateway.policy import may_send
from core.model_gateway.schemas import (
    AIExecutionConfig,
    ModelMapping,
    RequestPolicy,
)
from core.realtime import commit_with_replay, make_timeline_change
from core.worker_cursors import STATE_KEY, read_cursor, write_cursor
from core.worker_queues import queue_for
from core.workspaces import public as workspaces
from core.workspaces.schemas import InternalJobScope, Scope
from modules.knowledge.documents import public as documents
from modules.knowledge.entities import public as entities
from modules.settings import public as settings_public
from modules.settings.prompts import resolve_prompt
from modules.sources import public as sources
from modules.timeline import public as timeline
from modules.timeline.extraction import (
    EXTRACTOR_VERSION,
    PROMPT_VERSION,
    extraction_messages,
    response_content,
    response_schema,
)
from modules.timeline.models import Event, TimelineExtractionWork

ROLE = "background"


def _factory(ctx: dict[str, object]) -> async_sessionmaker[AsyncSession]:
    """Resolve the worker's configured asynchronous session factory."""
    return cast(async_sessionmaker[AsyncSession], ctx["session_factory"])


async def _workspace_job_scope(
    session: AsyncSession, workspace_id: UUID, *, multi_workspace_enabled: bool,
) -> InternalJobScope | None:
    """Resolve an owner-backed scope from the durable extraction workspace binding."""
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
    """Fingerprint nonsecret extraction policy and verify a current structured capability result."""
    mapping = config.defaults.resolve(ROLE)
    # Native structured evidence, or chat evidence for the gateway's prompted-JSON fallback.
    capability = await structured_evidence(
        redis, ROLE, mapping, gateway_identity=config.gateway_identity,
        configuration_revision=config.configuration_revision,
        workspace_id=scope.workspace_id, actor_user_id=config.actor_user_id,
    )
    supported = capability is not None
    privacy = config.privacy
    values = {
        "revision": config.configuration_revision,
        "gateway": config.gateway_identity,
        "destination": config.endpoint_destination_id,
        "denied": config.endpoint_policy_denied,
        "credential": config.gateway_credential_configured,
        "mapping": (mapping.model, mapping.version) if mapping else None,
        "remote": privacy.allow_remote_reasoning,
        "destinations": sorted(privacy.reasoning_destinations),
        "capability": capability.model_dump(mode="json") if capability else None,
    }
    fingerprint = hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    return fingerprint, supported


def _policy(config: AIExecutionConfig, mapping: ModelMapping | None, destination: str | None, local_only: bool) -> RequestPolicy:
    """Build the current structured extraction destination/privacy policy."""
    privacy = config.privacy
    return RequestPolicy(
        workspace_id=config.workspace_id, actor_user_id=config.actor_user_id,
        membership_revision=config.membership_revision, gateway_identity=config.gateway_identity,
        reasoning_allowed=privacy.allow_remote_reasoning,
        local_only=local_only,
        permitted_destinations=frozenset({destination}) if destination else frozenset(),
        reasoning_destinations=frozenset(privacy.reasoning_destinations),
        configuration_revision=config.configuration_revision,
    )


def _allowed(config: AIExecutionConfig, mapping: ModelMapping | None, destination: str | None, local_only: bool) -> bool:
    """Check the selected model role, credential, destination, and privacy gates."""
    if mapping is None or not destination or config.endpoint_policy_denied:
        return False
    return may_send(
        _policy(config, mapping, destination, local_only), ROLE, mapping, destination,
        config.gateway_credential_configured, "structured",
    )


async def process_timeline_extraction_work(ctx: dict[str, object], work_id_value: str) -> None:
    """Run one durable extraction lease: lock -> release -> infer -> re-lock-and-compare -> publish.

    Phase 1 locks workspace/membership/source/document rows only to capture plain DTOs, then
    rolls back before provider I/O so chat's access fence is never blocked for the request's
    duration. Egress is rechecked in ``before_send`` on an independent session, and publication
    re-locks and compares source generation, raw URI and MIME type; any drift raises
    ``ValueError("document_unavailable")`` (retryable) and nothing is published.

    The model call uses the existing two-slot ModelGateway and the worker's
    shared PostgreSQL heavy slot. Work status and replay omit source text; results
    retain only bounded structured proposals and exact support IDs, never raw
    source chunks.
    """
    factory = _factory(ctx)
    redis = cast(Redis, ctx["redis"])
    settings = cast(Settings, ctx["settings"])
    multi_workspace_enabled = settings.multi_workspace_enabled
    work_id = UUID(work_id_value)
    lease_owner = hashlib.sha256(f"{work_id}:{datetime.now(UTC).isoformat()}".encode()).hexdigest()[:48]
    dependency_fingerprint: str | None = None
    async with factory() as session:
        workspace_id = await session.scalar(select(TimelineExtractionWork.workspace_id).where(
            TimelineExtractionWork.id == work_id,
        ))
        if workspace_id is None:
            return
        async def admit() -> tuple[InternalJobScope, TimelineExtractionWork | None] | None:
            await session.rollback()
            job_scope = await _workspace_job_scope(
                session, workspace_id, multi_workspace_enabled=multi_workspace_enabled,
            )
            if job_scope is None or not await settings_public.module_is_enabled(
                session, "knowledge.timeline", scope=job_scope, multi_workspace_enabled=multi_workspace_enabled,
            ):
                # ponytail: untouched work is re-claimed every sweep while the module is disabled;
                # upgrade = ack+requeue on module enable.
                return None  # unavailable lineage or disabled module: leave durable work untouched
            return job_scope, await timeline.claim_extraction_work(
                session, work_id, lease_owner, datetime.now(UTC), scope=job_scope,
                multi_workspace_enabled=multi_workspace_enabled,
                lease_seconds=max(110, settings.background_llm_timeout_seconds + 75),
            )

        try:
            admitted = await admit_retry_stale(admit)
        except HTTPException as exc:
            await session.rollback()
            await terminalize(factory, TimelineExtractionWork, work_id, workspace_id, denial_code(exc) or STALE_SCOPE)
            return
        if admitted is None:
            await session.rollback()
            return
        scope, work = admitted
        if work is None:
            await session.commit()
            return
        version_id, generation = work.document_version_id, work.source_generation
        await session.commit()
    try:
        async with factory() as session:
            access_fence = await workspaces.read_access_fence(
                session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
            locator = await documents.get_ready_version_ref(
                session, version_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
            if locator is None or locator.source_generation != generation:
                await timeline.set_extraction_work_error(session, work_id, lease_owner, "document_unavailable", blocked=True,
                    scope=scope, multi_workspace_enabled=multi_workspace_enabled)
                await session.commit()
                return
            source = await sources.lock_source(session, locator.source_id, scope=scope,
                multi_workspace_enabled=multi_workspace_enabled, expected_access_fence=access_fence)
            if source is None or source.status != "active" or source.generation != generation:
                await timeline.set_extraction_work_error(session, work_id, lease_owner, "source_generation_changed", blocked=True,
                    scope=scope, multi_workspace_enabled=multi_workspace_enabled)
                await session.commit()
                return
            if not await documents.lock_document_for_extraction(
                session, locator.document_id, source.id, scope=scope,
                multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
                source_fence=source, expected_raw_uri=locator.raw_uri, expected_mime_type=locator.mime_type,
            ):
                await timeline.set_extraction_work_error(session, work_id, lease_owner, "document_unavailable", blocked=True,
                    scope=scope, multi_workspace_enabled=multi_workspace_enabled)
                await session.commit()
                return
            # Locks are released before inference and reacquired for publication.
            ready = await documents.get_ready_version_ref(
                session, version_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
            if ready is None or ready.source_generation != generation or ready.document_id != locator.document_id:
                await timeline.set_extraction_work_error(session, work_id, lease_owner, "document_unavailable", blocked=True,
                    scope=scope, multi_workspace_enabled=multi_workspace_enabled)
                await session.commit()
                return
            if source.local_only:
                await timeline.set_extraction_work_error(session, work_id, lease_owner, "local_only_source", blocked=True,
                    scope=scope, multi_workspace_enabled=multi_workspace_enabled)
                await session.commit()
                return
            if not source.graph_enabled:
                # Switch flipped after scheduling: park quietly; intentional owner choice, not an error.
                await timeline.set_extraction_work_error(session, work_id, lease_owner, "graph_disabled", blocked=True,
                    scope=scope, multi_workspace_enabled=multi_workspace_enabled)
                await session.commit()
                return
            data = await documents.read_extraction_input(
                session, version_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
            if data is None or data.source_generation != generation:
                await timeline.set_extraction_work_error(session, work_id, lease_owner, "document_unavailable", blocked=True,
                    scope=scope, multi_workspace_enabled=multi_workspace_enabled)
                await session.commit()
                return
            try:
                membership_refs = await entities.list_version_membership_refs(
                    session, version_id, [item.id for item in data.chunks], scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled,
                )
            except ValueError:
                await timeline.set_extraction_work_error(session, work_id, lease_owner, "membership_context_bounded", blocked=True,
                    scope=scope, multi_workspace_enabled=multi_workspace_enabled)
                await session.commit()
                return
            config = settings_public.derive_for_role(
                await settings_public.get_ai_execution_config(session, settings, redis, scope=scope), ROLE)
            mapping = config.defaults.resolve(ROLE)
            destination = config.endpoint_destination_id
            fingerprint, capability_supported = await _dependency_snapshot(config, redis, scope=scope)
            dependency_fingerprint = fingerprint
            if not capability_supported:
                await timeline.set_extraction_work_error(
                    session, work_id, lease_owner, "structured_unsupported", blocked=True,
                    dependency_fingerprint=fingerprint, scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled,
                )
                await session.commit()
                return
            if not _allowed(config, mapping, destination, source.local_only):
                await timeline.set_extraction_work_error(
                    session, work_id, lease_owner, "ai_policy_denied", blocked=True,
                    dependency_fingerprint=fingerprint, scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled,
                )
                await session.commit()
                return

            async def before_send() -> None:
                """Recheck destination, configuration revision, capability, and policy immediately before egress."""
                async with factory() as check_session:
                    current = settings_public.derive_for_role(
                        await settings_public.get_ai_execution_config(check_session, settings, redis, scope=scope), ROLE)
                    current_mapping = current.defaults.resolve(ROLE)
                    current_destination = current.endpoint_destination_id
                    current_fingerprint, current_supported = await _dependency_snapshot(current, redis, scope=scope)
                    if (
                        current.configuration_revision != config.configuration_revision
                        or current.gateway_identity != config.gateway_identity
                        or current_mapping != mapping or current_destination != destination
                        or not current_supported or current_fingerprint != fingerprint
                        or not _allowed(current, current_mapping, current_destination, source.local_only)
                    ):
                        raise PrivacyPolicyDenied("Timeline extraction policy changed before send")
                    # Fresh session: the outer transaction was rolled back before inference.
                    if await workspaces.read_access_fence(
                        check_session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                    ) != access_fence:
                        # 409 (not PrivacyPolicyDenied) so work is retried, not blocked forever.
                        raise HTTPException(status_code=409, detail="Workspace access changed")
                    current_ready = await documents.get_ready_version_ref(
                        check_session, version_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                    )
                    if (
                        current_ready is None or current_ready.document_id != data.document_id
                        or current_ready.source_id != source.id
                        or current_ready.source_generation != generation
                        or current_ready.local_only
                    ):
                        raise PrivacyPolicyDenied("Timeline extraction source revision changed before send")

            gateway = ModelGateway(
                redis=redis, base_url=config.gateway_base_url, api_key=config.gateway_api_key,
                destination_id=cast(str, destination), timeout_seconds=settings.background_llm_timeout_seconds,
                scope=scope, gateway_identity=config.gateway_identity,
                configuration_revision=config.configuration_revision, before_send=before_send,
                approved_endpoint_cidrs=config.endpoint_allowed_cidrs,
                allow_public_endpoints=config.allow_public_providers,
            )
            # Release workspace, membership, Source, Document and raw-URI locks before provider I/O.
            await session.rollback()
            response = await gateway.structured(
                ROLE, mapping, _policy(config, mapping, destination, source.local_only),
                extraction_messages(
                    [(item.id, item.content) for item in data.chunks],
                    [item.model_dump(mode="json", exclude={"name", "entity_id", "entity_revision", "observed_at"}) for item in membership_refs],
                    resolve_prompt(config.system_prompts, "timeline_extraction"),
                ),
                response_schema(), probe=False, before_send=before_send,
            )
            if not isinstance(response, dict):
                raise ValueError("invalid_model_response")  # noqa: TRY004  # ValueError is part of the contract; TypeError would change behavior
            proposals, model = response_content(response)
            async with factory() as check_session:
                current_config = settings_public.derive_for_role(
                    await settings_public.get_ai_execution_config(check_session, settings, redis, scope=scope), ROLE)
                current_mapping = current_config.defaults.resolve(ROLE)
                current_destination = current_config.endpoint_destination_id
                current_fingerprint, current_supported = await _dependency_snapshot(current_config, redis, scope=scope)
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
            # Reacquire the locks released before inference and compare against the phase-1 snapshot.
            locked = await sources.lock_source(
                session, data.source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                expected_access_fence=access_fence,
            )
            if (
                locked is None or locked.status != "active" or locked.generation != generation or locked.local_only
                or not await documents.lock_document_for_extraction(
                    session, data.document_id, locked.id, scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
                    source_fence=locked, expected_raw_uri=ready.raw_uri, expected_mime_type=ready.mime_type,
                )
            ):
                raise ValueError("document_unavailable")
            current_ready = await documents.get_ready_version_ref(
                session, version_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
            if (
                current_ready is None or current_ready.document_id != data.document_id
                or current_ready.source_id != data.source_id or current_ready.source_generation != generation
                or current_ready.raw_uri != ready.raw_uri or current_ready.mime_type != ready.mime_type
            ):
                raise ValueError("document_unavailable")
            if not await timeline.publish_extracted_events(
                session, work_id=work_id, lease_owner=lease_owner,
                ready=data, proposals=proposals, model=model,
                membership_revisions={item.membership_id: item.entity_revision for item in membership_refs},
                scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            ):
                await session.rollback()
                return
            from modules.timeline.models import TimelineExtractionResult
            result = await session.scalar(select(TimelineExtractionResult).join(
                TimelineExtractionWork, TimelineExtractionWork.id == TimelineExtractionResult.work_id,
            ).where(
                TimelineExtractionWork.workspace_id == scope.workspace_id,
                TimelineExtractionResult.work_id == work_id,
            ))
            event_ids = [UUID(str(item["event_id"])) for item in result.proposals_json] if result else []
            event_revisions = dict((await session.execute(select(Event.id, Event.revision).where(
                Event.workspace_id == scope.workspace_id, Event.id.in_(event_ids)
            ))).all()) if event_ids else {}
            await commit_with_replay(session, [
                make_timeline_change(event_id, event_revisions[event_id], scope=scope)
                for event_id in sorted(set(event_ids), key=str) if event_id in event_revisions
            ], scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
    except (CapabilityUnsupported, PrivacyPolicyDenied) as exc:
        async with factory() as session:
            await timeline.set_extraction_work_error(
                session, work_id, lease_owner,
                "structured_unsupported" if isinstance(exc, CapabilityUnsupported) else "ai_policy_denied",
                blocked=True, dependency_fingerprint=dependency_fingerprint, scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
            await session.commit()
    except (ModelGatewayError, ValueError, LookupError) as exc:
        async with factory() as session:
            await timeline.set_extraction_work_error(
                session, work_id, lease_owner, type(exc).__name__.lower(), scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
            await session.commit()
    except HTTPException as exc:
        if exc.status_code == 409:
            raise  # lease expires; recovery re-resolves (attempt-budgeted)
        await terminalize(factory, TimelineExtractionWork, work_id, scope.workspace_id, PERMISSION_LOST)


_WORKSPACE_CURSOR_KEY = "bbd:timeline-extraction:workspace-cursor"
_WORKSPACE_PAGE = 100


async def _recovery_workspace_ids(ctx: dict[str, object], factory: async_sessionmaker[AsyncSession]) -> list[UUID]:
    """One keyset page of workspaces with work rows or ready documents (cursor via core.worker_cursors)."""
    ctx.setdefault(STATE_KEY, {})
    keys = (_WORKSPACE_CURSOR_KEY,)
    after = await read_cursor(ctx, _WORKSPACE_CURSOR_KEY, keys)
    page: list[UUID] = []
    for lower in ((after, None) if after is not None else (None,)):
        async with factory() as session:
            query = select(TimelineExtractionWork.workspace_id).distinct().order_by(TimelineExtractionWork.workspace_id).limit(_WORKSPACE_PAGE)
            if lower is not None:
                query = query.where(TimelineExtractionWork.workspace_id > lower)
            found = set((await session.scalars(query)).all())
            found.update(await documents.list_ready_document_workspace_ids(session, after=lower, limit=_WORKSPACE_PAGE))
            await session.rollback()
        page = sorted(found)[:_WORKSPACE_PAGE]
        if page:
            break
    await write_cursor(ctx, _WORKSPACE_CURSOR_KEY, page[-1] if page else None, keys)
    return page


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
    multi_workspace_enabled = cast(Settings, ctx["settings"]).multi_workspace_enabled
    count = 0
    work_ids: list[UUID] = []
    for workspace_id in await _recovery_workspace_ids(ctx, factory):
        async with factory() as session:
            scope = await _workspace_job_scope(
                session, workspace_id, multi_workspace_enabled=multi_workspace_enabled,
            )
            if scope is None or not await settings_public.module_is_enabled(
                session, "knowledge.timeline", scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            ):
                await session.rollback()
                continue
            cursor_key = f"bbd:timeline-extraction:ready-cursor:{workspace_id}"
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
                source = await sources.lock_source(session, ref.source_id, scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled, expected_access_fence=access_fence)
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
                if not await documents.lock_document_for_extraction(
                    session, current.document_id, source.id, scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
                    source_fence=source, expected_raw_uri=current.raw_uri,
                    expected_mime_type=current.mime_type,
                ):
                    await session.commit()
                    continue
                current = await documents.get_ready_version_ref(
                    session, ref.document_version_id, scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled,
                )
                if current is None or current.source_generation != source.generation:
                    await session.commit()
                    continue
                if not source.graph_enabled:
                    await session.commit()
                    continue  # owner switched the knowledge graph off for this Source
                work_id = await timeline.schedule_extraction_work(
                    session, current, EXTRACTOR_VERSION, PROMPT_VERSION, scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled,
                )
                if current.local_only:
                    work = await session.scalar(select(TimelineExtractionWork).where(
                        TimelineExtractionWork.workspace_id == scope.workspace_id,
                        TimelineExtractionWork.id == work_id,
                    ).with_for_update())
                    if work is not None and work.status != "succeeded":
                        work.status, work.error_code = "blocked", "local_only_source"
                        work.next_attempt_at = datetime.max.replace(tzinfo=UTC)
                        work.lease_owner = work.lease_expires_at = None
                await session.commit()
        if next_cursor:
            await redis.set(cursor_key, next_cursor)
        else:
            await redis.delete(cursor_key)
        requeued_ids: list[UUID] = []
        async with factory() as session:
            blocked_items = await timeline.list_blocked_extraction_work(
                session, limit=25, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
        for blocked_id, version_id, captured_generation, error_code, old_fingerprint in blocked_items:
            if error_code not in {"ai_policy_denied", "structured_unsupported"} or not old_fingerprint:
                continue
            async with factory() as session:
                access_fence = await workspaces.read_access_fence(
                    session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                )
                ready = await documents.get_ready_version_ref(
                    session, version_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                )
                source = await sources.lock_source(session, ready.source_id, scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled, expected_access_fence=access_fence) if ready is not None else None
                current = await documents.get_ready_version_ref(
                    session, version_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                ) if ready is not None else None
                if (
                    ready is None or source is None or source.status != "active" or source.local_only
                    or source.generation != captured_generation or current is None
                    or current.document_id != ready.document_id or current.source_id != source.id
                    or current.source_generation != captured_generation
                ):
                    await timeline.defer_blocked_extraction_recheck(
                        session, blocked_id, old_fingerprint, scope=scope,
                        multi_workspace_enabled=multi_workspace_enabled,
                    )
                    await session.commit()
                    continue
                config = settings_public.derive_for_role(await settings_public.get_ai_execution_config(
                    session, cast(Settings, ctx["settings"]), redis, scope=scope,
                ), ROLE)
                mapping = config.defaults.resolve(ROLE)
                fingerprint, capability_supported = await _dependency_snapshot(config, redis, scope=scope)
                if (
                    capability_supported and _allowed(config, mapping, config.endpoint_destination_id, source.local_only)
                    and await timeline.requeue_blocked_extraction_work(
                        session, blocked_id, old_fingerprint, fingerprint, scope=scope,
                        multi_workspace_enabled=multi_workspace_enabled,
                    )
                ):
                    requeued_ids.append(blocked_id)
                else:
                    await timeline.defer_blocked_extraction_recheck(
                        session, blocked_id, old_fingerprint, scope=scope,
                        multi_workspace_enabled=multi_workspace_enabled,
                    )
                await session.commit()
        async with factory() as session:
            work_ids.extend(await timeline.list_recoverable_extraction_work(
                session, 25, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            ))
            await session.commit()
        work_ids.extend(requeued_ids)
    work_ids = list(dict.fromkeys(work_ids))
    for work_id in work_ids:
        await redis.enqueue_job(
            "process_timeline_extraction_work", str(work_id),
            _job_id=f"timeline-extraction:{work_id}",
            _queue_name=queue_for("process_timeline_extraction_work"),
        )
        count += 1
    return count
