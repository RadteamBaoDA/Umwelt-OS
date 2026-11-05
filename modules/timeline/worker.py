"""Durable document-event extraction consumer and recovery loop."""

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from arq.connections import ArqRedis
from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.errors import ConflictError, DomainError
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
from core.realtime import commit_with_replay, make_timeline_change
from core.worker_context import resolve_owner_job_scope as _workspace_job_scope
from core.worker_context import worker_session_factory as _factory
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


async def _dependency_snapshot(config: AIExecutionConfig, redis: Redis, *, scope: Scope) -> tuple[str, bool]:
    """Fingerprint nonsecret extraction policy; capability is always reported supported.

    There is no evidence gate: the gateway tries native structured output and falls back to prompted JSON. A
    provider rejection parks the item with this fingerprint and it is requeued only when the fingerprint changes.
    """
    mapping = config.defaults.resolve(ROLE)
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
    }
    fingerprint = hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    return fingerprint, True


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


@dataclass
class _Run:
    """Identity of one extraction lease plus the one value later error handling needs.

    ``dependency_fingerprint`` is the attempted-request fingerprint; it is recorded as soon as
    the model policy is resolved so a failure after that point blocks the work against exactly
    the configuration that was attempted (explicit state instead of a closure ``nonlocal``).
    """

    factory: async_sessionmaker[AsyncSession]
    redis: Redis
    settings: Settings
    work_id: UUID
    lease_owner: str
    dependency_fingerprint: str | None = None

    @property
    def flag(self) -> bool:
        """Return the configured multi-workspace feature flag."""
        return self.settings.multi_workspace_enabled


@dataclass(frozen=True)
class _Lease:
    """Admitted scope and the document version/source generation captured at claim time."""

    scope: InternalJobScope
    version_id: UUID
    generation: int


@dataclass(frozen=True)
class _Captured:
    """Plain DTOs captured under phase-1 locks and carried through inference and publication."""

    access_fence: Any
    ready: Any
    source: Any
    data: Any
    membership_refs: list[Any]
    config: AIExecutionConfig
    mapping: ModelMapping | None
    destination: str | None
    fingerprint: str


async def _claim_lease(run: _Run) -> _Lease | None:
    """Admit the work's workspace and claim its lease; ``None`` means nothing to run.

    Runs on its own session: re-resolves the owner scope on every (stale-retried) admission
    attempt, leaves untouched work when the module is disabled, terminalizes denied work, and
    commits the claim before returning so no lock is held afterwards.
    """
    async with run.factory() as session:
        workspace_id = await session.scalar(select(TimelineExtractionWork.workspace_id).where(
            TimelineExtractionWork.id == run.work_id,
        ))
        if workspace_id is None:
            return None

        async def admit() -> tuple[InternalJobScope, TimelineExtractionWork | None] | None:
            """Re-resolve the owner scope and claim the lease; ``None`` when lineage is gone or the module is off."""
            await session.rollback()
            job_scope = await _workspace_job_scope(
                session, workspace_id, multi_workspace_enabled=run.flag,
            )
            if job_scope is None or not await settings_public.module_is_enabled(
                session, "knowledge.timeline", scope=job_scope, multi_workspace_enabled=run.flag,
            ):
                # ponytail: untouched work is re-claimed every sweep while the module is disabled;
                # upgrade = ack+requeue on module enable.
                return None  # unavailable lineage or disabled module: leave durable work untouched
            return job_scope, await timeline.claim_extraction_work(
                session, run.work_id, run.lease_owner, datetime.now(UTC), scope=job_scope,
                multi_workspace_enabled=run.flag,
                lease_seconds=max(110, run.settings.background_llm_timeout_seconds + 75),
            )

        try:
            admitted = await admit_retry_stale(admit)
        except (HTTPException, DomainError) as exc:
            await session.rollback()
            await terminalize(run.factory, TimelineExtractionWork, run.work_id, workspace_id, denial_code(exc) or STALE_SCOPE)
            return None
        if admitted is None:
            await session.rollback()
            return None
        scope, work = admitted
        if work is None:
            await session.commit()
            return None
        lease = _Lease(scope, work.document_version_id, work.source_generation)
        await session.commit()
    return lease


async def _block_work(
    run: _Run, session: AsyncSession, lease: _Lease, code: str, *, fingerprint: str | None = None,
) -> None:
    """Park the leased work as blocked with ``code`` and commit (the caller then stops).

    ``fingerprint`` is passed through only when given, so unblock sweeps can fence against the
    attempted configuration.
    """
    extra = {} if fingerprint is None else {"dependency_fingerprint": fingerprint}
    await timeline.set_extraction_work_error(
        session, run.work_id, run.lease_owner, code, blocked=True,
        scope=lease.scope, multi_workspace_enabled=run.flag, **extra,
    )
    await session.commit()


async def _lock_extraction_target(
    run: _Run, session: AsyncSession, lease: _Lease,
) -> tuple[Any, Any, Any] | None:
    """Lock Source then Document for the claimed version and re-verify it; ``None`` if work was parked.

    Returns ``(access_fence, source, ready)``. Each failed check blocks the work with its own
    code (document_unavailable, source_generation_changed, local_only_source, graph_disabled)
    and commits. Locks stay held until the caller rolls back before provider I/O.
    """
    scope, flag, generation = lease.scope, run.flag, lease.generation
    access_fence = await workspaces.read_access_fence(
        session, scope=scope, multi_workspace_enabled=flag,
    )
    locator = await documents.get_ready_version_ref(
        session, lease.version_id, scope=scope, multi_workspace_enabled=flag,
    )
    if locator is None or locator.source_generation != generation:
        await _block_work(run, session, lease, "document_unavailable")
        return None
    source = await sources.lock_source(session, locator.source_id, scope=scope,
        multi_workspace_enabled=flag, expected_access_fence=access_fence)
    if source is None or source.status != "active" or source.generation != generation:
        await _block_work(run, session, lease, "source_generation_changed")
        return None
    if not await documents.lock_document_for_extraction(
        session, locator.document_id, source.id, scope=scope,
        multi_workspace_enabled=flag, access_fence=access_fence,
        source_fence=source, expected_raw_uri=locator.raw_uri, expected_mime_type=locator.mime_type,
    ):
        await _block_work(run, session, lease, "document_unavailable")
        return None
    # Locks are released before inference and reacquired for publication.
    ready = await documents.get_ready_version_ref(
        session, lease.version_id, scope=scope, multi_workspace_enabled=flag,
    )
    if ready is None or ready.source_generation != generation or ready.document_id != locator.document_id:
        await _block_work(run, session, lease, "document_unavailable")
        return None
    if source.local_only:
        await _block_work(run, session, lease, "local_only_source")
        return None
    if not source.graph_enabled:
        # Switch flipped after scheduling: park quietly; intentional owner choice, not an error.
        await _block_work(run, session, lease, "graph_disabled")
        return None
    return access_fence, source, ready


async def _capture_inputs(run: _Run, session: AsyncSession, lease: _Lease) -> _Captured | None:
    """Phase 1: lock, read extraction input and resolve the model policy; ``None`` if work was parked.

    Reads the extraction input and entity membership context (bounded; overflow blocks with
    ``membership_context_bounded``), derives the background-role config, records the attempted
    dependency fingerprint on ``run`` and blocks with ``ai_policy_denied`` when egress is not
    permitted. Holds the phase-1 locks on return; the caller rolls back before inference.
    """
    locked = await _lock_extraction_target(run, session, lease)
    if locked is None:
        return None
    access_fence, source, ready = locked
    scope, flag = lease.scope, run.flag
    data = await documents.read_extraction_input(
        session, lease.version_id, scope=scope, multi_workspace_enabled=flag,
    )
    if data is None or data.source_generation != lease.generation:
        await _block_work(run, session, lease, "document_unavailable")
        return None
    try:
        membership_refs = await entities.list_version_membership_refs(
            session, lease.version_id, [item.id for item in data.chunks], scope=scope,
            multi_workspace_enabled=flag,
        )
    except ValueError:
        await _block_work(run, session, lease, "membership_context_bounded")
        return None
    config = settings_public.derive_for_role(
        await settings_public.get_ai_execution_config(session, run.settings, run.redis, scope=scope), ROLE)
    mapping = config.defaults.resolve(ROLE)
    destination = config.endpoint_destination_id
    fingerprint, _supported = await _dependency_snapshot(config, run.redis, scope=scope)
    run.dependency_fingerprint = fingerprint
    if not _allowed(config, mapping, destination, source.local_only):
        await _block_work(run, session, lease, "ai_policy_denied", fingerprint=fingerprint)
        return None
    return _Captured(access_fence, ready, source, data, membership_refs, config, mapping, destination, fingerprint)


async def _config_unchanged(
    run: _Run, session: AsyncSession, lease: _Lease, cap: _Captured,
) -> bool:
    """Return whether the current config, destination, capability and policy match the captured ones."""
    current = settings_public.derive_for_role(
        await settings_public.get_ai_execution_config(session, run.settings, run.redis, scope=lease.scope), ROLE)
    current_mapping = current.defaults.resolve(ROLE)
    current_destination = current.endpoint_destination_id
    current_fingerprint, current_supported = await _dependency_snapshot(current, run.redis, scope=lease.scope)
    return not (
        current.configuration_revision != cap.config.configuration_revision
        or current.gateway_identity != cap.config.gateway_identity
        or current_mapping != cap.mapping or current_destination != cap.destination
        or not current_supported or current_fingerprint != cap.fingerprint
        or not _allowed(current, current_mapping, current_destination, cap.source.local_only)
    )


def _make_before_send(run: _Run, lease: _Lease, cap: _Captured) -> Callable[[], Awaitable[None]]:
    """Build the egress guard the gateway awaits immediately before every provider request."""

    async def before_send() -> None:
        """Recheck destination, configuration revision, capability, and policy immediately before egress."""
        async with run.factory() as check_session:
            if not await _config_unchanged(run, check_session, lease, cap):
                raise PrivacyPolicyDenied("Timeline extraction policy changed before send")
            # Fresh session: the outer transaction was rolled back before inference.
            if await workspaces.read_access_fence(
                check_session, scope=lease.scope, multi_workspace_enabled=run.flag,
            ) != cap.access_fence:
                # 409 (not PrivacyPolicyDenied) so work is retried, not blocked forever.
                raise ConflictError("Workspace access changed")
            current_ready = await documents.get_ready_version_ref(
                check_session, lease.version_id, scope=lease.scope, multi_workspace_enabled=run.flag,
            )
            if (
                current_ready is None or current_ready.document_id != cap.data.document_id
                or current_ready.source_id != cap.source.id
                or current_ready.source_generation != lease.generation
                or current_ready.local_only
            ):
                raise PrivacyPolicyDenied("Timeline extraction source revision changed before send")

    return before_send


async def _infer(
    run: _Run, lease: _Lease, cap: _Captured, session: AsyncSession,
    before_send: Callable[[], Awaitable[None]],
) -> tuple[Any, Any]:
    """Release all phase-1 locks, then run the structured model call; returns ``(proposals, model)``.

    The rollback precedes provider I/O so chat's access fence is never blocked for the request's
    duration. A non-object response raises ``ValueError("invalid_model_response")`` (retryable).
    """
    config = cap.config
    gateway = ModelGateway(
        evidence=settings_public.capability_evidence(run.factory, config),
        redis=run.redis, base_url=config.gateway_base_url, api_key=config.gateway_api_key,
        destination_id=cast(str, cap.destination), timeout_seconds=run.settings.background_llm_timeout_seconds,
        scope=lease.scope, gateway_identity=config.gateway_identity, model_identity=config.model_identity,
        configuration_revision=config.configuration_revision, before_send=before_send,
        approved_endpoint_cidrs=config.endpoint_allowed_cidrs,
        allow_public_endpoints=config.allow_public_providers, kind=config.gateway_kind,
    )
    # Release workspace, membership, Source, Document and raw-URI locks before provider I/O.
    await session.rollback()
    response = await gateway.structured(
        ROLE, cap.mapping, _policy(config, cap.mapping, cap.destination, cap.source.local_only),
        extraction_messages(
            [(item.id, item.content) for item in cap.data.chunks],
            [item.model_dump(mode="json", exclude={"name", "entity_id", "entity_revision", "observed_at"}) for item in cap.membership_refs],
            resolve_prompt(config.system_prompts, "timeline_extraction"),
        ),
        response_schema(), probe=False, before_send=before_send,
    )
    if not isinstance(response, dict):
        raise ValueError("invalid_model_response")
    return response_content(response)


async def _publish_extraction(
    run: _Run, lease: _Lease, cap: _Captured, session: AsyncSession, proposals: Any, model: Any,
) -> None:
    """Reacquire the released locks, compare against the phase-1 snapshot, publish and commit.

    Any drift in source generation, locality, raw URI or MIME type raises
    ``ValueError("document_unavailable")`` and nothing is published. A lost lease (publish returns
    false) rolls back. On success the changed events are replayed with their new revisions in the
    same commit.
    """
    scope, flag, data, ready = lease.scope, run.flag, cap.data, cap.ready
    locked = await sources.lock_source(
        session, data.source_id, scope=scope, multi_workspace_enabled=flag,
        expected_access_fence=cap.access_fence,
    )
    if (
        locked is None or locked.status != "active" or locked.generation != lease.generation or locked.local_only
        or not await documents.lock_document_for_extraction(
            session, data.document_id, locked.id, scope=scope,
            multi_workspace_enabled=flag, access_fence=cap.access_fence,
            source_fence=locked, expected_raw_uri=ready.raw_uri, expected_mime_type=ready.mime_type,
        )
    ):
        raise ValueError("document_unavailable")
    current_ready = await documents.get_ready_version_ref(
        session, lease.version_id, scope=scope, multi_workspace_enabled=flag,
    )
    if (
        current_ready is None or current_ready.document_id != data.document_id
        or current_ready.source_id != data.source_id or current_ready.source_generation != lease.generation
        or current_ready.raw_uri != ready.raw_uri or current_ready.mime_type != ready.mime_type
    ):
        raise ValueError("document_unavailable")
    if not await timeline.publish_extracted_events(
        session, work_id=run.work_id, lease_owner=run.lease_owner,
        ready=data, proposals=proposals, model=model,
        membership_revisions={item.membership_id: item.entity_revision for item in cap.membership_refs},
        scope=scope, multi_workspace_enabled=flag,
    ):
        await session.rollback()
        return
    from modules.timeline.models import TimelineExtractionResult
    result = await session.scalar(select(TimelineExtractionResult).join(
        TimelineExtractionWork, TimelineExtractionWork.id == TimelineExtractionResult.work_id,
    ).where(
        TimelineExtractionWork.workspace_id == scope.workspace_id,
        TimelineExtractionResult.work_id == run.work_id,
    ))
    event_ids = [UUID(str(item["event_id"])) for item in result.proposals_json] if result else []
    event_revisions = dict((await session.execute(select(Event.id, Event.revision).where(
        Event.workspace_id == scope.workspace_id, Event.id.in_(event_ids)
    ))).all()) if event_ids else {}
    await commit_with_replay(session, [
        make_timeline_change(event_id, event_revisions[event_id], scope=scope)
        for event_id in sorted(set(event_ids), key=str) if event_id in event_revisions
    ], scope=scope, multi_workspace_enabled=flag, access_fence=cap.access_fence)


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

    Failure routing after the claim: capability/policy denials block the work with the attempted
    dependency fingerprint, gateway/value/lookup errors record a retry, a 409 re-raises (the lease
    expires and recovery re-resolves) and any other HTTP-shaped denial terminalizes as permission lost.
    """
    work_id = UUID(work_id_value)
    run = _Run(
        factory=_factory(ctx), redis=cast(Redis, ctx["redis"]), settings=cast(Settings, ctx["settings"]),
        work_id=work_id,
        lease_owner=hashlib.sha256(f"{work_id}:{datetime.now(UTC).isoformat()}".encode()).hexdigest()[:48],
    )
    lease = await _claim_lease(run)
    if lease is None:
        return
    scope = lease.scope
    try:
        async with run.factory() as session:
            cap = await _capture_inputs(run, session, lease)
            if cap is None:
                return
            before_send = _make_before_send(run, lease, cap)
            proposals, model = await _infer(run, lease, cap, session, before_send)
            async with run.factory() as check_session:
                if not await _config_unchanged(run, check_session, lease, cap):
                    # Keep the attempted-request fingerprint so recovery sees
                    # a changed, already-permitted configuration as retryable.
                    raise PrivacyPolicyDenied("Timeline extraction policy changed before publication")
            await _publish_extraction(run, lease, cap, session, proposals, model)
    except (CapabilityUnsupported, PrivacyPolicyDenied) as exc:
        async with run.factory() as session:
            await timeline.set_extraction_work_error(
                session, work_id, run.lease_owner,
                "structured_unsupported" if isinstance(exc, CapabilityUnsupported) else "ai_policy_denied",
                blocked=True, dependency_fingerprint=run.dependency_fingerprint, scope=scope,
                multi_workspace_enabled=run.flag,
            )
            await session.commit()
    except (ModelGatewayError, ValueError, LookupError) as exc:
        async with run.factory() as session:
            await timeline.set_extraction_work_error(
                session, work_id, run.lease_owner, type(exc).__name__.lower(), scope=scope,
                multi_workspace_enabled=run.flag,
            )
            await session.commit()
    except (HTTPException, DomainError) as exc:
        if exc.status_code == 409:
            raise  # lease expires; recovery re-resolves (attempt-budgeted)
        await terminalize(run.factory, TimelineExtractionWork, work_id, scope.workspace_id, PERMISSION_LOST)


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


async def _load_ready_page(
    factory: async_sessionmaker[AsyncSession], redis: ArqRedis, workspace_id: UUID, flag: bool,
) -> tuple[InternalJobScope, list[Any], str | None, str] | None:
    """Resolve the workspace scope and read one page of ready version refs; ``None`` skips it.

    Skips unavailable lineage and disabled modules (rolling back). The per-workspace Redis
    cursor is bounded (over-long values are ignored). Returns ``(scope, refs, next_cursor,
    cursor_key)``; the page read is committed before returning.
    """
    async with factory() as session:
        scope = await _workspace_job_scope(session, workspace_id, multi_workspace_enabled=flag)
        if scope is None or not await settings_public.module_is_enabled(
            session, "knowledge.timeline", scope=scope, multi_workspace_enabled=flag,
        ):
            await session.rollback()
            return None
        cursor_key = f"bbd:timeline-extraction:ready-cursor:{workspace_id}"
        raw_cursor = await redis.get(cursor_key)
        cursor = raw_cursor.decode() if isinstance(raw_cursor, bytes) else raw_cursor if isinstance(raw_cursor, str) else None
        if cursor is not None and len(cursor) > 512:
            cursor = None
        refs, next_cursor = await documents.list_ready_version_refs(
            session, limit=25, cursor=cursor, scope=scope,
            multi_workspace_enabled=flag,
        )
        await session.commit()
    return scope, refs, next_cursor, cursor_key


async def _schedule_ready_ref(
    factory: async_sessionmaker[AsyncSession], ref: Any, scope: InternalJobScope, flag: bool,
) -> None:
    """Schedule extraction work for one ready ref after re-verifying Source and Document locks.

    Order: read fence, ``lock_source``, ref re-read, ``lock_document_for_extraction``, second
    re-read, then ``schedule_extraction_work``. Any drift, an inactive source or a source with the
    knowledge graph off commits without scheduling. Local-only versions are parked as blocked
    (never sent to a model). One commit at every exit.
    """
    async with factory() as session:
        access_fence = await workspaces.read_access_fence(
            session, scope=scope, multi_workspace_enabled=flag,
        )
        source = await sources.lock_source(session, ref.source_id, scope=scope,
            multi_workspace_enabled=flag, expected_access_fence=access_fence)
        current = await documents.get_ready_version_ref(
            session, ref.document_version_id, scope=scope,
            multi_workspace_enabled=flag,
        )
        if (
            source is None or source.status != "active" or current is None
            or current.document_id != ref.document_id or current.source_id != source.id
            or current.source_generation != source.generation
            or current.source_generation != ref.source_generation
        ):
            await session.commit()
            return
        if not await documents.lock_document_for_extraction(
            session, current.document_id, source.id, scope=scope,
            multi_workspace_enabled=flag, access_fence=access_fence,
            source_fence=source, expected_raw_uri=current.raw_uri,
            expected_mime_type=current.mime_type,
        ):
            await session.commit()
            return
        current = await documents.get_ready_version_ref(
            session, ref.document_version_id, scope=scope,
            multi_workspace_enabled=flag,
        )
        if current is None or current.source_generation != source.generation:
            await session.commit()
            return
        if not source.graph_enabled:
            await session.commit()
            return  # owner switched the knowledge graph off for this Source
        work_id = await timeline.schedule_extraction_work(
            session, current, EXTRACTOR_VERSION, PROMPT_VERSION, scope=scope,
            multi_workspace_enabled=flag,
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


async def _recheck_blocked_item(
    factory: async_sessionmaker[AsyncSession], redis: ArqRedis, settings: Settings, scope: InternalJobScope,
    item: tuple[Any, ...],
) -> UUID | None:
    """Recheck one policy-blocked work item; return its ID when it was requeued.

    The item is requeued only when its source, capability and privacy fences permit another
    request AND the attempted-request fingerprint changed; otherwise its recheck is deferred.
    One commit at every exit.
    """
    flag = settings.multi_workspace_enabled
    blocked_id, version_id, captured_generation, _error_code, old_fingerprint = item
    async with factory() as session:
        access_fence = await workspaces.read_access_fence(
            session, scope=scope, multi_workspace_enabled=flag,
        )
        ready = await documents.get_ready_version_ref(
            session, version_id, scope=scope, multi_workspace_enabled=flag,
        )
        source = await sources.lock_source(session, ready.source_id, scope=scope,
            multi_workspace_enabled=flag, expected_access_fence=access_fence) if ready is not None else None
        current = await documents.get_ready_version_ref(
            session, version_id, scope=scope, multi_workspace_enabled=flag,
        ) if ready is not None else None
        if (
            ready is None or source is None or source.status != "active" or source.local_only
            or source.generation != captured_generation or current is None
            or current.document_id != ready.document_id or current.source_id != source.id
            or current.source_generation != captured_generation
        ):
            await timeline.defer_blocked_extraction_recheck(
                session, blocked_id, old_fingerprint, scope=scope,
                multi_workspace_enabled=flag,
            )
            await session.commit()
            return None
        config = settings_public.derive_for_role(await settings_public.get_ai_execution_config(
            session, settings, redis, scope=scope,
        ), ROLE)
        mapping = config.defaults.resolve(ROLE)
        fingerprint, capability_supported = await _dependency_snapshot(config, redis, scope=scope)
        requeued = (
            capability_supported and _allowed(config, mapping, config.endpoint_destination_id, source.local_only)
            and await timeline.requeue_blocked_extraction_work(
                session, blocked_id, old_fingerprint, fingerprint, scope=scope,
                multi_workspace_enabled=flag,
            )
        )
        if not requeued:
            await timeline.defer_blocked_extraction_recheck(
                session, blocked_id, old_fingerprint, scope=scope,
                multi_workspace_enabled=flag,
            )
        await session.commit()
        return blocked_id if requeued else None


async def _requeue_blocked_work(
    factory: async_sessionmaker[AsyncSession], redis: ArqRedis, settings: Settings, scope: InternalJobScope,
) -> list[UUID]:
    """Recheck up to 25 policy-blocked items of the workspace; returns the requeued work IDs."""
    flag = settings.multi_workspace_enabled
    requeued_ids: list[UUID] = []
    async with factory() as session:
        blocked_items = await timeline.list_blocked_extraction_work(
            session, limit=25, scope=scope, multi_workspace_enabled=flag,
        )
    for item in blocked_items:
        error_code, old_fingerprint = item[3], item[4]
        if error_code not in {"ai_policy_denied", "structured_unsupported"} or not old_fingerprint:
            continue
        requeued = await _recheck_blocked_item(factory, redis, settings, scope, item)
        if requeued is not None:
            requeued_ids.append(requeued)
    return requeued_ids


async def recover_timeline_extraction_work(ctx: dict[str, object]) -> int:
    """Rescan ready versions and requeue a bounded page of durable due timeline work.

    PostgreSQL uniqueness is the recovery authority; the Redis cursor only
    avoids repeatedly scanning the first ready-document page. Policy-blocked
    work is rechecked on a finite schedule and requeued only when its saved
    attempted-request fingerprint changed and current source, capability, and
    privacy fences permit another request. A stale response therefore cannot
    make its replacement configuration look already attempted.
    Returns the number of jobs enqueued.
    """
    factory = _factory(ctx)
    redis = cast(ArqRedis, ctx["redis"])
    settings = cast(Settings, ctx["settings"])
    flag = settings.multi_workspace_enabled
    count = 0
    work_ids: list[UUID] = []
    for workspace_id in await _recovery_workspace_ids(ctx, factory):
        page = await _load_ready_page(factory, redis, workspace_id, flag)
        if page is None:
            continue
        scope, refs, next_cursor, cursor_key = page
        for ref in refs:
            await _schedule_ready_ref(factory, ref, scope, flag)
        if next_cursor:
            await redis.set(cursor_key, next_cursor)
        else:
            await redis.delete(cursor_key)
        requeued_ids = await _requeue_blocked_work(factory, redis, settings, scope)
        async with factory() as session:
            work_ids.extend(await timeline.list_recoverable_extraction_work(
                session, 25, scope=scope, multi_workspace_enabled=flag,
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
