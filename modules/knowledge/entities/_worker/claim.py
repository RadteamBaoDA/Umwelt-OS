"""Claim, lock, snapshot, provider call and re-lock phases of one entity extraction run."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import cast
from uuid import UUID

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.errors import ConflictError, DomainError
from core.job_denial import (
    STALE_SCOPE,
    admit_retry_stale,
    denial_code,
    terminalize,
)
from core.model_gateway.client import (
    ModelGateway,
    PrivacyPolicyDenied,
)
from core.model_gateway.policy import may_send
from core.model_gateway.schemas import (
    AIExecutionConfig,
    ModelMapping,
    RequestPolicy,
)
from core.worker_context import resolve_owner_job_scope as _workspace_job_scope
from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.knowledge.documents import public as documents
from modules.knowledge.entities import public as entities
from modules.knowledge.entities._worker.ready import (
    EXTRACTION_ROLE,
    _dependency_snapshot,
    _policy_allows_extraction,
)
from modules.knowledge.entities.extraction import (
    ExtractionOutput,
    extraction_messages,
    response_content,
    response_schema,
)
from modules.knowledge.entities.models import EntityExtractionWork
from modules.settings import public as settings_public
from modules.settings.public import resolve_prompt
from modules.sources import public as sources

logger = logging.getLogger("modules.knowledge.entities.worker")  # same logger object as the facade


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
    session: AsyncSession,
    workspace_id: UUID,
    work_id: UUID,
    lease_owner: str,
    settings: Settings,
) -> tuple[InternalJobScope, EntityExtractionWork | None] | None:
    """Admission attempt: release stale locks, resolve the owner scope, and claim the work row.

    Starts with ``session.rollback()`` so a 409 retry (see ``admit_retry_stale``) re-reads fresh state. Returns
    ``None`` when the workspace owner is gone or the module is disabled; otherwise the scope and the claimed row
    (``None`` when another worker holds the lease). Does not commit.
    """
    multi_workspace_enabled = settings.multi_workspace_enabled
    await session.rollback()
    job_scope = await _workspace_job_scope(
        session,
        workspace_id,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if job_scope is None or not await settings_public.module_is_enabled(
        session,
        "knowledge.entities",
        scope=job_scope,
        multi_workspace_enabled=multi_workspace_enabled,
    ):
        return None
    return job_scope, await entities.claim_extraction_work(
        session,
        work_id,
        lease_owner,
        datetime.now(UTC),
        scope=job_scope,
        multi_workspace_enabled=multi_workspace_enabled,
        lease_seconds=max(110, settings.background_llm_timeout_seconds + 75),
    )


async def _claim(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    work_id: UUID,
    lease_owner: str,
) -> _Claim | None:
    """Claim ``work_id`` on ``session`` and commit the claim; ``None`` means there is nothing to run.

    A denied/stale scope (HTTP error from admission) terminalizes the row on its own session. A claim that
    lost the lease race commits the (empty) transaction and returns ``None``. On success the lease is
    committed before returning so inference never runs inside the claim transaction.
    """
    workspace_id = await session.scalar(
        select(EntityExtractionWork.workspace_id).where(
            EntityExtractionWork.id == work_id,
        )
    )
    if workspace_id is None:
        return None
    try:
        admitted = await admit_retry_stale(
            lambda: _admit_claim(session, workspace_id, work_id, lease_owner, settings)
        )
    except (HTTPException, DomainError) as exc:
        await session.rollback()
        await terminalize(
            factory, EntityExtractionWork, work_id, workspace_id, denial_code(exc) or STALE_SCOPE
        )
        return None
    if admitted is None:
        return None
    scope, work = admitted
    if work is None:
        await session.commit()
        return None
    claim = _Claim(
        work_id=work_id,
        lease_owner=lease_owner,
        scope=scope,
        multi_workspace_enabled=settings.multi_workspace_enabled,
        version_id=work.document_version_id,
        captured_generation=work.source_generation,
    )
    await session.commit()
    return claim


async def _block_work(
    session: AsyncSession,
    claim: _Claim,
    error_code: str,
    *,
    dependency_fingerprint: str | None = None,
) -> None:
    """Park the lease-owned work as blocked with ``error_code`` and commit on ``session``."""
    await entities.set_extraction_work_error(
        session,
        claim.work_id,
        claim.lease_owner,
        error_code,
        blocked=True,
        dependency_fingerprint=dependency_fingerprint,
        scope=claim.scope,
        multi_workspace_enabled=claim.multi_workspace_enabled,
    )
    await session.commit()


async def _lock_and_check_source(session: AsyncSession, claim: _Claim) -> _Locked | _Skip:
    """Lock Source then Document, compare against the claimed generation, and capture plain DTOs.

    Lock order is access fence read -> ready ref -> Source -> Document -> ready ref re-read. Every mismatch
    (document gone, generation drift, local-only, graph disabled) parks the work via ``_block_work`` (which
    commits) and returns ``_Skip``. Locks stay held on ``session`` until the caller's rollback.
    """
    scope, mwe = claim.scope, claim.multi_workspace_enabled
    access_fence = await workspaces.read_access_fence(
        session, scope=scope, multi_workspace_enabled=mwe
    )
    ready = await documents.get_ready_version_ref(
        session, claim.version_id, scope=scope, multi_workspace_enabled=mwe
    )
    if ready is None:
        await _block_work(session, claim, "document_unavailable")
        return _Skip()
    expected_document_id = ready.document_id
    source = await sources.lock_source(
        session,
        ready.source_id,
        scope=scope,
        multi_workspace_enabled=mwe,
        expected_access_fence=access_fence,
    )
    if (
        source is None
        or source.status != "active"
        or source.generation != claim.captured_generation
    ):
        await _block_work(session, claim, "source_generation_changed")
        return _Skip()
    if not await documents.lock_document_for_extraction(
        session,
        ready.document_id,
        ready.source_id,
        scope=scope,
        multi_workspace_enabled=mwe,
        access_fence=access_fence,
        source_fence=source,
        expected_raw_uri=ready.raw_uri,
        expected_mime_type=ready.mime_type,
    ):
        await _block_work(session, claim, "document_unavailable")
        return _Skip()
    # Locks are released before inference and reacquired for publication.
    ready = await documents.get_ready_version_ref(
        session, claim.version_id, scope=scope, multi_workspace_enabled=mwe
    )
    if (
        ready is None
        or ready.document_id != expected_document_id
        or ready.source_id != source.id
        or ready.source_generation != claim.captured_generation
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
        access_fence=access_fence,
        ready=ready,
        source_local_only=source.local_only,
        source_generation=source.generation,
    )


async def _capture_extraction_snapshot(
    session: AsyncSession,
    claim: _Claim,
    settings: Settings,
    redis: Redis,
    state: _RunState,
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
    data = await documents.read_extraction_input(
        session, claim.version_id, scope=scope, multi_workspace_enabled=mwe
    )
    if (
        data is None
        or data.source_generation != locked.source_generation
        or data.source_generation != claim.captured_generation
    ):
        await _block_work(session, claim, "document_unavailable")
        return _Skip()
    role = EXTRACTION_ROLE
    config = settings_public.derive_for_role(
        await settings_public.get_ai_execution_config(session, settings, redis, scope=scope), role
    )
    mapping = config.defaults.resolve(role)
    destination = config.endpoint_destination_id
    state.dependency_fingerprint, _ = await _dependency_snapshot(config, redis, scope=scope)
    policy = RequestPolicy(
        workspace_id=config.workspace_id,
        actor_user_id=config.actor_user_id,
        membership_revision=config.membership_revision,
        gateway_identity=config.gateway_identity,
        reasoning_allowed=config.privacy.allow_remote_reasoning,
        local_only=locked.source_local_only,
        permitted_destinations=frozenset({destination}) if destination else frozenset(),
        reasoning_destinations=frozenset(config.privacy.reasoning_destinations),
        configuration_revision=config.configuration_revision,
    )
    if not _policy_allows_extraction(config, mapping, destination, locked.source_local_only):
        await _block_work(
            session, claim, "ai_policy_denied", dependency_fingerprint=state.dependency_fingerprint
        )
        return _Skip()
    return _Snapshot(
        claim=claim,
        access_fence=locked.access_fence,
        ready=locked.ready,
        data=data,
        config=config,
        mapping=mapping,
        destination=destination,
        policy=policy,
    )


async def _recheck_before_send(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    redis: Redis,
    snap: _Snapshot,
) -> None:
    """Recheck saved settings, privacy policy, access fence and source revision immediately before remote use.

    Runs on its own fresh session because the outer transaction is rolled back before inference. Raises
    ``PrivacyPolicyDenied`` (blocks the work) on policy/source drift and HTTP 409 (retried) when the workspace
    access fence changed.
    """
    claim, config, mapping, destination = snap.claim, snap.config, snap.mapping, snap.destination
    scope, mwe, role = claim.scope, claim.multi_workspace_enabled, EXTRACTION_ROLE
    async with factory() as current_session:
        current = settings_public.derive_for_role(
            await settings_public.get_ai_execution_config(
                current_session,
                settings,
                redis,
                scope=scope,
            ),
            role,
        )
        current_mapping = current.defaults.resolve(role)
        current_destination = current.endpoint_destination_id
        if (
            current.configuration_revision != config.configuration_revision
            or current.gateway_identity != config.gateway_identity
            or current_destination != destination
            or current_mapping != mapping
            or current.endpoint_policy_denied
            or not current_destination
        ):
            raise PrivacyPolicyDenied("Extraction policy changed before send")
        current_policy = RequestPolicy(
            workspace_id=current.workspace_id,
            actor_user_id=current.actor_user_id,
            membership_revision=current.membership_revision,
            gateway_identity=current.gateway_identity,
            reasoning_allowed=current.privacy.allow_remote_reasoning,
            permitted_destinations=frozenset({current_destination}),
            reasoning_destinations=frozenset(current.privacy.reasoning_destinations),
            configuration_revision=current.configuration_revision,
        )
        if not may_send(
            current_policy,
            role,
            current_mapping,
            current_destination,
            current.gateway_credential_configured,
            "structured",
        ):
            raise PrivacyPolicyDenied("Extraction policy denied before send")
        # Fresh session: the outer transaction is rolled back before inference.
        if (
            await workspaces.read_access_fence(
                current_session,
                scope=scope,
                multi_workspace_enabled=mwe,
            )
            != snap.access_fence
        ):
            # 409 (not PrivacyPolicyDenied) so work is retried, not blocked forever.
            raise ConflictError("Workspace access changed")
        current_ready = await documents.get_ready_version_ref(
            current_session,
            claim.version_id,
            scope=scope,
            multi_workspace_enabled=mwe,
        )
        if (
            current_ready is None
            or current_ready.document_id != snap.data.document_id
            or current_ready.source_id != snap.data.source_id
            or current_ready.source_generation != claim.captured_generation
            or current_ready.local_only
        ):
            raise PrivacyPolicyDenied("Extraction source revision changed before send")


async def _call_provider(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    redis: Redis,
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
        redis=redis,
        base_url=config.gateway_base_url,
        api_key=config.gateway_api_key,
        destination_id=cast(str, snap.destination),
        timeout_seconds=settings.background_llm_timeout_seconds,
        scope=snap.claim.scope,
        gateway_identity=config.gateway_identity,
        model_identity=config.model_identity,
        configuration_revision=config.configuration_revision,
        before_send=before_send,
        approved_endpoint_cidrs=config.endpoint_allowed_cidrs,
        allow_public_endpoints=config.allow_public_providers,
        kind=config.gateway_kind,
    )
    # Release workspace, membership, Source, Document and raw-URI locks before provider I/O.
    await session.rollback()
    return await gateway.structured(
        EXTRACTION_ROLE,
        mapping,
        snap.policy,
        extraction_messages(
            [(chunk.id, chunk.content) for chunk in snap.data.chunks],
            resolve_prompt(config.system_prompts, "entity_extraction"),
        ),
        response_schema(),
        probe=False,
        before_send=before_send,
    )


def _validate_extraction_response(
    response: object,
    data: documents.ExtractionInput,
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
        item
        for item in extracted.relationships
        if item.source_key != item.target_key
        and item.source_key in entity_keys
        and item.target_key in entity_keys
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
        session,
        data.source_id,
        scope=scope,
        multi_workspace_enabled=mwe,
        expected_access_fence=snap.access_fence,
    )
    if (
        relocked is None
        or relocked.status != "active"
        or relocked.generation != claim.captured_generation
        or relocked.local_only
        or not await documents.lock_document_for_extraction(
            session,
            data.document_id,
            relocked.id,
            scope=scope,
            multi_workspace_enabled=mwe,
            access_fence=snap.access_fence,
            source_fence=relocked,
            expected_raw_uri=snap.ready.raw_uri,
            expected_mime_type=snap.ready.mime_type,
        )
    ):
        raise ValueError("document_unavailable")
    current_ready = await documents.get_ready_version_ref(
        session,
        claim.version_id,
        scope=scope,
        multi_workspace_enabled=mwe,
    )
    if (
        current_ready is None
        or current_ready.document_id != data.document_id
        or current_ready.source_id != data.source_id
        or current_ready.source_generation != claim.captured_generation
    ):
        raise ValueError("document_unavailable")
