"""Native provider collection acceptance (internal; import through ``public``).

The orchestration here is provider-agnostic. Provider proofs are verified by the
:class:`~modules.ingestion._native_verifiers.NativeProofVerifier` registered for the locked Source's provider.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import (
    DomainError,
)
from core.events import DomainEvent
from core.ids import uuid7
from core.realtime import (
    ReplayDraft,
    commit_with_replay,
    make_ingestion_change,
    make_source_change,
)
from core.workspaces.schemas import AccessFence, Scope
from modules.connectors.collection_schemas import CollectionRequestRef
from modules.ingestion._collection import (
    _apply_state_update,
    _authorize_collection,
    _lock_source_projection,
    _settle_request,
)
from modules.ingestion._credentials import lock_source_credentials_in_uow
from modules.ingestion._errors import domain_error_for_status
from modules.ingestion._event_publisher import publish_event
from modules.ingestion._native_records import native_record_key, seen_record_keys
from modules.ingestion._native_verifiers import (
    NativeAcceptance,
    NativeProofVerifier,
    NativeProviderParams,
    ProviderRecord,
    native_verifier_for,
)
from modules.ingestion._outbox import schedule_normalization
from modules.ingestion._scope import (
    _actor_id,
    _digest,
    _lease_access_fence,
    _lease_scope,
    _run_scope,
)
from modules.ingestion.models import (
    COLLECTION_LEASE,
    IngestionBatch,
    IngestionRun,
    IngestionStage,
    SourceIngestionState,
    SourceObservation,
)
from modules.ingestion.schemas import (
    CollectionStateUpdate,
    ConnectorCollectionLease,
    NativeCollectionBatch,
    NativeCollectionReceipt,
)
from modules.sources import public as sources
from modules.sources.schemas import ConnectorSource, SourceFence

_BATCH_KEY_MAX = 255


def _conflict(status_code: int, detail: str) -> DomainError:
    """Build the client-visible rejection raised by every proof check (see ``domain_error_for_status``)."""
    return domain_error_for_status(status_code, detail)


def _receipt(
    ctx: NativeAcceptance, *, batch_id: UUID, run_id: UUID, queued: bool, record_count: int, cursor_after: str | None,
) -> NativeCollectionReceipt:
    """Build the receipt under the original actor and membership epoch.

    ``queued`` selects ``queued``; otherwise the status is ``succeeded`` when Telegram updates were
    acknowledged and ``no_changes`` when the page carried nothing.
    """
    payload = ctx.payload
    return NativeCollectionReceipt(
        workspace_id=ctx.scope.workspace_id, actor_user_id=_actor_id(ctx.scope),
        membership_revision=ctx.scope.membership_revision, batch_id=batch_id, run_id=run_id,
        status="queued" if queued else ("succeeded" if payload.telegram_raw_deliveries else "no_changes"),
        received_update_count=len(payload.telegram_raw_deliveries), record_count=record_count,
        coverage=payload.coverage, cursor_after=cursor_after,
    )


def _check_lease_matches(lease: ConnectorCollectionLease, payload: NativeCollectionBatch) -> None:
    """Require the payload to equal the owner-issued lease on Source, generation, token, revision and cursor."""
    if (lease.source_id != payload.source_id or lease.source_generation != payload.source_generation
            or lease.token != payload.lease_token or lease.connector_revision != payload.connector_revision
            or lease.cursor_before != payload.cursor_before):
        raise _conflict(409, "Native collection payload differs from original lease")


async def _admit_source(
    session: AsyncSession, payload: NativeCollectionBatch, *, scope: Scope, multi_workspace_enabled: bool,
    expected_access_fence: AccessFence,
) -> tuple[ConnectorSource, SourceFence, AccessFence]:
    """Lock the Source projection and require an active, non-local, current-generation native Source.

    Returns the projection with its Source and access fences; the narrow Source fence is held from here on.
    """
    from modules.connectors import public as connectors

    source, source_fence, access_fence = await _lock_source_projection(
        session, payload.source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=expected_access_fence,
    )
    if source is None:
        raise _conflict(404, "Source not found")
    if source.status != "active" or source.local_only or source.generation != payload.source_generation:
        raise _conflict(409, "Source generation changed during collection")
    if not connectors.is_native_provider(source.provider):
        raise _conflict(409, "Native provider is not configured")
    return source, source_fence, access_fence


async def _admit_provider_proofs(ctx: NativeAcceptance, verifier: NativeProofVerifier) -> None:
    """Check the capture shape, the connector collection fence and the provider credential revision proof.

    Order: capture shape (422) -> collection fence (409) -> REST revision proof -> provider admission hook.
    The revision proof is mandatory for ``required`` providers, optional for ``optional`` ones and must be
    absent for every other provider.
    """
    from modules.connectors import public as connectors

    source, payload = ctx.source, ctx.payload
    verifier.check_capture(ctx)
    if not await connectors.require_collection_fence(
        ctx.session, source,
        connectors.CollectionFence(
            source_generation=payload.source_generation, connector_revision=payload.connector_revision,
        ),
        lock=True, scope=ctx.scope, multi_workspace_enabled=ctx.multi_workspace_enabled):
        raise _conflict(409, "Connector collection fence is stale")
    revision = ctx.params.credential_revision
    policy = verifier.revision_policy
    if (policy == "required" and revision is None) or (revision is not None and policy == "forbidden"):
        raise _conflict(422, "Provider credential revision proof does not match the source")
    if revision is not None and not await connectors.validate_provider_credential_revision_in_uow(
        ctx.session, source.id, revision, access_fence=ctx.access_fence, source_fence=ctx.source_fence,
        scope=ctx.scope, multi_workspace_enabled=ctx.multi_workspace_enabled,
    ):
        raise _conflict(409, "Provider credential changed during collection")
    await verifier.verify_admission(ctx)


async def _authorize_and_verify_grant(
    ctx: NativeAcceptance, verifier: NativeProofVerifier, *, collector_token: str | None,
    request_ref: CollectionRequestRef | None,
) -> None:
    """Take collector credential rows, authorize the write, then verify the provider grant.

    Visibility preparation already holds the credential rows before Tools, so the credential lock is skipped
    exactly when ``ctx.needs_visibility_fence`` (never reenter that set). Exact bearer eligibility precedes the
    provider grant hook.
    """
    source = ctx.source
    if not ctx.needs_visibility_fence:
        await lock_source_credentials_in_uow(
            ctx.session, source.id, scope=ctx.scope, multi_workspace_enabled=ctx.multi_workspace_enabled,
            access_fence=ctx.access_fence, source_fence=ctx.source_fence,
        )
    await _authorize_collection(
        ctx.session, source_id=source.id, source_generation=ctx.payload.source_generation,
        connector_revision=ctx.payload.connector_revision, collector_token=collector_token,
        request_ref=request_ref, scope=ctx.scope,
    )
    await verifier.verify_grant(ctx)


def _batch_identity(ctx: NativeAcceptance) -> tuple[str, str]:
    """Return ``(payload_hash, batch_key)``; credential operation ids and ``collected_at`` never enter the hash."""
    payload, source = ctx.payload, ctx.source
    stable = {
        "source_id": str(payload.source_id), "source_generation": payload.source_generation,
        "connector_revision": payload.connector_revision, "cursor_before": payload.cursor_before,
        "cursor_after": payload.cursor_after,
        "raw_hashes": [item.raw_update_sha256 for item in payload.telegram_raw_deliveries],
        "telegram_deliveries": [item.model_dump(mode="json") for item in payload.telegram_deliveries],
        "github_segment": ctx.github_proof.model_dump(mode="json") if ctx.github_proof is not None else None,
        "records": [record.model_dump(mode="json", exclude={"collected_at"}) for record in payload.records],
        "coverage": payload.coverage,
    }
    payload_hash = _digest(stable)
    batch_key = f"native:{source.provider}:{payload_hash}"
    if len(batch_key) > _BATCH_KEY_MAX:
        raise _conflict(422, "Native batch key exceeds its bound")
    return payload_hash, batch_key


async def _replay_receipt(
    ctx: NativeAcceptance, state: SourceIngestionState | None, existing: IngestionBatch, *, payload_hash: str,
    request_ref: CollectionRequestRef | None,
) -> NativeCollectionReceipt:
    """Return the historical receipt of an exact replay without re-checking token, cursor or expiry.

    A replay that holds a fresh reservation releases only that exact token, settles the request and publishes
    the replay head (last); a replay without it only reads.
    """
    source, payload, scope = ctx.source, ctx.payload, ctx.scope
    if existing.payload_hash != payload_hash:
        raise _conflict(409, "Native receipt conflicts with an existing batch")
    run = await ctx.session.scalar(select(IngestionRun).where(
        IngestionRun.batch_id == existing.id, IngestionRun.source_id == source.id, *_run_scope(scope)))
    if run is None:
        raise RuntimeError("Native ingestion batch has no run")
    if state is not None and state.collection_lease_token == payload.lease_token:
        # A replay may have acquired a fresh reservation; release only that exact token.
        state.collection_lease_token = None
        state.lease_expires_at = None
        await _settle_request(
            ctx.session, request_ref, outcome="succeeded" if payload.records else "no_changes", run_id=run.id,
            scope=scope, source_id=source.id, source_generation=source.generation,
            connector_revision=payload.connector_revision, batch_id=existing.id, payload_digest=payload_hash,
            cursor_before=payload.cursor_before, cursor_after=payload.cursor_after)
        await commit_with_replay(
            ctx.session, [make_source_change(source.id, source.generation, source.status, scope=scope)],
            scope=scope, multi_workspace_enabled=ctx.multi_workspace_enabled, access_fence=ctx.access_fence)
    count = len(payload.records)
    return _receipt(ctx, batch_id=existing.id, run_id=run.id, queued=bool(count), record_count=count,
                    cursor_after=payload.cursor_after)


def _check_reservation(ctx: NativeAcceptance, state: SourceIngestionState | None) -> datetime:
    """Require a live reservation for new work and return the evaluation instant.

    New work requires: no active run, the exact lease token, an expiry equal to the actual lease and still in
    the future, and the reserved cursor. No state revision field or journal-only authority is invented.
    """
    if state is None or state.collection_lease_token != ctx.payload.lease_token or state.lease_run_id is not None:
        raise _conflict(409, "Native collection reservation is stale")
    now = datetime.now(UTC)
    if (state.lease_expires_at is None or state.lease_expires_at != ctx.lease.expires_at
            or state.lease_expires_at <= now):
        raise _conflict(409, "Native collection reservation expired")
    if state.cursor != ctx.payload.cursor_before:
        raise _conflict(409, "Native collection cursor is stale")
    return now


async def _drop_seen(ctx: NativeAcceptance, provider_records: list[ProviderRecord]) -> list[ProviderRecord]:
    """Drop records whose ``(provider_id, record_hash)`` is already stored.

    Already-stored observations add nothing (documents dedupe by record_hash); an all-seen poll therefore
    takes the empty succeeded path instead of queueing normalization.
    """
    source = ctx.source
    if not provider_records:
        return provider_records
    seen_keys = await seen_record_keys(
        ctx.session, source_id=source.id, source_generation=source.generation, provider=source.provider,
        keys=[native_record_key(rec) for rec, _, _ in provider_records])
    if not seen_keys:
        return provider_records
    return [item for item in provider_records if native_record_key(item[0]) not in seen_keys]


async def _insert_run(
    ctx: NativeAcceptance, provider_records: list[ProviderRecord], *, batch_key: str, payload_hash: str,
    now: datetime,
) -> tuple[IngestionBatch, IngestionRun, IngestionStage]:
    """Insert batch, run, receive stage and de-duplicated observations; flush only.

    The run keeps the original actor and membership. Telegram owner proof is stored beside (not inside) the
    caller record so the accepted hash stays stable.
    """
    source, scope, session = ctx.source, ctx.scope, ctx.session
    batch = IngestionBatch(
        source_id=source.id, batch_key=batch_key, payload_hash=payload_hash, source_generation=source.generation,
    )
    session.add(batch)
    await session.flush()
    run = IngestionRun(
        workspace_id=scope.workspace_id, actor_user_id=_actor_id(scope), membership_revision=scope.membership_revision,
        batch_id=batch.id, source_id=source.id, status="queued" if provider_records else "succeeded",
    )
    session.add(run)
    await session.flush()
    receive_stage = IngestionStage(
        run_id=run.id, stage_key="receive", status="pending" if provider_records else "succeeded",
        result_count=len(provider_records),
    )
    session.add(receive_stage)
    await session.flush()
    seen: set[tuple[str, str, datetime]] = set()
    for record, record_data, telegram_envelope in provider_records:
        record_hash = native_record_key(record)[1]
        identity = (record.provider_id, record_hash, record.observed_at)
        if identity in seen:
            continue
        seen.add(identity)
        observation_payload = dict(record_data)
        if telegram_envelope is not None:
            # Keep owner proof outside the caller record so the accepted hash remains stable.
            observation_payload["_native_telegram"] = telegram_envelope
        session.add(SourceObservation(
            source_id=source.id, batch_id=batch.id, provider_id=record.provider_id, record_hash=record_hash,
            payload=observation_payload, observed_at=record.observed_at, received_at=now,
            collected_at=ctx.payload.collected_at,
        ))
    return batch, run, receive_stage


async def _queue_or_release(
    ctx: NativeAcceptance, state: SourceIngestionState, run: IngestionRun, batch: IngestionBatch,
    receive_stage: IngestionStage, *, has_records: bool, now: datetime, state_update: CollectionStateUpdate | None,
) -> list[ReplayDraft]:
    """Publish the stage request and schedule normalization (records) or release the reservation (no records).

    Mutates the locked state row: with records the lease moves to the run and gets a fresh expiry, without
    records everything is cleared. The cursor is advanced and the checkpoint written in both cases.
    Returns the replay drafts; the caller publishes them last.
    """
    source, scope, payload = ctx.source, ctx.scope, ctx.payload
    changes: list[ReplayDraft] = [make_source_change(source.id, source.generation, source.status, scope=scope)]
    if has_records:
        event = DomainEvent(
            id=uuid7(), type="ingestion.stage.requested", version=1, occurred_at=now, producer="modules.ingestion",
            payload={
                "run_id": str(run.id), "stage_id": str(receive_stage.id),
                "source_generation": source.generation, "connector_revision": payload.connector_revision,
            },
        )
        await publish_event(ctx.session, event, scope=scope, multi_workspace_enabled=ctx.multi_workspace_enabled)
        await ctx.session.flush()
        normalize_stage = await schedule_normalization(
            ctx.session, run, batch, source.generation, now, scope=scope,
            multi_workspace_enabled=ctx.multi_workspace_enabled)
        if normalize_stage is not None:
            changes.append(make_ingestion_change(
                source.id, run.id, run.status, normalize_stage.stage_key, normalize_stage.status, scope=scope))
        state.lease_run_id = run.id
        state.collection_lease_token = None
        state.lease_expires_at = now + COLLECTION_LEASE
    else:
        state.lease_run_id = None
        state.collection_lease_token = None
        state.lease_expires_at = None
    changes.append(make_ingestion_change(
        source.id, run.id, run.status, receive_stage.stage_key, receive_stage.status, scope=scope))
    state.cursor = ctx.cursor_after
    _apply_state_update(state, state_update, payload.connector_revision)
    return changes


async def _accept_new_work(
    ctx: NativeAcceptance, verifier: NativeProofVerifier, state: SourceIngestionState | None, *, payload_hash: str,
    batch_key: str, request_ref: CollectionRequestRef | None, state_update: CollectionStateUpdate | None,
) -> NativeCollectionReceipt:
    """Recheck the live reservation, recompute the provider transition and persist the page (flush only).

    Order: reservation recheck -> provider transition -> record verification -> de-duplication -> inserts ->
    stage request/normalization -> provider settlement (hint ack / pause) -> collection result -> request
    settlement -> replay-head publication (last).
    """
    source, payload, scope = ctx.source, ctx.payload, ctx.scope
    now = _check_reservation(ctx, state)
    assert state is not None  # _check_reservation rejects a missing row
    await verifier.verify_transition(ctx, state)
    provider_records = await _drop_seen(ctx, verifier.verify_records(ctx))
    batch, run, receive_stage = await _insert_run(
        ctx, provider_records, batch_key=batch_key, payload_hash=payload_hash, now=now)
    changes = await _queue_or_release(
        ctx, state, run, batch, receive_stage, has_records=bool(provider_records), now=now, state_update=state_update)
    source_paused = await verifier.settle_provider_state(
        ctx, batch_id=batch.id, has_records=bool(provider_records), changes=changes)
    if not source_paused:
        await sources.record_collection_result_in_uow(
            ctx.session, source.id, source.generation, now, None, no_changes=not provider_records, scope=scope,
            multi_workspace_enabled=ctx.multi_workspace_enabled, access_fence=ctx.access_fence,
            source_fence=ctx.source_fence)
    await _settle_request(
        ctx.session, request_ref,
        outcome="succeeded" if provider_records or payload.telegram_raw_deliveries else "no_changes", run_id=run.id,
        scope=scope, source_id=source.id, source_generation=source.generation,
        connector_revision=payload.connector_revision, batch_id=batch.id, payload_digest=payload_hash,
        cursor_before=payload.cursor_before, cursor_after=ctx.cursor_after, state_update=state_update)
    await commit_with_replay(
        ctx.session, changes, scope=scope, multi_workspace_enabled=ctx.multi_workspace_enabled,
        access_fence=ctx.access_fence)
    return _receipt(ctx, batch_id=batch.id, run_id=run.id, queued=bool(provider_records),
                    record_count=len(provider_records), cursor_after=ctx.cursor_after)


async def accept_native_collection_with_params(
    session: AsyncSession,
    payload: NativeCollectionBatch,
    *,
    params: NativeProviderParams,
    collector_token: str | None,
    lease: ConnectorCollectionLease,
    scope: Scope,
    multi_workspace_enabled: bool,
    request_ref: CollectionRequestRef | None = None,
    state_update: CollectionStateUpdate | None = None,
) -> NativeCollectionReceipt:
    """Authenticate, reclassify, and atomically persist one reserved native page.

    Provider-specific proofs arrive in ``params`` and are verified by the registry entry of the locked Source's
    provider. Phases and lock order (credential -> collector -> grant -> state -> receipt):

    1. The owner-issued lease must equal the scope and the payload; its original workspace configuration
       fence is compared during admission before the Source lock is taken.
    2. Source lock; capture shape, collection fence, credential revision and provider admission proof (the
       Telegram native credential, Alpha world operation, GitHub segment/visibility preparation).
    3. Source credential rows (skipped when visibility preparation already holds them, before Tools), the
       collector/request authorization, then the provider grant (GitHub binding fence, Google/Reddit OAuth).
    4. State row lock, then exact replay lookup, which follows the grant fences and precedes the token/cursor
       recheck. Replay preserves historical receipt semantics and releases only an exact fresh token.
    5. New work only: no run, exact token/cursor/expiry equal to the actual lease and future expiry; the
       provider recomputes proofs/cursor from the live reservation; records are verified, de-duplicated and
       inserted; targeted hints are acknowledged flush-only in the same transaction (an ambiguous missing
       GitHub target acknowledges uncertainty before pausing the Source); replay-head publication is last.

    The function flushes only: the caller owns the transaction. Credential operation ids never enter the
    stable content hash. A validated empty receipt retains G; a late apply returns G+1 solely for replay.

    Raises:
        DomainError: 404/409/413/422 per the checks above; ``RuntimeError`` if a replayed batch lost its run.
    """
    expected_access_fence = _lease_access_fence(lease)
    _lease_scope(lease, scope)
    _check_lease_matches(lease, payload)
    source, source_fence, access_fence = await _admit_source(
        session, payload, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=expected_access_fence,
    )
    ctx = NativeAcceptance(
        session=session, payload=payload, lease=lease, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        params=params, expected_access_fence=expected_access_fence, source=source, source_fence=source_fence,
        access_fence=access_fence,
    )
    verifier = native_verifier_for(source.provider)
    await _admit_provider_proofs(ctx, verifier)
    await _authorize_and_verify_grant(ctx, verifier, collector_token=collector_token, request_ref=request_ref)
    payload_hash, batch_key = _batch_identity(ctx)
    # Provider credential -> collector -> optional provider grant -> state -> receipt order.
    state = await session.scalar(select(SourceIngestionState).where(
        SourceIngestionState.source_id == ctx.source.id,
    ).with_for_update().execution_options(populate_existing=True))
    existing = await session.scalar(select(IngestionBatch).where(
        IngestionBatch.source_id == ctx.source.id, IngestionBatch.batch_key == batch_key,
    ))
    if existing is not None:
        return await _replay_receipt(ctx, state, existing, payload_hash=payload_hash, request_ref=request_ref)
    return await _accept_new_work(
        ctx, verifier, state, payload_hash=payload_hash, batch_key=batch_key,
        request_ref=request_ref, state_update=state_update,
    )


async def accept_native_collection(
    session: AsyncSession,
    payload: NativeCollectionBatch,
    *,
    collector_token: str | None, lease: ConnectorCollectionLease, scope: Scope, multi_workspace_enabled: bool,
    expected_native_operation_id: UUID | None,
    expected_world_credential_operation_id: UUID | None,
    request_ref: CollectionRequestRef | None = None,
    state_update: CollectionStateUpdate | None = None,
    provider_credential_revision: int | None = None,
) -> NativeCollectionReceipt:
    """Compatibility wrapper: pack the legacy provider keywords into :class:`NativeProviderParams`.

    Behaviour is exactly that of :func:`accept_native_collection_with_params`; callers keep the original
    keyword signature (``expected_native_operation_id`` for Telegram, ``expected_world_credential_operation_id``
    for Alpha Vantage, ``provider_credential_revision`` for REST providers). The shape of each capture is
    validated against the locked Source's provider, not here.
    """
    return await accept_native_collection_with_params(
        session, payload,
        params=NativeProviderParams(
            native_operation_id=expected_native_operation_id,
            world_credential_operation_id=expected_world_credential_operation_id,
            credential_revision=provider_credential_revision,
        ),
        collector_token=collector_token, lease=lease, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        request_ref=request_ref, state_update=state_update,
    )
