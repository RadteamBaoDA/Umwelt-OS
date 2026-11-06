"""Worker admission, ordered row locking and claim comparison for ingestion and normalization jobs."""

from __future__ import annotations

import hashlib
import json
from typing import Any, NoReturn, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ConflictError, NotFoundError
from core.events.dispatch import valid_event_envelope
from core.workspaces.public import read_access_fence
from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.connectors import public as connectors
from modules.connectors.public import CollectionFence
from modules.ingestion import public as ingestion_api
from modules.ingestion._worker_state import (
    MAX_STAGE_ATTEMPTS,
    NORMALIZATION_BATCH_RECORDS,
    NORMALIZATION_VERSION,
    NormalizationCandidate,
    NormalizationPreparationConflict,
    WorkerClaim,
    WorkerState,
    _Admitted,
    _Discovery,
    _same_worker_claim,
)
from modules.ingestion.models import (
    EventOutbox,
    IngestionBatch,
    IngestionRun,
    IngestionStage,
    ObservationNormalization,
    SourceIngestionState,
    SourceObservation,
)
from modules.ingestion.schemas import EventDelivery
from modules.knowledge.documents import public as documents
from modules.knowledge.documents.public import MEASUREMENT_PROVIDER_IDS
from modules.knowledge.observations import public as observations
from modules.sources import public as sources
from modules.sources.public import ConnectorSource, SourceFence


async def _capture_normalization_retry_claim_in_uow(
    session: AsyncSession,
    *,
    delivery: EventDelivery,
    scope: InternalJobScope,
    access_fence: AccessFence,
    source_fence: SourceFence,
    multi_workspace_enabled: bool,
) -> WorkerClaim | None:
    """Read an original normalization comparison before D/I/O/outbox preparation.

    Caller actually holds admitted account/workspace/Source locks. Scalar discovery
    proves exact run/batch/stage lineage and completed predecessors without mutation.
    Terminal, missing or malformed work supplies no retry authority. Every field must
    be freshly compared under the final materializing or retry-only loader's locks.
    """
    if type(multi_workspace_enabled) is not bool:
        raise TypeError("Actual workspace rollout flag required")
    if (
        delivery.type != "ingestion.normalize.requested"
        or delivery.producer != "modules.ingestion"
        or delivery.status != "queued"
        or delivery.dispatched_at is None
        or not valid_event_envelope(delivery, scope)
        or scope.source_id != source_fence.id
        or scope.source_generation != source_fence.generation
        or source_fence.status != "active"
    ):
        return None
    run_id, stage_id = UUID(delivery.payload["run_id"]), UUID(delivery.payload["stage_id"])
    row = (
        await session.execute(
            select(
                IngestionRun.id.label("run_id"),
                IngestionRun.batch_id,
                IngestionRun.status.label("run_status"),
                IngestionStage.id.label("stage_id"),
                IngestionStage.attempts,
                IngestionStage.status.label("stage_status"),
                IngestionStage.lease_expires_at,
                SourceIngestionState.lease_run_id,
                SourceIngestionState.collection_lease_token,
            )
            .select_from(IngestionRun)
            .join(IngestionBatch, IngestionBatch.id == IngestionRun.batch_id)
            .join(IngestionStage, IngestionStage.run_id == IngestionRun.id)
            .outerjoin(
                SourceIngestionState, SourceIngestionState.source_id == IngestionRun.source_id
            )
            .where(
                IngestionRun.id == run_id,
                IngestionRun.workspace_id == scope.workspace_id,
                IngestionRun.actor_user_id == scope.actor_user_id,
                IngestionRun.membership_revision == scope.membership_revision,
                IngestionRun.source_id == source_fence.id,
                IngestionBatch.source_id == source_fence.id,
                IngestionBatch.source_generation == source_fence.generation,
                IngestionStage.id == stage_id,
                IngestionStage.stage_key == "normalize",
            )
        )
    ).one_or_none()
    if (
        row is None
        or row.run_status in {"failed", "succeeded", "needs_ocr"}
        or row.stage_status in {"failed", "succeeded"}
        or type(row.attempts) is not int
        or row.attempts < 0
    ):
        return None
    if (
        await session.scalar(
            select(IngestionStage.id)
            .where(
                IngestionStage.run_id == run_id,
                IngestionStage.stage_key.in_(("receive", "collect_web")),
                IngestionStage.status != "succeeded",
            )
            .limit(1)
        )
        is not None
    ):
        return None
    try:
        payload_hash = hashlib.sha256(
            json.dumps(
                delivery.payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        ).hexdigest()
    except (ValueError, TypeError):
        return None
    return WorkerClaim(
        scope=scope,
        access_fence=access_fence,
        source_fence=source_fence,
        event_id=delivery.id,
        event_type=delivery.type,
        producer=delivery.producer,
        dispatched_at=delivery.dispatched_at,
        payload_hash=payload_hash,
        run_id=row.run_id,
        batch_id=row.batch_id,
        stage_id=row.stage_id,
        attempts=row.attempts,
        stage_status=row.stage_status,
        stage_lease_expires_at=row.lease_expires_at,
        state_lease_run_id=row.lease_run_id,
        state_collection_token=row.collection_lease_token,
    )


async def _lock_normalization_retry_event(
    session: AsyncSession,
    identifier: UUID,
    *,
    multi_workspace_enabled: bool,
    expected: WorkerClaim,
) -> WorkerState | None:
    """Lock only original normalization scheduling roots for post-rollback disposition.

    Fresh original account/workspace/Source admission and full dispatch/stage/state CAS
    precede every write. No D/O, candidate journal, accepted parent or parser preparation
    is reachable; the sole caller may only consume the durable failure budget and replay.
    Empty owner snapshots cannot be used to continue content materialization.
    """
    if type(multi_workspace_enabled) is not bool:
        raise TypeError("Actual workspace rollout flag required")
    if (
        identifier != expected.event_id
        or expected.event_type != "ingestion.normalize.requested"
        or expected.producer != "modules.ingestion"
        or expected.scope.source_id is None
    ):
        return None
    scope = await ingestion_api.resolve_ingestion_event_scope(
        session,
        identifier,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if scope != expected.scope:
        return None
    access_fence = await read_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if access_fence != expected.access_fence:
        raise ConflictError("Original normalization admission changed")
    if (
        await ingestion_api.resolve_ingestion_run_scope(
            session, expected.run_id, multi_workspace_enabled=multi_workspace_enabled
        )
        != scope
    ):
        return None
    try:
        locked = await sources.lock_source_set(
            session,
            (cast(UUID, scope.source_id),),
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            expected_access_fence=expected.access_fence,
        )
    except NotFoundError:
        # Source generation advanced or Source vanished: treat as lost admission, not a worker crash.
        return None
    source = locked.fences[0]
    if (
        source is None
        or source != expected.source_fence
        or source.status != "active"
        or source.generation != scope.source_generation
    ):
        return None
    projection = await sources.get_connector_source(
        session, source.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if (
        projection is None
        or projection.status != source.status
        or projection.generation != source.generation
    ):
        return None
    state = await session.get(
        SourceIngestionState, source.id, with_for_update=True, populate_existing=True
    )
    run = await session.scalar(
        select(IngestionRun)
        .where(
            IngestionRun.id == expected.run_id,
            IngestionRun.workspace_id == scope.workspace_id,
            IngestionRun.actor_user_id == scope.actor_user_id,
            IngestionRun.membership_revision == scope.membership_revision,
            IngestionRun.source_id == source.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        run is None
        or run.batch_id != expected.batch_id
        or run.status in {"failed", "succeeded", "needs_ocr"}
    ):
        return None
    batch = await session.scalar(
        select(IngestionBatch)
        .where(
            IngestionBatch.id == expected.batch_id,
            IngestionBatch.source_id == source.id,
            IngestionBatch.source_generation == source.generation,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    stage = await session.scalar(
        select(IngestionStage)
        .where(
            IngestionStage.id == expected.stage_id,
            IngestionStage.run_id == expected.run_id,
            IngestionStage.stage_key == "normalize",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if batch is None or stage is None or stage.status in {"failed", "succeeded"}:
        return None
    if (
        await session.scalar(
            select(IngestionStage.id)
            .where(
                IngestionStage.run_id == run.id,
                IngestionStage.stage_key.in_(("receive", "collect_web")),
                IngestionStage.status != "succeeded",
            )
            .limit(1)
        )
        is not None
    ):
        return None
    event = await session.scalar(
        select(EventOutbox)
        .where(
            EventOutbox.id == identifier,
            EventOutbox.workspace_id == scope.workspace_id,
            EventOutbox.actor_user_id == scope.actor_user_id,
            EventOutbox.membership_revision == scope.membership_revision,
            EventOutbox.status == "queued",
            EventOutbox.dispatched_at == expected.dispatched_at,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        event is None
        or not valid_event_envelope(event, scope)
        or event.payload.get("run_id") != str(run.id)
        or event.payload.get("stage_id") != str(stage.id)
    ):
        return None
    work = WorkerState(
        scope, locked.access_fence, source, projection, state, run, batch, stage, event
    )
    return work if _same_worker_claim(work, expected) else None


async def _admit_worker_delivery(
    session: AsyncSession,
    identifier: UUID,
    *,
    multi_workspace_enabled: bool,
    event_types: tuple[str, ...],
    expected: WorkerClaim | None,
) -> _Admitted | None:
    """Resolve the retained principal, recheck the original access fence and validate the queued envelope.

    Reads only (no domain row locks). Returns ``None`` for a missing/successor/changed event; raises a 409
    when ``expected`` carries an access fence that no longer matches (original admission lost).
    """
    scope = await ingestion_api.resolve_ingestion_event_scope(
        session,
        identifier,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if scope is None or (expected is not None and scope != expected.scope):
        return None
    access_fence = await read_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if expected is not None and access_fence != expected.access_fence:
        raise ConflictError("Original worker admission changed")
    delivery = await ingestion_api.get_event_delivery(
        session, identifier, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if (
        delivery is None
        or delivery.type not in event_types
        or delivery.status != "queued"
        or delivery.dispatched_at is None
    ):
        return None
    if not valid_event_envelope(delivery, scope):
        return None
    if expected is not None and (
        delivery.id != expected.event_id
        or delivery.type != expected.event_type
        or delivery.producer != expected.producer
        or delivery.dispatched_at != expected.dispatched_at
        or hashlib.sha256(
            json.dumps(
                delivery.payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        ).hexdigest()
        != expected.payload_hash
    ):
        # Reject changed preparation identities before any earlier owner-resource lock.
        return None
    try:
        run_id = UUID(delivery.payload["run_id"])
        stage_id = UUID(delivery.payload["stage_id"])
    except (KeyError, ValueError, TypeError, AttributeError):
        return None
    run_scope = await ingestion_api.resolve_ingestion_run_scope(
        session, run_id, multi_workspace_enabled=multi_workspace_enabled
    )
    if run_scope != scope:
        return None
    return _Admitted(scope, access_fence, delivery, run_id, stage_id)


async def _lock_worker_source(
    session: AsyncSession,
    admitted: _Admitted,
    *,
    multi_workspace_enabled: bool,
    expected: WorkerClaim | None,
) -> tuple[Any, SourceFence, ConnectorSource] | None:
    """Lock the Source set, compare the original Source fence and read the detached connector projection.

    A crawl event additionally takes the Connector collection fence (``lock=True``) and rejects native
    providers. Returns ``(locked_set, source_fence, projection)`` or ``None`` when any comparison fails.
    """
    scope, delivery = admitted.scope, admitted.delivery
    try:
        locked = await sources.lock_source_set(
            session,
            (cast(UUID, scope.source_id),),
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            expected_access_fence=admitted.access_fence,
        )
    except NotFoundError:
        # Generation advanced or Source gone: the dispatcher gate fails the outbox row; no work here.
        return None
    source = locked.fences[0]
    if source is None or source.status != "active" or source.generation != scope.source_generation:
        return None
    if expected is not None and source != expected.source_fence:
        return None
    projection = await sources.get_connector_source(
        session, source.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if (
        projection is None
        or projection.status != source.status
        or projection.generation != source.generation
    ):
        return None
    if delivery.type == "connector.crawl.requested":
        revision = delivery.payload.get("connector_revision")
        if (
            connectors.is_native_provider(projection.provider)
            or type(revision) is not int
            or revision < 1
            or not await connectors.require_collection_fence(
                session,
                projection,
                CollectionFence(source_generation=source.generation, connector_revision=revision),
                lock=True,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        ):
            return None
    return locked, source, projection


async def _prepare_upload_document(
    session: AsyncSession,
    admitted: _Admitted,
    source: SourceFence,
    *,
    multi_workspace_enabled: bool,
) -> bool:
    """Prepare the existing Document and its URI lifecycle for an upload event; ``False`` means no work.

    Every phase prepares the Document before I roots. The stored key must lie under this workspace's prefix (or
    the legacy prefix); a malformed envelope raises ``ValueError``.
    """
    scope, delivery = admitted.scope, admitted.delivery
    document_id = UUID(delivery.payload["document_id"])
    raw_uri, mime_type = delivery.payload.get("raw_uri"), delivery.payload.get("mime_type")
    if not isinstance(raw_uri, str) or not isinstance(mime_type, str):
        # ValueError (not TypeError) is part of the established contract for a malformed envelope.
        raise ValueError("Upload envelope lacks stored extraction identity")
    expected_prefix = f"workspaces/{scope.workspace_id}/documents/{document_id}/"
    legacy_prefix = f"documents/{document_id}/"
    if not raw_uri.startswith((expected_prefix, legacy_prefix)):
        raise ValueError("Upload storage key differs from retained document scope")
    return bool(
        await documents.lock_document_for_extraction(
            session,
            document_id,
            source.id,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=admitted.access_fence,
            source_fence=source,
            expected_raw_uri=raw_uri,
            expected_mime_type=mime_type,
        )
    )


async def _discover_normalization(
    session: AsyncSession,
    claim: WorkerClaim,
    admitted: _Admitted,
    source: SourceFence,
    *,
    multi_workspace_enabled: bool,
) -> _Discovery:
    """Discover up to 32 pending journal identities nonlockingly, then prepare D's sorted keys before I.

    The returned query is re-executed under held locks to compare the exact lineage set.
    """
    scope = admitted.scope
    query = (
        select(
            ObservationNormalization.id,
            SourceObservation.id,
            SourceObservation.provider_id,
            SourceObservation.record_hash,
            ObservationNormalization.workspace_id,
            ObservationNormalization.source_id,
            ObservationNormalization.source_generation,
            ObservationNormalization.run_id,
            SourceObservation.batch_id,
            ObservationNormalization.stage_id,
            ObservationNormalization.normalization_version,
            SourceObservation.source_id,
        )
        .select_from(ObservationNormalization)
        .join(SourceObservation, SourceObservation.id == ObservationNormalization.observation_id)
        .where(
            ObservationNormalization.workspace_id == scope.workspace_id,
            ObservationNormalization.source_id == source.id,
            ObservationNormalization.source_generation == source.generation,
            ObservationNormalization.run_id == admitted.run_id,
            ObservationNormalization.stage_id == admitted.stage_id,
            ObservationNormalization.normalization_version == NORMALIZATION_VERSION,
            ObservationNormalization.disposition == "pending",
            SourceObservation.source_id == source.id,
            SourceObservation.batch_id == claim.batch_id,
        )
        .order_by(SourceObservation.provider_id, SourceObservation.id)
        .limit(NORMALIZATION_BATCH_RECORDS)
    )
    candidates = tuple(
        NormalizationCandidate(*tuple(row)) for row in (await session.execute(query)).all()
    )
    document_preparation = await documents.prepare_normalized_document_keys(
        session,
        source.id,
        tuple(sorted({item.external_id for item in candidates})),
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=admitted.access_fence,
        source_fence=source,
    )
    return _Discovery(query, candidates, claim.batch_id, document_preparation)


async def _lock_run_batch_stage(
    session: AsyncSession,
    admitted: _Admitted,
    source: SourceFence,
) -> tuple[SourceIngestionState | None, IngestionRun, IngestionBatch, IngestionStage] | None:
    """Lock state, then the exact run, batch and stage rows (in that order); ``None`` if any is missing."""
    scope = admitted.scope
    state = await session.get(
        SourceIngestionState, source.id, with_for_update=True, populate_existing=True
    )
    run = await session.scalar(
        select(IngestionRun)
        .where(
            IngestionRun.id == admitted.run_id,
            IngestionRun.workspace_id == scope.workspace_id,
            IngestionRun.actor_user_id == scope.actor_user_id,
            IngestionRun.membership_revision == scope.membership_revision,
            IngestionRun.source_id == source.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if run is None:
        return None
    batch = await session.scalar(
        select(IngestionBatch)
        .where(
            IngestionBatch.id == run.batch_id,
            IngestionBatch.source_id == source.id,
            IngestionBatch.source_generation == source.generation,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    stage_key = {
        "ingestion.stage.requested": "receive",
        "connector.crawl.requested": "collect_web",
        "ingestion.normalize.requested": "normalize",
        "document.file.uploaded": "parse_file",
    }[admitted.delivery.type]
    stage = await session.scalar(
        select(IngestionStage)
        .where(
            IngestionStage.id == admitted.stage_id,
            IngestionStage.run_id == run.id,
            IngestionStage.stage_key == stage_key,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if batch is None or stage is None:
        return None
    return state, run, batch, stage


async def _lock_normalization_rows(
    session: AsyncSession,
    claim: WorkerClaim,
    discovery: _Discovery,
    admitted: _Admitted,
    source: SourceFence,
    projection: ConnectorSource,
    run: IngestionRun,
    batch: IngestionBatch,
    stage: IngestionStage,
    *,
    multi_workspace_enabled: bool,
) -> tuple[tuple[tuple[ObservationNormalization, SourceObservation], ...], Any]:
    """Lock the discovered journal/accepted rows in id order, compare the lineage set and prepare Observations.

    Prepares the complete discovered owner sets (including the O insert's accepted FK parents). Any change
    between discovery and the held locks raises a ``NormalizationPreparationConflict`` carrying ``claim``.
    Returns the rows in accepted order plus the Observations preparation (measurement providers only).
    """
    scope, candidates = admitted.scope, discovery.candidates
    if run.batch_id != discovery.batch_id:
        raise NormalizationPreparationConflict("normalization_batch_changed", claim)
    await session.execute(
        select(ObservationNormalization)
        .where(
            ObservationNormalization.id.in_(tuple(item.journal_id for item in candidates)),
            ObservationNormalization.workspace_id == scope.workspace_id,
            ObservationNormalization.source_id == source.id,
            ObservationNormalization.source_generation == source.generation,
            ObservationNormalization.run_id == run.id,
            ObservationNormalization.stage_id == stage.id,
            ObservationNormalization.normalization_version == NORMALIZATION_VERSION,
        )
        .order_by(ObservationNormalization.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    await session.execute(
        select(SourceObservation)
        .where(
            SourceObservation.id.in_(tuple(item.observation_id for item in candidates)),
            SourceObservation.source_id == source.id,
            SourceObservation.batch_id == batch.id,
        )
        .order_by(SourceObservation.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    refreshed_candidates = tuple(
        NormalizationCandidate(*cast(Any, tuple(row)))
        for row in (await session.execute(discovery.query)).all()
    )
    if refreshed_candidates != candidates:
        raise NormalizationPreparationConflict("normalization_candidate_set_changed", claim)
    normalization_rows = tuple(
        (progress, observation)
        for progress, observation in (
            await session.execute(
                discovery.query.with_only_columns(
                    ObservationNormalization,
                    SourceObservation,
                ).execution_options(populate_existing=True)
            )
        ).all()
    )
    # Content reload must still have the exact identities compared under those held rows.
    if (
        tuple(
            NormalizationCandidate(
                progress.id,
                observation.id,
                observation.provider_id,
                observation.record_hash,
                progress.workspace_id,
                progress.source_id,
                progress.source_generation,
                progress.run_id,
                observation.batch_id,
                progress.stage_id,
                progress.normalization_version,
                observation.source_id,
            )
            for progress, observation in normalization_rows
        )
        != candidates
    ):
        raise NormalizationPreparationConflict("normalization_candidate_lineage_changed", claim)
    observation_preparation = None
    if projection.provider in MEASUREMENT_PROVIDER_IDS:
        observation_preparation = await observations.prepare_ingestion_observation_keys(
            session,
            source.id,
            tuple(
                sorted(
                    {(item.external_id, item.observation_id) for item in candidates},
                    key=lambda item: (item[0], str(item[1])),
                )
            ),
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=admitted.access_fence,
            source_fence=source,
        )
    return normalization_rows, observation_preparation


async def _lock_worker_outbox(
    session: AsyncSession,
    identifier: UUID,
    admitted: _Admitted,
    run: IngestionRun,
    stage: IngestionStage,
) -> EventOutbox | None:
    """Lock the outbox row last and require it to equal the earlier detached envelope and the locked lineage.

    A normalization event whose receive/browser predecessor has not succeeded yields no work: intake
    publishes both stages together, and normalization must not release the Source run lease while its
    predecessor still owns collection settlement.
    """
    scope, delivery = admitted.scope, admitted.delivery
    event = await session.scalar(
        select(EventOutbox)
        .where(
            EventOutbox.id == identifier,
            EventOutbox.workspace_id == scope.workspace_id,
            EventOutbox.actor_user_id == scope.actor_user_id,
            EventOutbox.membership_revision == scope.membership_revision,
            EventOutbox.status == "queued",
            EventOutbox.dispatched_at == delivery.dispatched_at,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if event is None or not valid_event_envelope(event, scope):
        return None
    if (
        event.type != delivery.type
        or event.version != delivery.version
        or event.producer != delivery.producer
        or event.payload != delivery.payload
    ):
        # Earlier owner preparation was for this exact admitted detached envelope only.
        return None
    if event.payload.get("run_id") != str(run.id) or event.payload.get("stage_id") != str(stage.id):
        return None
    if (
        event.type == "ingestion.normalize.requested"
        and await session.scalar(
            select(IngestionStage.id)
            .where(
                IngestionStage.run_id == run.id,
                IngestionStage.stage_key.in_(("receive", "collect_web")),
                IngestionStage.status != "succeeded",
            )
            .limit(1)
        )
        is not None
    ):
        return None
    return event


async def _lock_worker_event(
    session: AsyncSession,
    identifier: UUID,
    *,
    multi_workspace_enabled: bool,
    event_types: tuple[str, ...],
    expected: WorkerClaim | None = None,
) -> WorkerState | None:
    """Resolve/admit retained event+run identity before content or domain locks.

    Owner resolvers inspect bounded scalar identity first. Recheck the original access
    fence before Source, then Connector (crawl). Parser singleton Document/URI/MIME
    preparation precedes state, exact batch/run/stage and outbox, including recovery.
    Normalization discovers32 identities nonlockingly, prepares D before I, then locks
    exact journals/accepted parents, compares the selected lineage set, prepares O and
    only then locks outbox. Changed preparation never adds an earlier lock late.
    Its pre-D scalar WorkerClaim is only comparison data: final locked rows must match;
    eligible preparation failures carry it after rollback to the I-only retry loader.
    Existing five-failure budgets bypass content work through terminal settlement only.
    Require queued timestamp, producer/type/version/full payload principal and exact
    run/batch/Source generation lineage. A successor claim or lost original access returns
    no work; it never borrows another actor/default or upgrades an epoch.

    Lock order (unchanged by the helper split): admission reads -> Source set (+ Connector fence for crawl)
    -> Document (upload) -> state -> run -> batch -> stage -> journal/accepted rows -> Observations keys ->
    outbox.
    """
    admitted = await _admit_worker_delivery(
        session,
        identifier,
        multi_workspace_enabled=multi_workspace_enabled,
        event_types=event_types,
        expected=expected,
    )
    if admitted is None:
        return None
    sourced = await _lock_worker_source(
        session, admitted, multi_workspace_enabled=multi_workspace_enabled, expected=expected
    )
    if sourced is None:
        return None
    locked, source, projection = sourced
    delivery = admitted.delivery
    normalization_claim, proceed = await _capture_normalization_claim(
        session,
        admitted,
        source,
        multi_workspace_enabled=multi_workspace_enabled,
        expected=expected,
    )
    if not proceed:
        return None
    try:
        if delivery.type == "document.file.uploaded" and not await _prepare_upload_document(
            session, admitted, source, multi_workspace_enabled=multi_workspace_enabled
        ):
            return None
        discovery = None
        if normalization_claim is not None:
            discovery = await _discover_normalization(
                session,
                normalization_claim,
                admitted,
                source,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        locked_rows = await _lock_run_batch_stage(session, admitted, source)
        if locked_rows is None:
            return None
        state, run, batch, stage = locked_rows
        normalization_rows: tuple[tuple[ObservationNormalization, SourceObservation], ...] = ()
        observation_preparation = None
        if discovery is not None:
            normalization_rows, observation_preparation = await _lock_normalization_rows(
                session,
                cast(WorkerClaim, normalization_claim),
                discovery,
                admitted,
                source,
                projection,
                run,
                batch,
                stage,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        event = await _lock_worker_outbox(session, identifier, admitted, run, stage)
        if event is None:
            return None
        work = WorkerState(
            admitted.scope,
            locked.access_fence,
            source,
            projection,
            state,
            run,
            batch,
            stage,
            event,
            normalization_rows,
            discovery.document_preparation if discovery else None,
            observation_preparation,
        )
        return work if _claim_matches(work, normalization_claim, expected) else None
    except (OperationalError, IntegrityError, RuntimeError) as exc:
        _raise_preparation_conflict(exc, normalization_claim)


def _claim_matches(
    work: WorkerState, normalization_claim: WorkerClaim | None, expected: WorkerClaim | None
) -> bool:
    """Final comparison of the locked rows against the pre-D claim and the caller's expected claim.

    A normalization event without a pre-D claim may only serve an already-terminal stage (the idempotent path
    that needs no D preparation); with a claim, the locked rows must equal it. A caller-supplied ``expected``
    claim must equal the complete snapshot too.
    """
    if work.event.type == "ingestion.normalize.requested":
        if normalization_claim is None:
            if work.stage.status not in {"succeeded", "failed"}:
                return False
        elif not _same_worker_claim(work, normalization_claim):
            return False
    return expected is None or _same_worker_claim(work, expected)


def _raise_preparation_conflict(
    exc: Exception, normalization_claim: WorkerClaim | None
) -> NoReturn:
    """Translate a preparation-time database/runtime failure into the carried-claim conflict, never swallowing it.

    Without a normalization claim the original error propagates. A conflict must carry the same claim that was
    captured (else ``RuntimeError``); any other eligible failure is wrapped so the I-only retry loader can spend
    one durable attempt under that claim.
    """
    if normalization_claim is None:
        raise exc
    if isinstance(exc, NormalizationPreparationConflict):
        if exc.claim != normalization_claim:
            raise RuntimeError("Normalization preparation comparisons disagree") from exc
        raise exc
    raise NormalizationPreparationConflict(
        "normalization_preparation_conflict", normalization_claim
    ) from exc


async def _capture_normalization_claim(
    session: AsyncSession,
    admitted: _Admitted,
    source: SourceFence,
    *,
    multi_workspace_enabled: bool,
    expected: WorkerClaim | None,
) -> tuple[WorkerClaim | None, bool]:
    """Capture the pre-D normalization comparison; the flag is ``False`` when the load must yield no work.

    Only normalization events have a claim. A caller's ``expected`` claim must equal it, and a spent retry budget
    raises ``NormalizationPreparationConflict`` so the terminal settlement path (not content work) runs.
    """
    if admitted.delivery.type != "ingestion.normalize.requested":
        return None, True
    claim = await _capture_normalization_retry_claim_in_uow(
        session,
        delivery=admitted.delivery,
        scope=admitted.scope,
        access_fence=admitted.access_fence,
        source_fence=source,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if expected is not None and claim != expected:
        return claim, False
    if claim is not None and claim.attempts >= MAX_STAGE_ATTEMPTS:
        raise NormalizationPreparationConflict("normalization_retry_exhausted", claim)
    return claim, True
