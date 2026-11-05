"""Collection reservation, authorization, receipt settlement and cursor state (internal)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ConflictError, NotFoundError, UnauthorizedError
from core.ids import uuid7
from core.realtime import (
    commit_with_replay,
    make_source_change,
)
from core.workspaces.schemas import AccessFence, Scope
from modules.ingestion._credentials import (
    lock_source_credentials_in_uow,
)
from modules.ingestion._scope import (
    _actor_id,
    _admit_ingestion_scope,
    _lease_access_fence,
    _lease_scope,
    _run_scope,
    _source_in_scope,
)
from modules.ingestion.models import (
    COLLECTION_LEASE,
    RECEIPT_RETENTION,
    CollectionReceipt,
    CollectorCredential,
    IngestionRun,
    SourceIngestionState,
)
from modules.ingestion.schemas import (
    CollectionState,
    CollectionStateUpdate,
    ConnectorCollectionLease,
    TelegramCursor,
)
from modules.sources import public as sources
from modules.sources.public import ConnectorSource, SourceFence

if TYPE_CHECKING:
    # Annotation-only: a runtime import would close an import cycle between public contracts.
    from modules.connectors.public import CollectionRequestRef


async def get_source_cursor(
    session: AsyncSession,
    source_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> str | None:
    """Return the persisted collection cursor, or None before first ingestion."""
    await _admit_ingestion_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if (
        await _source_in_scope(
            session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        is None
    ):
        return None
    state = await session.get(SourceIngestionState, source_id)
    return state.cursor if state is not None else None


async def get_collection_state(
    session: AsyncSession,
    source_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> CollectionState:
    """Return cursor, validators and continuation checkpoint for an in-scope source (empty otherwise)."""
    await _admit_ingestion_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if (
        await _source_in_scope(
            session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        is None
    ):
        return CollectionState()
    state = await session.get(SourceIngestionState, source_id)
    if state is None:
        return CollectionState()
    return CollectionState(
        state.cursor,
        state.etag,
        state.last_modified,
        state.validators_revision,
        state.continuation_state,
    )


async def reset_native_collection_cursor(
    session: AsyncSession,
    source_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> None:
    """Clear one native provider cursor only after its exact collection and run leases are inactive.

    The caller holds ordered admission, Source, connector/provider identity locks and
    supplies its captured AccessFence before this state lock. Nonlocking equality
    revalidation rejects stale configuration/epoch without earlier lock acquisition. The
    owner commits the reset with replay publication; a live collector or nonterminal run rejects it.
    """
    current_access = await _admit_ingestion_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if current_access != access_fence:
        raise ConflictError(detail="Collection access fence is stale")
    source = await _source_in_scope(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if source is None:
        raise NotFoundError(detail="Source not found")
    state = await session.get(SourceIngestionState, source_id, with_for_update=True)
    if state is None:
        await commit_with_replay(
            session,
            [make_source_change(source.id, source.generation, source.status, scope=scope)],
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
        )
        return
    now = datetime.now(UTC)
    if (
        state.collection_lease_token is not None
        and state.lease_expires_at is not None
        and state.lease_expires_at > now
    ):
        raise ConflictError(detail="A collection is still active")
    active_run = await session.scalar(
        select(IngestionRun.id)
        .where(
            *_run_scope(scope),
            IngestionRun.source_id == source_id,
            IngestionRun.status.not_in(("succeeded", "failed")),
        )
        .limit(1)
        .with_for_update()
    )
    if active_run is not None:
        raise ConflictError(detail="An ingestion run is still active")
    state.collection_lease_token = None
    state.lease_run_id = None
    state.lease_expires_at = None
    state.cursor = None
    await commit_with_replay(
        session,
        [make_source_change(source.id, source.generation, source.status, scope=scope)],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )


def _encode_telegram_cursor(cursor: TelegramCursor) -> str:
    """Serialize the exact bounded Telegram cursor object stored in source state."""
    return json.dumps(cursor.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))


def _decode_telegram_cursor(value: str | None) -> TelegramCursor | None:
    """Parse a persisted Telegram cursor and fail closed on corrupt state."""
    if value is None:
        return None
    try:
        return TelegramCursor.model_validate_json(value)
    except ValueError as exc:
        raise ConflictError(detail="Telegram collection cursor is invalid") from exc


async def _lock_source_projection(
    session: AsyncSession,
    source_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    expected_access_fence: AccessFence | None = None,
) -> tuple[ConnectorSource | None, SourceFence, AccessFence]:
    """Hold the narrow source fence while reading the detached connector projection.

    Entry holds no domain locks. Source set admission captures AccessFence before the
    Source lifecycle lock; its detached configuration is compared under that lock.
    Caller owns release/commit, and no network work occurs under these locks.
    """
    locked = await sources.lock_source_set(
        session,
        (source_id,),
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=expected_access_fence,
    )
    fence = locked.fences[0]
    projection = await sources.get_connector_source(
        session,
        source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if projection is None or (
        projection.workspace_id != fence.workspace_id
        or projection.status != fence.status
        or projection.generation != fence.generation
    ):
        raise ConflictError(detail="Source projection changed under lifecycle lock")
    return projection, fence, locked.access_fence


async def _authorize_collection(
    session: AsyncSession,
    *,
    source_id: UUID,
    source_generation: int,
    connector_revision: int | None,
    collector_token: str | None,
    request_ref: CollectionRequestRef | None,
    scope: Scope,
    credential_scope: str = "ingestion:write",
) -> None:
    """Authorize a collection write by bearer capability or by an exact admitted request attempt.

    A request-bound caller (the in-process native worker) holds no bearer: its authority is the
    locked running request + unexpired slot + captured fences, re-proven here under the parent
    locks. Exactly one of the two proofs is required; an absent proof is 401, a stale attempt 409.
    """
    if request_ref is not None:
        from modules.connectors import public as connectors

        if connector_revision is None or not await connectors.lock_collection_request_in_uow(
            session,
            request_ref,
            source_id=source_id,
            source_generation=source_generation,
            connector_revision=connector_revision,
            scope=scope,
        ):
            raise ConflictError(detail="Collection admission is no longer current")
        return
    token_hash = hashlib.sha256((collector_token or "").encode()).hexdigest()
    if collector_token is None or not await session.scalar(
        select(CollectorCredential.token_hash).where(
            CollectorCredential.token_hash == token_hash,
            CollectorCredential.source_id == source_id,
            CollectorCredential.scope == credential_scope,
            CollectorCredential.revoked_at.is_(None),
        )
    ):
        raise UnauthorizedError(detail="Collector authentication required")


COLLECTION_STATS_MAX_BYTES = 1024


def _bounded_stats(stats: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return ``stats`` when its compact JSON fits ``COLLECTION_STATS_MAX_BYTES``, else ``None``.

    Telemetry is advisory: an oversized or unserialisable value is dropped whole rather than truncated into
    invalid JSON, and never fails the acceptance it rides on.
    """
    if not stats:
        return None
    try:
        encoded = json.dumps(stats, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    return stats if len(encoded.encode("utf-8")) <= COLLECTION_STATS_MAX_BYTES else None


async def _settle_request(
    session: AsyncSession,
    request_ref: CollectionRequestRef | None,
    *,
    outcome: Literal["succeeded", "no_changes"],
    run_id: UUID | None,
    scope: Scope,
    source_id: UUID,
    source_generation: int,
    connector_revision: int | None,
    batch_id: UUID | None = None,
    payload_digest: str | None = None,
    cursor_before: str | None = None,
    cursor_after: str | None = None,
    state_update: CollectionStateUpdate | None = None,
) -> None:
    """Insert the acceptance receipt, flush request outcome + slot release; the caller commits once.

    Receipt first (get-or-create by request UUID, so a replay never writes a second one), then the
    request is pointed at it in the same flush. ``state_update.stats`` is stored bounded on the receipt only.
    """
    if request_ref is None:
        return
    from modules.connectors import public as connectors

    receipt_id = await session.scalar(
        select(CollectionReceipt.id).where(CollectionReceipt.request_id == request_ref.request_id)
    )
    if receipt_id is None:
        now = datetime.now(UTC)
        receipt_id = uuid7()
        state_update = state_update or CollectionStateUpdate()
        session.add(
            CollectionReceipt(
                id=receipt_id,
                request_id=request_ref.request_id,
                workspace_id=scope.workspace_id,
                source_id=source_id,
                source_generation=source_generation,
                connector_revision=connector_revision,
                admission_token=request_ref.admission_token,
                outcome="accepted" if outcome == "succeeded" else "no_changes",
                batch_id=batch_id,
                run_id=run_id,
                payload_digest=payload_digest,
                cursor_before=cursor_before,
                cursor_after=cursor_after,
                coverage=state_update.coverage,
                etag=state_update.etag if state_update.update_validators else None,
                last_modified=state_update.last_modified
                if state_update.update_validators
                else None,
                collection_stats=_bounded_stats(state_update.stats),
                accepted_at=now,
                retain_until=now + RECEIPT_RETENTION,
            )
        )
        await session.flush()
    if not await connectors.settle_collection_in_uow(
        session,
        request_ref,
        outcome=outcome,
        ingestion_run_id=run_id,
        accepted_receipt_id=receipt_id,
    ):
        raise ConflictError(detail="Collection admission changed before acceptance")


def _apply_state_update(
    state: SourceIngestionState, state_update: CollectionStateUpdate | None, revision: int | None
) -> None:
    """Write checkpoint and (when the walk finished) validators onto the locked state row."""
    if state_update is None:
        return
    state.continuation_state = state_update.continuation_state
    if state_update.update_validators:
        state.etag, state.last_modified, state.validators_revision = (
            state_update.etag,
            state_update.last_modified,
            revision,
        )


async def validate_connector_collection_in_uow(
    session: AsyncSession,
    lease: ConnectorCollectionLease,
    *,
    collector_token: str | None,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
    request_ref: CollectionRequestRef | None = None,
) -> bool:
    """Validate original lease/bearer under prepared parents; lock only Ingestion state.

    Caller holds original account/session admission where applicable, Source/provisioning,
    required slots/native/world and all Source collector credentials; for GitHub those
    credentials precede its already-held grant, which precedes this state lock. Fresh full
    original lease/access/Source and nonlocking active applied Connector proof are mandatory.
    Read the exact prepared ingestion:write hash freshly, never reacquire earlier rows.
    Unavailable Source fails404, stale fences/provisioning409, ineligible bearer401; malformed
    lease/scope/admission and storage errors retain typed failures. False means only changed,
    missing or expired reservation (exact token, no run, cursor, expiry and future UTC time).
    No rotation/renewal/cursor/health/replay mutation, commit, rollback or I/O. Caller releases
    SQL before each physical send. This is not the original provider-credential comparison
    or acceptance's atomic proof, and carries no retained-effect journal authority.
    """
    original_access = _lease_access_fence(lease)
    _lease_scope(lease, scope)
    if access_fence != original_access:
        raise ConflictError(detail="Collection access capture differs from original lease")
    current_access = await _admit_ingestion_scope(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    current_source_fence = await sources.get_source_fence(
        session,
        lease.source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    source = await sources.get_connector_source(
        session,
        lease.source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if current_source_fence is None or source is None:
        raise NotFoundError(detail="Source not found")
    if (
        current_access != original_access
        or current_source_fence != source_fence
        or source_fence.id != lease.source_id
        or source_fence.workspace_id != lease.workspace_id
        or source_fence.generation != lease.source_generation
        or source.status != "active"
        or source.local_only
    ):
        raise ConflictError(detail="Original collection Source/access fence changed")
    from modules.connectors import public as connectors

    if not connectors.is_native_provider(source.provider):
        raise ConflictError(detail="Native provider is not configured")
    if not await connectors.require_collection_fence(
        session,
        source,
        connectors.CollectionFence(
            source_generation=lease.source_generation, connector_revision=lease.connector_revision
        ),
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    ):
        raise ConflictError(detail="Connector collection fence is stale")
    await _authorize_collection(
        session,
        source_id=lease.source_id,
        source_generation=lease.source_generation,
        connector_revision=lease.connector_revision,
        collector_token=collector_token,
        request_ref=request_ref,
        scope=scope,
    )
    state = await session.scalar(
        select(SourceIngestionState)
        .where(
            SourceIngestionState.source_id == lease.source_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return bool(
        state is not None
        and state.collection_lease_token == lease.token
        and state.lease_run_id is None
        and state.cursor == lease.cursor_before
        and state.lease_expires_at is not None
        and state.lease_expires_at == lease.expires_at
        and state.lease_expires_at > datetime.now(UTC)
    )


async def acquire_connector_collection(
    session: AsyncSession,
    *,
    source_id: UUID,
    source_generation: int,
    connector_revision: int,
    collector_token: str | None,
    scope: Scope,
    multi_workspace_enabled: bool,
    request_ref: CollectionRequestRef | None = None,
) -> ConnectorCollectionLease:
    """Reserve one native fetch under source, provisioning, credential, then state locks.

    The lease token is distinct from a processing run lease. It is committed with
    the source collection-start event before the provider performs network I/O;
    only an expired owner can be replaced and every later write rechecks its token.
    Detached lease captures workspace/actor/membership/configuration epoch; entry holds
    no domain locks. All Source collector rows are actually prepared after any native
    credential and before state; the exact ingestion:write hash is read under those locks.
    Provider sends occur only after this wrapper commits.
    """
    source, source_fence, access_fence = await _lock_source_projection(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if source is None:
        raise NotFoundError(detail="Source not found")
    if source.status != "active" or source.generation != source_generation:
        raise ConflictError(detail="Source generation is not active")
    from modules.connectors import public as connectors

    if not connectors.is_native_provider(source.provider):
        raise ConflictError(detail="Native provider is not configured")
    if not await connectors.require_collection_fence(
        session,
        source,
        connectors.CollectionFence(
            source_generation=source_generation, connector_revision=connector_revision
        ),
        lock=True,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    ):
        raise ConflictError(detail="Connector collection fence is stale")
    if source.provider == "telegram":
        credential = await connectors.get_native_credential_snapshot(
            session,
            source_id,
            source_generation=source_generation,
            connector_revision=connector_revision,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if (
            credential is None
            or credential.source_generation != source_generation
            or credential.configuration_revision != connector_revision
            or credential.state != "ready"
            or not credential.verified_bot_id
            or not credential.encrypted_token
            or credential.validated_at is None
        ):
            raise ConflictError(detail="Native Telegram credential is not ready")
    await lock_source_credentials_in_uow(
        session,
        source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    )
    await _authorize_collection(
        session,
        source_id=source_id,
        source_generation=source_generation,
        connector_revision=connector_revision,
        collector_token=collector_token,
        request_ref=request_ref,
        scope=scope,
    )
    state = await session.scalar(
        select(SourceIngestionState)
        .where(
            SourceIngestionState.source_id == source_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if state is None:
        state = SourceIngestionState(source_id=source_id, cursor=None)
        session.add(state)
        await session.flush()
    now = datetime.now(UTC)
    if (state.lease_run_id is not None and state.lease_expires_at is None) or (
        state.collection_lease_token is not None and state.lease_expires_at is None
    ):
        raise ConflictError(detail="Source collection ownership state is invalid")
    if state.lease_expires_at is not None and state.lease_expires_at > now:
        raise ConflictError(detail="Source collection is already in progress")
    token = uuid7()
    expires_at = now + COLLECTION_LEASE
    state.lease_run_id = None
    state.collection_lease_token = token
    state.lease_expires_at = expires_at
    if not await sources.record_collection_started_in_uow(
        session,
        source_id,
        source_generation,
        now,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    ):
        raise ConflictError(detail="Source is not active")
    await commit_with_replay(
        session,
        [make_source_change(source.id, source.generation, source.status, scope=scope)],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    return ConnectorCollectionLease(
        workspace_id=scope.workspace_id,
        actor_user_id=_actor_id(scope),
        membership_revision=scope.membership_revision,
        source_id=source_id,
        source_generation=source_generation,
        connector_revision=connector_revision,
        token=token,
        cursor_before=state.cursor,
        expires_at=expires_at,
        configuration_revision=access_fence.configuration_revision,
    )


async def read_telegram_collection_state(
    session: AsyncSession,
    lease: ConnectorCollectionLease,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> TelegramCursor | None:
    """Read a reserved Telegram cursor after rechecking source, revision, and bot fences.

    Entry holds no domain locks and rechecks original principal/configuration before
    Source. This short transaction verifies the current native binding and releases every
    row lock before the connector performs network I/O.
    """
    _lease_scope(lease, scope)
    source, _source_fence, _access_fence = await _lock_source_projection(
        session,
        lease.source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=AccessFence(
            lease.workspace_id,
            lease.actor_user_id,
            lease.membership_revision,
            lease.configuration_revision,
        ),
    )
    from modules.connectors import public as connectors

    if (
        source is None
        or source.provider != "telegram"
        or source.status != "active"
        or source.generation != lease.source_generation
    ):
        await session.rollback()
        raise ConflictError(detail="Telegram collection reservation is stale")
    if not await connectors.require_collection_fence(
        session,
        source,
        connectors.CollectionFence(
            source_generation=lease.source_generation,
            connector_revision=lease.connector_revision,
        ),
        lock=True,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    ):
        await session.rollback()
        raise ConflictError(detail="Telegram connector revision changed")
    credential = await connectors.get_native_credential_snapshot(
        session,
        lease.source_id,
        source_generation=lease.source_generation,
        connector_revision=lease.connector_revision,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if credential is None or credential.state != "ready" or not credential.verified_bot_id:
        await session.rollback()
        raise ConflictError(detail="Native Telegram credential is not ready")
    if (
        credential.source_generation != lease.source_generation
        or credential.configuration_revision != lease.connector_revision
        or credential.encrypted_token is None
        or credential.validated_at is None
    ):
        await session.rollback()
        raise ConflictError(detail="Native Telegram credential fence changed")
    state = await session.get(SourceIngestionState, lease.source_id, with_for_update=True)
    now = datetime.now(UTC)
    if (
        state is None
        or state.collection_lease_token != lease.token
        or state.lease_expires_at is None
        or state.lease_expires_at <= now
        or state.cursor != lease.cursor_before
    ):
        await session.rollback()
        raise ConflictError(detail="Telegram collection reservation is stale")
    cursor = _decode_telegram_cursor(state.cursor)
    if cursor is not None and cursor.bot_id != credential.verified_bot_id:
        await session.rollback()
        raise ConflictError(detail="Telegram cursor bot binding changed")
    await session.commit()
    return cursor


async def release_connector_collection(
    session: AsyncSession,
    lease: ConnectorCollectionLease,
    *,
    error_code: str | None,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> bool:
    """Release the matching reservation; stale revisions never publish old health errors.

    An exact token from the same source generation is cleared even after a desired
    revision changes so it cannot block the replacement. Health updates are only
    written while the original connector revision is still active.
    Entry holds no domain locks; original lease principal/configuration is mandatory.
    Success preserves the wrapper commit using its captured scoped replay fence.
    """
    _lease_scope(lease, scope)
    source, source_fence, access_fence = await _lock_source_projection(
        session,
        lease.source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=AccessFence(
            lease.workspace_id,
            lease.actor_user_id,
            lease.membership_revision,
            lease.configuration_revision,
        ),
    )
    from modules.connectors import public as connectors

    fence_current = False
    if (
        source is not None
        and source.status == "active"
        and source.generation == lease.source_generation
    ):
        fence_current = await connectors.require_collection_fence(
            session,
            source,
            connectors.CollectionFence(
                source_generation=lease.source_generation,
                connector_revision=lease.connector_revision,
            ),
            lock=True,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    state = await session.get(SourceIngestionState, lease.source_id, with_for_update=True)
    if (
        source is None
        or source.generation != lease.source_generation
        or state is None
        or state.collection_lease_token != lease.token
    ):
        await session.rollback()
        return False
    if error_code is not None and (not error_code or len(error_code) > 64):
        raise ValueError("Collection error code must be bounded")
    state.collection_lease_token = None
    state.lease_expires_at = None
    now = datetime.now(UTC)
    if error_code is not None and fence_current:
        await sources.record_collection_result_in_uow(
            session,
            source.id,
            source.generation,
            now,
            error_code,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
            source_fence=source_fence,
        )
    await commit_with_replay(
        session,
        [make_source_change(source.id, source.generation, source.status, scope=scope)],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    return True
