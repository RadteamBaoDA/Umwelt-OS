"""Batch, crawl and file receipt acceptance (internal; import through ``public``)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from typing import cast as typing_cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ConflictError, NotFoundError
from core.events import DomainEvent
from core.ids import uuid7
from core.realtime import (
    ReplayDraft,
    commit_with_replay,
    make_ingestion_change,
    make_knowledge_change,
    make_source_change,
)
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope, WorkspaceContext
from modules.ingestion._collection import (
    _apply_state_update,
    _authorize_collection,
    _lock_source_projection,
    _settle_request,
)
from modules.ingestion._event_publisher import (
    publish_event,
)
from modules.ingestion._outbox import (
    schedule_normalization,
)
from modules.ingestion._scope import (
    _actor_id,
    _digest,
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
    CrawlReceipt,
    Receipt,
    ReceiveBatch,
)
from modules.knowledge.documents import public as documents
from modules.sources import public as sources
from modules.sources.public import ConnectorSource, SourceFence

if TYPE_CHECKING:
    # Annotation-only: a runtime import would close an import cycle between public contracts.
    from modules.connectors.public import CollectionRequestRef

async def _admit_batch(
    session: AsyncSession, payload: ReceiveBatch, collector_token: str | None, *, scope: Scope,
    multi_workspace_enabled: bool, request_ref: CollectionRequestRef | None,
) -> tuple[ConnectorSource, SourceFence, AccessFence]:
    """Lock the Source projection and authorize a generic (non-native) collection batch.

    Order: Source lock -> active/generation checks -> collector or request authorization -> native-provider
    rejection -> connector batch fence. Every failure is a ``DomainError`` (404/409) raised before any
    duplicate lookup. Returns the detached projection and its fences.
    """
    source, source_fence, access_fence = await _lock_source_projection(session, payload.source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source is None:
        raise NotFoundError(detail="Source not found")
    if source.status != "active":
        raise ConflictError(detail="Source is not active")
    if source.generation != payload.source_generation:
        raise ConflictError(detail="Source generation changed during collection")
    await _authorize_collection(
        session, source_id=payload.source_id, source_generation=payload.source_generation,
        connector_revision=payload.connector_revision, collector_token=collector_token,
        request_ref=request_ref, scope=scope,
    )
    from modules.connectors import public as connectors

    if connectors.is_native_provider(source.provider):
        raise ConflictError(detail="Native provider collection is required")

    if not await connectors.require_batch_fence(
        session,
        source,
        payload.source_generation,
        payload.connector_revision, scope=scope, multi_workspace_enabled=multi_workspace_enabled):
        raise ConflictError(detail="Connector collection fence is stale or required")
    return source, source_fence, access_fence


async def _replay_existing_batch(
    session: AsyncSession, existing: IngestionBatch, payload: ReceiveBatch, payload_hash: str, *,
    source: ConnectorSource, scope: Scope, request_ref: CollectionRequestRef | None,
) -> tuple[IngestionBatch, IngestionRun]:
    """Return an exact duplicate batch with its run; a different payload under the same key is a 409.

    A request-bound caller is settled against the existing run and committed (the only commit on this path);
    a bearer caller only reads. A batch without a run is a hard ``RuntimeError``.
    """
    if existing.payload_hash != payload_hash:
        raise ConflictError(detail="Batch key was already used with different content")
    run = await session.scalar(select(IngestionRun).where(IngestionRun.batch_id == existing.id, IngestionRun.source_id == source.id, *_run_scope(scope)))
    if run is None:
        raise RuntimeError("Ingestion batch has no run")
    if request_ref is not None:
        # Identical content already accepted: this request is satisfied by the existing run.
        await _settle_request(
            session, request_ref, outcome="succeeded", run_id=run.id, scope=scope, source_id=source.id,
            source_generation=source.generation, connector_revision=payload.connector_revision,
            batch_id=existing.id, payload_digest=payload_hash, cursor_before=payload.cursor_before,
            cursor_after=payload.cursor_after)
        await session.commit()
    return existing, run


async def _reserve_collection_slot(
    session: AsyncSession, payload: ReceiveBatch, source: ConnectorSource, source_fence: SourceFence,
    access_fence: AccessFence, *, scope: Scope, multi_workspace_enabled: bool,
) -> tuple[SourceIngestionState, datetime]:
    """Lock (creating if absent) the Source state row and validate that a new run may start.

    Returns the locked state row and the evaluation instant.

    Rejects an inconsistent ownership state, an unexpired lease and a stale cursor with 409, then records the
    collection start through Sources (409 if the Source is no longer active).
    """
    state = await session.get(SourceIngestionState, payload.source_id, with_for_update=True)
    if state is None:
        state = SourceIngestionState(source_id=payload.source_id, cursor=None)
        session.add(state)
        await session.flush()
    now = datetime.now(UTC)
    _require_idle_collection_state(state, payload.cursor_before, now)
    if not await sources.record_collection_started_in_uow(session, payload.source_id, source.generation, now, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence):
        raise ConflictError(detail="Source is not active")
    return state, now


async def _insert_batch_graph(
    session: AsyncSession, payload: ReceiveBatch, source: ConnectorSource, payload_hash: str, now: datetime, *,
    scope: Scope, multi_workspace_enabled: bool,
) -> tuple[IngestionBatch, IngestionRun, IngestionStage]:
    """Insert batch, run, receive stage, the stage-requested outbox event and de-duplicated observations.

    Every distinct provider/content/observation-time record is kept once. Flush only; the caller commits.
    """
    batch = IngestionBatch(
        source_id=payload.source_id, batch_key=payload.batch_key, payload_hash=payload_hash,
        source_generation=source.generation,
    )
    session.add(batch)
    await session.flush()
    run = IngestionRun(workspace_id=scope.workspace_id, actor_user_id=_actor_id(scope), membership_revision=scope.membership_revision, batch_id=batch.id, source_id=payload.source_id, status="queued")
    session.add(run)
    await session.flush()
    stage = IngestionStage(run_id=run.id, stage_key="receive", status="pending")
    session.add(stage)
    await session.flush()
    event = DomainEvent(
        id=uuid7(),
        type="ingestion.stage.requested",
        version=1,
        occurred_at=now,
        producer="modules.ingestion",
        payload={
            "run_id": str(run.id),
            "stage_id": str(stage.id),
            "source_generation": source.generation,
            **({"connector_revision": payload.connector_revision} if payload.connector_revision is not None else {}),
        },
    )
    await publish_event(session, event, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    # Keep every distinct provider/content observation in the accepted batch.
    seen: set[tuple[str, str, datetime]] = set()
    for record in payload.records:
        data = record.model_dump(mode="json")
        record_hash = _digest({"version": record.version, "content": record.content, "metadata": record.metadata})
        identity = (record.provider_id, record_hash, record.observed_at)
        if identity in seen:
            continue
        seen.add(identity)
        session.add(
            SourceObservation(
                source_id=payload.source_id,
                batch_id=batch.id,
                provider_id=record.provider_id,
                record_hash=record_hash,
                payload=data,
                observed_at=record.observed_at,
                received_at=now,
                collected_at=record.collected_at,
            )
        )
    await session.flush()
    return batch, run, stage


async def receive_batch(
    session: AsyncSession,
    payload: ReceiveBatch,
    collector_token: str | None, *, scope: Scope, multi_workspace_enabled: bool,
    request_ref: CollectionRequestRef | None = None, state_update: CollectionStateUpdate | None = None,
) -> tuple[IngestionBatch, IngestionRun]:
    """Authenticate and idempotently accept a fenced collection batch.

    Enforces active source/generation, collector token, and connector fence
    before duplicate lookup. Exact duplicate keys return the existing batch/run
    before new-work cursor/lease checks and without committing new work; native
    provider sources fail closed to the native reservation path. Differing
    payload hashes raise HTTP 409. A new batch deduplicates identical
    provider/hash/observation-time records, advances the cursor and lease, writes
    stage/outbox state, and commits with realtime changes before returning.
    """
    source, source_fence, access_fence = await _admit_batch(
        session, payload, collector_token, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        request_ref=request_ref)
    payload_hash = _digest(payload.model_dump(mode="json"))
    existing = await session.scalar(
        select(IngestionBatch).where(
            IngestionBatch.source_id == payload.source_id,
            IngestionBatch.batch_key == payload.batch_key,
        )
    )
    if existing is not None:
        return await _replay_existing_batch(
            session, existing, payload, payload_hash, source=source, scope=scope, request_ref=request_ref)
    state, now = await _reserve_collection_slot(
        session, payload, source, source_fence, access_fence, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled)
    batch, run, stage = await _insert_batch_graph(
        session, payload, source, payload_hash, now, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    normalize_stage = await schedule_normalization(session, run, batch, source.generation, now, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    result = await session.execute(
        update(SourceIngestionState)
        .where(
            SourceIngestionState.source_id == payload.source_id,
            SourceIngestionState.cursor.is_not_distinct_from(payload.cursor_before),
        )
        .values(
            cursor=payload.cursor_after, lease_run_id=run.id,
            collection_lease_token=None, lease_expires_at=now + COLLECTION_LEASE,
        )
    )
    if typing_cast("CursorResult[Any]", result).rowcount != 1:
        raise ConflictError(detail="Collection cursor changed")
    changes: list[ReplayDraft] = [
        make_source_change(source.id, source.generation, source.status, scope=scope),
        make_ingestion_change(source.id, run.id, run.status, stage.stage_key, stage.status, scope=scope),
    ]
    if normalize_stage is not None:
        changes.append(make_ingestion_change(source.id, run.id, run.status, normalize_stage.stage_key, normalize_stage.status, scope=scope))
    _apply_state_update(state, state_update, payload.connector_revision)
    await _settle_request(
        session, request_ref, outcome="succeeded", run_id=run.id, scope=scope, source_id=source.id,
        source_generation=source.generation, connector_revision=payload.connector_revision, batch_id=batch.id,
        payload_digest=payload_hash, cursor_before=payload.cursor_before, cursor_after=payload.cursor_after,
        state_update=state_update)
    await commit_with_replay(session, changes, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
    await session.refresh(batch)
    await session.refresh(run)
    return batch, run


async def receive_connector_batch(
    session: AsyncSession, payload: ReceiveBatch, collector_token: str | None, *, scope: Scope, multi_workspace_enabled: bool,
    request_ref: CollectionRequestRef | None = None, state_update: CollectionStateUpdate | None = None,
) -> Receipt:
    """Accept a connector batch and return its public run receipt."""
    batch, run = await receive_batch(
        session, payload, collector_token, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        request_ref=request_ref, state_update=state_update)
    return Receipt(workspace_id=scope.workspace_id, batch_id=batch.id, run_id=run.id, status=run.status)


# Sentinel: distinguishes "no expectation" from an expected cursor of None.
_NO_EXPECTED_CURSOR: object = object()


async def accept_collection_no_changes(
    session: AsyncSession, *, source_id: UUID, source_generation: int, connector_revision: int,
    request_ref: CollectionRequestRef, scope: Scope, multi_workspace_enabled: bool,
    state_update: CollectionStateUpdate | None = None, expected_cursor: str | None | object = _NO_EXPECTED_CURSOR,
) -> None:
    """Settle a generic request with no new data in one commit: health, receipt, request outcome and slot.

    ``expected_cursor`` (when supplied) is the cursor the walk started from; if the locked state row
    holds a different cursor the call raises 409 before any write, so a slow no-change settlement
    cannot overwrite a cursor another run advanced meanwhile. Omitted keeps the unguarded behaviour.

    No batch or run is created and last-good observations stay untouched. ``state_update`` may clear a
    finished continuation, store validators and (only here, with the state row locked) advance the
    cursor a resumed walk earned in earlier committed segments. Native
    providers acknowledge emptiness through their own receipt and are refused here.
    """
    source, source_fence, access_fence = await _lock_source_projection(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source is None or source_fence is None:
        raise NotFoundError(detail="Source not found")
    if source.status != "active" or source.generation != source_generation:
        raise ConflictError(detail="Source generation changed during collection")
    from modules.connectors import public as connectors

    if connectors.is_native_provider(source.provider):
        raise ConflictError(detail="Native provider collection is required")
    if not await connectors.require_collection_fence(
        session, source,
        connectors.CollectionFence(source_generation=source_generation, connector_revision=connector_revision),
        lock=True, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    ):
        raise ConflictError(detail="Connector collection fence is stale")
    await _authorize_collection(
        session, source_id=source_id, source_generation=source_generation,
        connector_revision=connector_revision, collector_token=None, request_ref=request_ref, scope=scope)
    state = await session.scalar(select(SourceIngestionState).where(
        SourceIngestionState.source_id == source_id,
    ).with_for_update().execution_options(populate_existing=True))
    if state is None:
        state = SourceIngestionState(source_id=source_id, cursor=None)
        session.add(state)
        await session.flush()
    cursor_before = state.cursor
    if expected_cursor is not _NO_EXPECTED_CURSOR and cursor_before != expected_cursor:
        raise ConflictError(detail="Collection cursor changed")
    if state_update is not None and state_update.cursor_after is not None:
        state.cursor = state_update.cursor_after
    _apply_state_update(state, state_update, connector_revision)
    if not await sources.record_collection_result_in_uow(
        session, source_id, source_generation, datetime.now(UTC), None, no_changes=True,
        access_fence=access_fence, source_fence=source_fence,
        multi_workspace_enabled=multi_workspace_enabled, scope=scope,
    ):
        raise ConflictError(detail="Source is no longer active")
    await _settle_request(
        session, request_ref, outcome="no_changes", run_id=None, scope=scope, source_id=source_id,
        source_generation=source_generation, connector_revision=connector_revision,
        cursor_before=cursor_before, cursor_after=state.cursor, state_update=state_update)
    await commit_with_replay(
        session, [make_source_change(source.id, source.generation, source.status, scope=scope)],
        scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)


def _require_idle_collection_state(state: SourceIngestionState, cursor_before: str | None, now: datetime) -> None:
    """Require a consistent, unleased state row positioned at ``cursor_before`` before a new run may start.

    409 when ownership is inconsistent (a lease owner without an expiry), a live lease exists, or the cursor
    moved. Pure: reads the already locked row only.
    """
    if (
        (state.lease_run_id is not None and state.lease_expires_at is None)
        or (state.collection_lease_token is not None and state.lease_expires_at is None)
    ):
        raise ConflictError(detail="Source collection ownership state is invalid")
    if state.lease_expires_at is not None and state.lease_expires_at > now:
        raise ConflictError(detail="Source already has an active collection run")
    if state.cursor != cursor_before:
        raise ConflictError(detail="Collection cursor is stale")


async def _admit_crawl(
    session: AsyncSession, source_id: UUID, source_generation: int, connector_revision: int, *, scope: Scope,
    multi_workspace_enabled: bool, request_ref: CollectionRequestRef | None,
) -> tuple[ConnectorSource, SourceFence, AccessFence]:
    """Lock the Source projection, reject native providers, take the collection fence and re-prove the request.

    Order: Source lock -> active check -> native rejection -> connector collection fence (``lock=True``) ->
    request-ref authorization (request-bound callers only). 404/409 on any failure.
    """
    source, source_fence, access_fence = await _lock_source_projection(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source is None:
        raise NotFoundError(detail="Source not found")
    if source.status != "active":
        raise ConflictError(detail="Source is not active")
    from modules.connectors import public as connectors
    from modules.connectors.public import CollectionFence

    if connectors.is_native_provider(source.provider):
        raise ConflictError(detail="Native provider collection is required")

    if not await connectors.require_collection_fence(
        session,
        source,
        CollectionFence(
            source_generation=source_generation,
            connector_revision=connector_revision,
        ),
        lock=True, scope=scope, multi_workspace_enabled=multi_workspace_enabled):
        raise ConflictError(detail="Connector collection fence is stale")
    if request_ref is not None:
        await _authorize_collection(
            session, source_id=source_id, source_generation=source_generation,
            connector_revision=connector_revision, collector_token=None, request_ref=request_ref, scope=scope)
    return source, source_fence, access_fence


async def _replay_crawl(
    session: AsyncSession, existing: IngestionBatch, *, source: ConnectorSource, source_generation: int,
    connector_revision: int, cursor_before: str | None, scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence, request_ref: CollectionRequestRef | None,
) -> CrawlReceipt:
    """Return the receipt of an identical crawl queued within the same minute (settling a bound request).

    A request-bound caller is settled against the existing run and published with replay; a bearer caller
    only reads. A batch without a run is a hard ``RuntimeError``.
    """
    run = await session.scalar(select(IngestionRun).where(IngestionRun.batch_id == existing.id, IngestionRun.source_id == source.id, *_run_scope(scope)))
    if run is None:
        raise RuntimeError("Crawl batch has no run")
    if request_ref is not None:  # same minute, same work: the request still resolves to the existing run
        await _settle_request(
            session, request_ref, outcome="succeeded", run_id=run.id, scope=scope, source_id=source.id,
            source_generation=source_generation, connector_revision=connector_revision,
            batch_id=existing.id, payload_digest=existing.payload_hash, cursor_before=cursor_before,
            cursor_after=cursor_before)
        await commit_with_replay(session, [make_source_change(source.id, source.generation, source.status, scope=scope)],
                                 scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
    return CrawlReceipt(workspace_id=scope.workspace_id, run_id=run.id)


async def _insert_crawl_graph(
    session: AsyncSession, source: ConnectorSource, key: str, configuration: dict[str, object], *,
    source_generation: int, connector_revision: int, cursor_before: str | None, now: datetime, scope: Scope,
    multi_workspace_enabled: bool,
) -> tuple[IngestionBatch, IngestionRun, IngestionStage]:
    """Insert the crawl batch, run and ``collect_web`` stage and publish the ``connector.crawl.requested`` event.

    Flush only; the caller moves the state lease and commits with replay.
    """
    batch = IngestionBatch(
        source_id=source.id, batch_key=key, payload_hash=_digest(configuration),
        source_generation=source.generation,
    )
    session.add(batch)
    await session.flush()
    run = IngestionRun(workspace_id=scope.workspace_id, actor_user_id=_actor_id(scope), membership_revision=scope.membership_revision, batch_id=batch.id, source_id=source.id, status="queued")
    session.add(run)
    await session.flush()
    stage = IngestionStage(run_id=run.id, stage_key="collect_web", status="pending")
    session.add(stage)
    await session.flush()
    event = DomainEvent(
        id=uuid7(),
        type="connector.crawl.requested",
        version=1,
        occurred_at=now,
        producer="modules.connectors",
        payload={
            "source_id": str(source.id),
            "source_generation": source_generation,
            "connector_revision": connector_revision,
            "run_id": str(run.id),
            "stage_id": str(stage.id),
            "cursor_before": cursor_before,
            "configuration": configuration,
        },
    )
    await publish_event(session, event, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return batch, run, stage


async def queue_connector_crawl(
    session: AsyncSession,
    source_id: UUID,
    source_generation: int,
    connector_revision: int,
    cursor_before: str | None,
    configuration: dict[str, object], *, scope: Scope, multi_workspace_enabled: bool,
    request_ref: CollectionRequestRef | None = None,
) -> CrawlReceipt:
    """Idempotently queue a generic crawl keyed by source, cursor, config, and minute.

    A matching batch/run receipt returns without the new-work commit. New work
    persists its stage, request event, cursor lease and realtime updates in this
    function's commit; stale source, connector revision, cursor, or active-lease
    checks raise HTTP 404/409; native sources must use their provider adapter. With ``request_ref`` the
    admitted request is re-proved under lock and settled against the queued run in the same commit.
    """
    source, source_fence, access_fence = await _admit_crawl(
        session, source_id, source_generation, connector_revision, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, request_ref=request_ref)
    state = await session.get(SourceIngestionState, source_id, with_for_update=True)
    if state is None:
        state = SourceIngestionState(source_id=source_id, cursor=None)
        session.add(state)
        await session.flush()
    now = datetime.now(UTC)
    minute = now.replace(second=0, microsecond=0).isoformat()
    key = "crawl:" + _digest({"source_id": str(source_id), "cursor": cursor_before, "config": configuration, "minute": minute})
    existing = await session.scalar(
        select(IngestionBatch).where(IngestionBatch.source_id == source_id, IngestionBatch.batch_key == key)
    )
    if existing is not None:
        return await _replay_crawl(
            session, existing, source=source, source_generation=source_generation,
            connector_revision=connector_revision, cursor_before=cursor_before, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, request_ref=request_ref)
    _require_idle_collection_state(state, cursor_before, now)
    if not await sources.record_collection_started_in_uow(session, source_id, source.generation, now, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence):
        raise ConflictError(detail="Source is not active")
    batch, run, stage = await _insert_crawl_graph(
        session, source, key, configuration, source_generation=source_generation,
        connector_revision=connector_revision, cursor_before=cursor_before, now=now, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled)
    state.lease_run_id = run.id
    state.collection_lease_token = None
    state.lease_expires_at = now + COLLECTION_LEASE
    await _settle_request(
        session, request_ref, outcome="succeeded", run_id=run.id, scope=scope, source_id=source_id,
        source_generation=source_generation, connector_revision=connector_revision, batch_id=batch.id,
        payload_digest=batch.payload_hash, cursor_before=cursor_before, cursor_after=cursor_before)
    await commit_with_replay(session, [
        make_source_change(source.id, source.generation, source.status, scope=scope),
        make_ingestion_change(source.id, run.id, run.status, stage.stage_key, stage.status, scope=scope),
    ], scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
    await session.refresh(run)
    return CrawlReceipt(workspace_id=scope.workspace_id, run_id=run.id)


def _validate_upload_capture(
    source_id: UUID, *, scope: Scope, expected_access_fence: AccessFence, expected_source_fence: SourceFence,
) -> None:
    """Require the route's captured fences to describe this exact principal, workspace epoch and Source.

    Pure (no I/O) and run before the first SourceSet acquisition; any mismatch, wrong type or a Source-bound job
    scope pointing at another Source/generation is a 409 ``Upload capture proof is invalid``.
    """
    if (not isinstance(scope, (WorkspaceContext, InternalJobScope))
            or not isinstance(expected_access_fence, AccessFence)
            or not isinstance(expected_source_fence, SourceFence)
            or expected_access_fence.workspace_id != scope.workspace_id
            or expected_access_fence.user_id != _actor_id(scope)
            or expected_access_fence.membership_revision != scope.membership_revision
            or expected_source_fence.id != source_id
            or expected_source_fence.workspace_id != scope.workspace_id
            or isinstance(scope, InternalJobScope) and scope.source_id is not None
            and (scope.source_id != source_id or scope.source_generation != expected_source_fence.generation)):
        raise ConflictError(detail="Upload capture proof is invalid")


async def receive_file(
    session: AsyncSession,
    source_id: UUID,
    document_id: UUID,
    filename: str,
    mime_type: str,
    raw_uri: str,
    size: int,
    digest: str, *, scope: Scope, multi_workspace_enabled: bool,
    expected_access_fence: AccessFence, expected_source_fence: SourceFence,
) -> tuple[IngestionRun, bool]:
    """Publish an upload only under its original principal/access/Source capture; return run/created.

    Owner-write route captures both required fences before upload I/O, releases SQL locks
    for staging, and owns discarded raw-byte cleanup/exact-session admission. Validate the
    original actor/workspace/epoch and Source identity before the first SourceSet acquisition,
    forward the original AccessFence to its admission CAS, and compare the full locked
    SourceFence before reads/effects. No current-fence fallback upgrades the capture. Commit
    the existing idempotent run (False) or new document/batch/stage/event (True); stale proof,
    inactive Source or reuse after canonical deletion fails409 before publication.
    Only a proven new-batch branch prepares Document/URI/normalized identities under these
    held fences before collection-start and Ingestion DML. Late Document initialization
    consumes the exact original tuple without earlier reentry; retries retain the unchanged
    canonical identity/run False path and acquire no new raw-URI preparation.
    """
    _validate_upload_capture(
        source_id, scope=scope, expected_access_fence=expected_access_fence, expected_source_fence=expected_source_fence)
    locked = await sources.lock_source_set(session, (source_id,), scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, expected_access_fence=expected_access_fence)
    source = locked.fences[0]
    source_fence, access_fence = source, locked.access_fence
    if source_fence != expected_source_fence or access_fence != expected_access_fence:
        raise ConflictError(detail="Upload capture proof is stale")
    if source is None or source.status != "active":
        raise ConflictError(detail="Source is not active")
    batch_key = f"file:{digest}"
    existing = await session.scalar(
        select(IngestionBatch).where(IngestionBatch.source_id == source_id, IngestionBatch.batch_key == batch_key)
    )
    if existing is not None:
        if existing.payload_hash != digest:
            raise ConflictError(detail="Upload identity conflicts with stored content")
        if not await documents.has_document_identity(session, source_id, f"file:{digest}", scope=scope, multi_workspace_enabled=multi_workspace_enabled):
            raise ConflictError(detail="This file was previously ingested and its document was deleted")
        run = await session.scalar(select(IngestionRun).where(IngestionRun.batch_id == existing.id, IngestionRun.source_id == source.id, *_run_scope(scope)))
        if run is None:
            raise RuntimeError("Ingestion batch has no run")
        await session.commit()
        return run, False

    await documents.prepare_uploaded_document_in_uow(
        session, source_id=source_id, document_id=document_id, external_id=f"file:{digest}",
        raw_uri=raw_uri, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    now = datetime.now(UTC)
    if not await sources.record_collection_started_in_uow(session, source_id, source.generation, now, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence):
        raise ConflictError(detail="Source is not active")
    batch = IngestionBatch(
        source_id=source_id, batch_key=batch_key, payload_hash=digest,
        source_generation=source.generation,
    )
    session.add(batch)
    await session.flush()
    run = IngestionRun(workspace_id=scope.workspace_id, actor_user_id=_actor_id(scope), membership_revision=scope.membership_revision, batch_id=batch.id, source_id=source_id, status="queued")
    session.add(run)
    await session.flush()
    stage = IngestionStage(run_id=run.id, stage_key="parse_file", status="pending")
    session.add(stage)
    await session.flush()
    metadata = {"filename": filename, "raw_sha256": digest, "raw_size": size, "format": mime_type}
    stored_document_id = await documents.add_uploaded_document(
        session, source_id, filename[:500] or "Uploaded file", mime_type, raw_uri, metadata, f"file:{digest}", document_id,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    event = DomainEvent(
        id=uuid7(),
        type="document.file.uploaded",
        version=1,
        occurred_at=now,
        producer="modules.ingestion",
        payload={"run_id": str(run.id), "stage_id": str(stage.id), "document_id": str(stored_document_id), "raw_uri": raw_uri, "mime_type": mime_type, "source_generation": source.generation},
    )
    await publish_event(session, event, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    await commit_with_replay(session, [
        make_source_change(source.id, source.generation, source.status, scope=scope),
        make_ingestion_change(source.id, run.id, run.status, stage.stage_key, stage.status, scope=scope),
        make_knowledge_change(source_id, stored_document_id, 1, scope=scope),
    ], scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
    await session.refresh(run)
    return run, True
