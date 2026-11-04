"""Ingestion purge preparation and retention sweeps (internal; import through ``public``)."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import String, and_, cast, delete, func, not_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ConflictError
from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.ingestion._scope import (
    _actor_id,
    _admit_ingestion_scope,
)
from modules.ingestion.models import (
    CollectionReceipt,
    CollectorCredential,
    EventOutbox,
    IngestionBatch,
    IngestionRun,
    IngestionStage,
    ObservationNormalization,
    SourceIngestionState,
    SourceObservation,
)
from modules.knowledge.documents import public as documents
from modules.knowledge.observations import public as observations_public
from modules.sources import public as sources
from modules.sources.public import SourceFence


async def _validate_source_ingestion_purge_in_uow(
    session: AsyncSession, source_id: UUID, *, scope: InternalJobScope,
    multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
) -> None:
    """Compare exact internal Source subject and both actual current fences without locks.

    Caller already holds real account/workspace/membership and Source locks throughout its
    purge transaction. DTO construction is not lock proof. Archived matching Sources are
    permitted; active-only credential/retained-evidence acquiring wrappers are inappropriate.
    Missing/malformed proof or any changed status/local_only/generation/access field fails
    before effects, without upgrading epoch/generation, acquiring early locks or committing.
    """
    if (not isinstance(scope, InternalJobScope) or not isinstance(access_fence, AccessFence)
            or not isinstance(source_fence, SourceFence) or not isinstance(source_id, UUID)
            or scope.source_id != source_id or scope.source_generation is None
            or source_fence.id != source_id or source_fence.workspace_id != scope.workspace_id
            or source_fence.generation != scope.source_generation):
        raise ConflictError(detail="Source ingestion purge proof is invalid")
    current_access = await _admit_ingestion_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    current_source = await sources.get_source_fence(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if current_access != access_fence or current_source is None or current_source != source_fence:
        raise ConflictError(detail="Source ingestion purge proof is stale")


async def _source_ingestion_purge_lineage(
    session: AsyncSession, source_id: UUID, *, scope: InternalJobScope,
) -> None:
    """Abort contradictory own-table FK lineage before a complete Source purge can widen.

    Read only scalar EXISTS under already-held admission/Source serialization; no foreign
    ORM, payload, locks, mutation or history list. Check both direct Source rows and children
    reached through actual batch/run/stage/observation cascades, including foreign state lease
    references to selected runs. All historical epochs/generations remain eligible. Document/
    version SET NULL may clear journal references after preparation and is not a mismatch.
    """
    batch_ids = select(IngestionBatch.id).where(IngestionBatch.source_id == source_id)
    run_ids = select(IngestionRun.id).where(
        IngestionRun.source_id == source_id, IngestionRun.workspace_id == scope.workspace_id,
    )
    observation_ids = select(SourceObservation.id).where(SourceObservation.source_id == source_id)
    stage_ids = select(IngestionStage.id).where(IngestionStage.run_id.in_(run_ids))
    invalid_run = select(IngestionRun.id).where(
        or_(IngestionRun.source_id == source_id, IngestionRun.batch_id.in_(batch_ids)),
        or_(IngestionRun.source_id != source_id, IngestionRun.workspace_id != scope.workspace_id,
            IngestionRun.actor_user_id != scope.actor_user_id, IngestionRun.batch_id.not_in(batch_ids)),
    ).exists()
    invalid_observation = select(SourceObservation.id).where(
        or_(SourceObservation.source_id == source_id, SourceObservation.batch_id.in_(batch_ids)),
        or_(SourceObservation.source_id != source_id, SourceObservation.batch_id.not_in(batch_ids)),
    ).exists()
    matching_journal_parents = select(SourceObservation.id).join(
        IngestionRun, IngestionRun.batch_id == SourceObservation.batch_id,
    ).join(IngestionStage, IngestionStage.run_id == IngestionRun.id).join(
        IngestionBatch, IngestionBatch.id == IngestionRun.batch_id,
    ).where(
        SourceObservation.id == ObservationNormalization.observation_id,
        IngestionRun.id == ObservationNormalization.run_id,
        IngestionStage.id == ObservationNormalization.stage_id,
        SourceObservation.source_id == source_id, IngestionBatch.source_id == source_id,
        IngestionRun.source_id == source_id, IngestionRun.workspace_id == scope.workspace_id,
        IngestionRun.actor_user_id == scope.actor_user_id,
        IngestionBatch.source_generation == ObservationNormalization.source_generation,
    ).correlate(ObservationNormalization).exists()
    invalid_journal = select(ObservationNormalization.id).where(
        or_(ObservationNormalization.source_id == source_id,
            ObservationNormalization.observation_id.in_(observation_ids),
            ObservationNormalization.run_id.in_(run_ids), ObservationNormalization.stage_id.in_(stage_ids)),
        or_(ObservationNormalization.source_id != source_id,
            ObservationNormalization.workspace_id != scope.workspace_id, ~matching_journal_parents),
    ).exists()
    invalid_state = select(SourceIngestionState.source_id).where(or_(
        and_(SourceIngestionState.source_id != source_id, SourceIngestionState.lease_run_id.in_(run_ids)),
        and_(SourceIngestionState.source_id == source_id, SourceIngestionState.lease_run_id.is_not(None),
             SourceIngestionState.lease_run_id.not_in(run_ids)),
    )).exists()
    if await session.scalar(select(or_(invalid_run, invalid_observation, invalid_journal, invalid_state))):
        raise ConflictError(detail="Source ingestion purge lineage is inconsistent")


async def _lock_ingestion_purge_identities(
    session: AsyncSession, source_id: UUID, *, scope: InternalJobScope,
) -> None:
    """Lock the seven complete own row sets in their fixed local order, returning no data.

    Caller has freshly validated exact Source/access proof and own cascade lineage. Each
    MATERIALIZED identity-only ordered FOR UPDATE CTE is exhausted by a scalar COUNT in
    PostgreSQL, so Python memory stays constant and no payload/config/secret is collected.
    No cap/SKIP LOCKED/partial commit omits history; all locks remain held through one apply.
    Credentials include revoked rows; state absence stays absent; historical run epochs and
    batch generations are not restricted to the purge's newly archived generation/revision.
    """
    batch_ids = select(IngestionBatch.id).where(IngestionBatch.source_id == source_id)
    run_ids = select(IngestionRun.id).where(
        IngestionRun.source_id == source_id, IngestionRun.workspace_id == scope.workspace_id,
        IngestionRun.batch_id.in_(batch_ids),
    )
    observation_ids = select(SourceObservation.id).where(
        SourceObservation.source_id == source_id, SourceObservation.batch_id.in_(batch_ids),
    )
    stage_ids = select(IngestionStage.id).where(IngestionStage.run_id.in_(run_ids))
    queries = (
        select(CollectorCredential.token_hash).where(CollectorCredential.source_id == source_id)
        .order_by(CollectorCredential.token_hash),
        select(SourceIngestionState.source_id).where(SourceIngestionState.source_id == source_id)
        .order_by(SourceIngestionState.source_id),
        select(IngestionBatch.id).where(IngestionBatch.source_id == source_id).order_by(IngestionBatch.id),
        select(IngestionRun.id).where(IngestionRun.id.in_(run_ids)).order_by(IngestionRun.id),
        select(IngestionStage.id).where(IngestionStage.run_id.in_(run_ids))
        .order_by(IngestionStage.run_id, IngestionStage.stage_key, IngestionStage.id),
        select(SourceObservation.id).where(SourceObservation.id.in_(observation_ids)).order_by(SourceObservation.id),
        select(ObservationNormalization.id).where(
            ObservationNormalization.workspace_id == scope.workspace_id,
            ObservationNormalization.source_id == source_id,
            ObservationNormalization.observation_id.in_(observation_ids),
            ObservationNormalization.run_id.in_(run_ids), ObservationNormalization.stage_id.in_(stage_ids),
        ).order_by(ObservationNormalization.id),
    )
    for query in queries:
        locked = query.with_for_update().cte("source_ingestion_purge_locks").prefix_with("MATERIALIZED")
        await session.scalar(select(func.count()).select_from(locked))


async def prepare_source_ingestion_purge_in_uow(
    session: AsyncSession, source_id: UUID, *, scope: InternalJobScope,
    multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
) -> None:
    """Prepare complete Ingestion roots/children after Source+Documents/URI, before operation/outbox.

    Caller retains real account/workspace/Source locks and the same transaction through D
    cleanup and cancellation. Fresh nonlocking fences and own FK lineage must match; then
    lock credentials -> state -> batches -> runs -> stages -> observations -> journals,
    each in stable identity order. No outbox lock, mutation, token, commit or external I/O.
    Memory use is bounded; complete atomic SQL/transaction work grows with Source history.
    """
    await _validate_source_ingestion_purge_in_uow(session, source_id, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence)
    await _source_ingestion_purge_lineage(session, source_id, scope=scope)
    await _lock_ingestion_purge_identities(session, source_id, scope=scope)
    await _source_ingestion_purge_lineage(session, source_id, scope=scope)


async def cancel_and_purge_source_ingestion(
    session: AsyncSession, source_id: UUID, *, scope: InternalJobScope,
    multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
) -> None:
    """Apply complete prepared Source cancellation after claimed D canonical cleanup, without early locks.

    Caller holds real access/Source, prepared Documents/URI and seven Ingestion row sets,
    exact purge operation and complete sorted existing event union; original queued dispatch
    CAS succeeded before D apply. Revalidate fences/own lineage nonlocking, then fail all
    matching historical run events (all statuses/epochs/generations), delete observations and
    batches with existing cascades, clear only existing state leases and revoke all credentials.
    Keep cursor, retained outbox payload/principal/dispatch/schedule and purge receipts intact.
    No missing state insertion, preparation/acquiring call, commit or external effect. D has
    already removed derived structured observations before ingestion-observation FK cascades.
    """
    await _validate_source_ingestion_purge_in_uow(session, source_id, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence)
    await _source_ingestion_purge_lineage(session, source_id, scope=scope)
    # A lifecycle purge cancels all historical generations/epochs of this verified
    # Source, rather than only the newly archived generation of its operation.
    run_ids = select(cast(IngestionRun.id, String)).where(
        IngestionRun.source_id == source_id, IngestionRun.workspace_id == scope.workspace_id,
    )
    await session.execute(
        update(EventOutbox)
        .where(EventOutbox.workspace_id == scope.workspace_id,
               EventOutbox.actor_user_id == _actor_id(scope),
               EventOutbox.payload["run_id"].astext.in_(run_ids))
        .values(status="failed")
    )
    await session.execute(delete(SourceObservation).where(SourceObservation.source_id == source_id))
    await session.execute(delete(IngestionBatch).where(IngestionBatch.source_id == source_id))
    # Preparation holds the existing state; reread without reacquiring roots after outboxes.
    state = await session.scalar(select(SourceIngestionState).where(
        SourceIngestionState.source_id == source_id,
    ).execution_options(populate_existing=True))
    if state is not None:
        state.lease_run_id = None
        state.collection_lease_token = None
        state.lease_expires_at = None
    await session.execute(update(CollectorCredential).where(CollectorCredential.source_id == source_id)
                          .values(revoked_at=datetime.now(UTC)))


async def purge_expired_collection_receipts(session: AsyncSession, *, now: datetime | None = None, limit: int = 500) -> int:
    """Delete expired receipts whose request can no longer be recovered; the caller commits.

    A receipt is the recovery evidence for its request, so one is kept while the connectors-owned
    request is still queued/running even past ``retain_until``. Returns the number deleted.
    """
    from modules.connectors import public as connectors

    now = now or datetime.now(UTC)
    rows = (await session.execute(
        select(CollectionReceipt.id, CollectionReceipt.request_id).where(CollectionReceipt.retain_until < now)
        .order_by(CollectionReceipt.retain_until).limit(limit))).all()
    if not rows:
        return 0
    recoverable = await connectors.recoverable_collection_request_ids(session, [row.request_id for row in rows])
    doomed = [row.id for row in rows if row.request_id not in recoverable]
    if doomed:
        await session.execute(delete(CollectionReceipt).where(CollectionReceipt.id.in_(doomed)))
    return len(doomed)


# Fan-out stage events nobody reads after delivery; other types stay (automations read
# document.version.ready by cursor and cleanup receipts check fixed event ids).
_PURGEABLE_OUTBOX_TYPES = ("ingestion.stage.requested", "ingestion.normalize.requested")


OUTBOX_RETENTION = timedelta(days=7)


async def purge_delivered_outbox(session: AsyncSession, *, now: datetime | None = None, limit: int = 1000) -> int:
    """Delete one bounded slice of long-delivered ingestion stage events; the caller commits.

    Only ``delivered`` rows of the two ingestion stage types older than 7 days qualify, so pending,
    queued and failed rows (still retryable or diagnostic) and every other event type are untouched.
    ``id IN (subselect LIMIT n)`` bounds each pass; the hourly cron drains a backlog. Returns rows deleted.
    """
    now = now or datetime.now(UTC)
    doomed = select(EventOutbox.id).where(
        EventOutbox.status == "delivered",
        EventOutbox.type.in_(_PURGEABLE_OUTBOX_TYPES),
        EventOutbox.created_at < now - OUTBOX_RETENTION,
    ).limit(limit)
    result = await session.execute(delete(EventOutbox).where(EventOutbox.id.in_(doomed)))
    return int(getattr(result, "rowcount", 0) or 0)


async def purge_expired_datasource_data(
    session: AsyncSession, *, now: datetime | None = None, limit: int = 500,
) -> int:
    """Delete one bounded slice of expired connector datasource history per source retention policy.

    Preserves all current observations (``Observation.is_current == True``), all user-authored
    documents (sources of type ``file`` or ``manual``; such sources get no retention cutoff), and any document with an owner bookmark
    (``DocumentInteraction.bookmarked_at IS NOT NULL``). Uses ``FOR UPDATE SKIP LOCKED`` subqueries
    so retention cleanup never blocks concurrent ingestion or queries. The caller commits.

    Args:
        session: Active asynchronous database session.
        now: Reference UTC timestamp (defaults to ``datetime.now(UTC)``).
        limit: Maximum rows to delete per table in a single pass.

    Returns:
        Total number of expired observation, source_observation, and document version rows deleted.
    """
    now = now or datetime.now(UTC)
    cutoffs = await sources.list_retention_cutoffs(session, now=now)
    if not cutoffs:
        return 0

    deleted = await observations_public.purge_expired_noncurrent_observations(session, cutoffs, limit=limit)

    # Keep source observations still backing a current observation.
    src_obs_cutoff_predicate = or_(
        *(and_(SourceObservation.source_id == sid, SourceObservation.observed_at < cutoff) for sid, cutoff in cutoffs)
    )
    doomed_src_obs = (
        select(SourceObservation.id)
        .where(
            not_(observations_public.current_observation_references(
                SourceObservation.id, field="ingestion_observation_id",
            )),
            src_obs_cutoff_predicate,
        )
        .order_by(SourceObservation.observed_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    src_obs_res = await session.execute(
        delete(SourceObservation).where(SourceObservation.id.in_(doomed_src_obs))
    )
    deleted += int(getattr(src_obs_res, "rowcount", 0) or 0)

    deleted += await documents.purge_expired_document_versions(
        session, cutoffs, limit=limit,
        is_protected=lambda col: observations_public.current_observation_references(
            col, field="document_version_id",
        ),
    )
    return deleted
