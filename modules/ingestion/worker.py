from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from arq import Retry
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.chunking import chunk_text
from core.config import Settings
from core.events import DomainEvent
from core.heavy_work import bounded_heavy_work
from core.realtime import (
    ReplayDraft,
    commit_with_replay,
    make_ingestion_change,
    make_knowledge_change,
    make_source_change,
)
from core.storage import cleanup_orphaned_files, storage_path
from core.telemetry import count, set_trace, timed
from core.workspaces.public import read_access_fence
from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.connectors.public import CollectionFence, ConnectorRecord
from modules.ingestion import public as ingestion_api
from modules.ingestion.dispatcher import valid_event_envelope
from modules.ingestion.models import (
    COLLECTION_LEASE,
    EventOutbox,
    IngestionBatch,
    IngestionRun,
    IngestionStage,
    ObservationNormalization,
    SourceIngestionState,
    SourceObservation,
)
from modules.ingestion.parsers import parse_file_bounded
from modules.ingestion.schemas import EventDelivery, IngestionRecord
from modules.knowledge.documents import public as documents
from modules.knowledge.documents.schemas import NormalizedDocumentInput
from modules.knowledge.observations import public as observations
from modules.sources import public as sources
from modules.sources.schemas import ConnectorSource, SourceFence

logger = logging.getLogger("bbd.worker")
STAGE_TIMEOUT_SECONDS = 120
MAX_STAGE_ATTEMPTS = 5
NORMALIZATION_VERSION = 1
NORMALIZATION_BATCH_RECORDS = 32
NORMALIZATION_BATCH_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class WorkerClaim:
    """Retain one original admitted dispatch/stage attempt across external work.

    Captured access/Source fences and payload hash are comparisons, not authority. Each
    phase must resolve original durable identity, lock in order and match every field.
    """
    scope: InternalJobScope
    access_fence: AccessFence
    source_fence: SourceFence
    event_id: UUID
    event_type: str
    producer: str
    dispatched_at: datetime
    payload_hash: str
    run_id: UUID
    batch_id: UUID
    stage_id: UUID
    attempts: int
    stage_status: str
    stage_lease_expires_at: datetime | None
    state_lease_run_id: UUID | None
    state_collection_token: UUID | None


@dataclass(frozen=True)
class NormalizationCandidate:
    """Detach one bounded journal/accepted-key identity with its complete scoped lineage."""
    journal_id: UUID
    observation_id: UUID
    external_id: str
    record_hash: str
    workspace_id: UUID
    source_id: UUID
    source_generation: int
    run_id: UUID
    batch_id: UUID
    stage_id: UUID
    normalization_version: int
    observation_source_id: UUID


class NormalizationPreparationConflict(RuntimeError):
    """Carry only a bounded code and detached original comparison after a failed attempt."""

    def __init__(self, code: str, claim: WorkerClaim) -> None:
        """Retain the original comparison, never owner snapshots, ORM rows or authority."""
        super().__init__(code)
        self.code = code
        self.claim = claim


@dataclass(frozen=True)
class WorkerState:
    """Transaction-local rows and finite comparison snapshots after D/I/O/outbox preparation.

    Owner snapshots never prove locks; preparation APIs acquired the actual existing rows.
    Normalization rows keep accepted record order while lock acquisition uses sorted IDs.
    """
    scope: InternalJobScope
    access_fence: AccessFence
    source: SourceFence
    projection: ConnectorSource
    state: SourceIngestionState | None
    run: IngestionRun
    batch: IngestionBatch
    stage: IngestionStage
    event: EventOutbox
    normalization_rows: tuple[tuple[ObservationNormalization, SourceObservation], ...] = ()
    document_preparation: documents.NormalizedDocumentPreparation | None = None
    observation_preparation: observations.IngestionObservationPreparation | None = None


def _worker_flag(ctx: dict[str, object]) -> bool:
    """Require the configured boolean; no absent/default rollout context is accepted."""
    enabled = cast(Settings, ctx["settings"]).multi_workspace_enabled
    if type(enabled) is not bool:
        raise TypeError("Actual workspace rollout flag required")
    return enabled


def _capture_worker_claim(work: WorkerState) -> WorkerClaim:
    """Detach the exact queued timestamp and current stage attempt before releasing locks."""
    if work.event.dispatched_at is None:
        raise ValueError("A durable dispatch claim is required")
    return WorkerClaim(
        scope=work.scope, access_fence=work.access_fence, source_fence=work.source,
        event_id=work.event.id, event_type=work.event.type, producer=work.event.producer,
        dispatched_at=work.event.dispatched_at,
        payload_hash=hashlib.sha256(json.dumps(work.event.payload, sort_keys=True,
            separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest(),
        run_id=work.run.id, batch_id=work.batch.id, stage_id=work.stage.id,
        attempts=work.stage.attempts, stage_status=work.stage.status,
        stage_lease_expires_at=work.stage.lease_expires_at,
        state_lease_run_id=work.state.lease_run_id if work.state is not None else None,
        state_collection_token=work.state.collection_lease_token if work.state is not None else None,
    )


def _same_worker_claim(work: WorkerState, claim: WorkerClaim) -> bool:
    """Compare the complete immutable original dispatch/stage snapshot; never rebase it."""
    return _capture_worker_claim(work) == claim


async def _capture_normalization_retry_claim_in_uow(
    session: AsyncSession, *, delivery: EventDelivery,
    scope: InternalJobScope, access_fence: AccessFence,
    source_fence: SourceFence, multi_workspace_enabled: bool,
) -> WorkerClaim | None:
    """Read an original normalization comparison before D/I/O/outbox preparation.

    Caller actually holds admitted account/workspace/Source locks. Scalar discovery
    proves exact run/batch/stage lineage and completed predecessors without mutation.
    Terminal, missing or malformed work supplies no retry authority. Every field must
    be freshly compared under the final materializing or retry-only loader's locks.
    """
    if type(multi_workspace_enabled) is not bool:
        raise TypeError("Actual workspace rollout flag required")
    if (delivery.type != "ingestion.normalize.requested" or delivery.producer != "modules.ingestion"
            or delivery.status != "queued" or delivery.dispatched_at is None
            or not valid_event_envelope(delivery, scope) or scope.source_id != source_fence.id
            or scope.source_generation != source_fence.generation or source_fence.status != "active"):
        return None
    run_id, stage_id = UUID(delivery.payload["run_id"]), UUID(delivery.payload["stage_id"])
    row = (await session.execute(select(
        IngestionRun.id.label("run_id"), IngestionRun.batch_id, IngestionRun.status.label("run_status"),
        IngestionStage.id.label("stage_id"), IngestionStage.attempts,
        IngestionStage.status.label("stage_status"), IngestionStage.lease_expires_at,
        SourceIngestionState.lease_run_id, SourceIngestionState.collection_lease_token,
    ).select_from(IngestionRun).join(IngestionBatch, IngestionBatch.id == IngestionRun.batch_id)
        .join(IngestionStage, IngestionStage.run_id == IngestionRun.id)
        .outerjoin(SourceIngestionState, SourceIngestionState.source_id == IngestionRun.source_id).where(
            IngestionRun.id == run_id, IngestionRun.workspace_id == scope.workspace_id,
            IngestionRun.actor_user_id == scope.actor_user_id,
            IngestionRun.membership_revision == scope.membership_revision,
            IngestionRun.source_id == source_fence.id, IngestionBatch.source_id == source_fence.id,
            IngestionBatch.source_generation == source_fence.generation,
            IngestionStage.id == stage_id, IngestionStage.stage_key == "normalize",
        ))).one_or_none()
    if (row is None or row.run_status in {"failed", "succeeded", "needs_ocr"}
            or row.stage_status in {"failed", "succeeded"}
            or type(row.attempts) is not int or row.attempts < 0):
        return None
    if await session.scalar(select(IngestionStage.id).where(
        IngestionStage.run_id == run_id, IngestionStage.stage_key.in_(("receive", "collect_web")),
        IngestionStage.status != "succeeded",
    ).limit(1)) is not None:
        return None
    try:
        payload_hash = hashlib.sha256(json.dumps(delivery.payload, sort_keys=True,
            separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    except (ValueError, TypeError):
        return None
    return WorkerClaim(
        scope=scope, access_fence=access_fence, source_fence=source_fence,
        event_id=delivery.id, event_type=delivery.type, producer=delivery.producer,
        dispatched_at=delivery.dispatched_at, payload_hash=payload_hash,
        run_id=row.run_id, batch_id=row.batch_id, stage_id=row.stage_id,
        attempts=row.attempts, stage_status=row.stage_status, stage_lease_expires_at=row.lease_expires_at,
        state_lease_run_id=row.lease_run_id, state_collection_token=row.collection_lease_token,
    )


async def _lock_normalization_retry_event(
    session: AsyncSession, identifier: UUID, *,
    multi_workspace_enabled: bool, expected: WorkerClaim,
) -> WorkerState | None:
    """Lock only original normalization scheduling roots for post-rollback disposition.

    Fresh original account/workspace/Source admission and full dispatch/stage/state CAS
    precede every write. No D/O, candidate journal, accepted parent or parser preparation
    is reachable; the sole caller may only consume the durable failure budget and replay.
    Empty owner snapshots cannot be used to continue content materialization.
    """
    from modules.settings.public import admit_write

    if type(multi_workspace_enabled) is not bool:
        raise TypeError("Actual workspace rollout flag required")
    if (identifier != expected.event_id or expected.event_type != "ingestion.normalize.requested"
            or expected.producer != "modules.ingestion" or expected.scope.source_id is None):
        return None
    await admit_write(session, "ingestion_normalization_retry", str(identifier))
    scope = await ingestion_api.resolve_ingestion_event_scope(
        session, identifier, multi_workspace_enabled=multi_workspace_enabled,
    )
    if scope != expected.scope:
        return None
    access_fence = await read_access_fence(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if access_fence != expected.access_fence:
        raise HTTPException(status_code=409, detail="Original normalization admission changed")
    if await ingestion_api.resolve_ingestion_run_scope(session, expected.run_id,
        multi_workspace_enabled=multi_workspace_enabled) != scope:
        return None
    locked = await sources.lock_source_set(session, (scope.source_id,), scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, expected_access_fence=expected.access_fence)
    source = locked.fences[0]
    if (source is None or source != expected.source_fence or source.status != "active"
            or source.generation != scope.source_generation):
        return None
    projection = await sources.get_connector_source(session, source.id, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled)
    if projection is None or projection.status != source.status or projection.generation != source.generation:
        return None
    state = await session.get(SourceIngestionState, source.id, with_for_update=True, populate_existing=True)
    run = await session.scalar(select(IngestionRun).where(
        IngestionRun.id == expected.run_id, IngestionRun.workspace_id == scope.workspace_id,
        IngestionRun.actor_user_id == scope.actor_user_id,
        IngestionRun.membership_revision == scope.membership_revision, IngestionRun.source_id == source.id,
    ).with_for_update().execution_options(populate_existing=True))
    if run is None or run.batch_id != expected.batch_id or run.status in {"failed", "succeeded", "needs_ocr"}:
        return None
    batch = await session.scalar(select(IngestionBatch).where(
        IngestionBatch.id == expected.batch_id, IngestionBatch.source_id == source.id,
        IngestionBatch.source_generation == source.generation,
    ).with_for_update().execution_options(populate_existing=True))
    stage = await session.scalar(select(IngestionStage).where(
        IngestionStage.id == expected.stage_id, IngestionStage.run_id == expected.run_id,
        IngestionStage.stage_key == "normalize",
    ).with_for_update().execution_options(populate_existing=True))
    if batch is None or stage is None or stage.status in {"failed", "succeeded"}:
        return None
    if await session.scalar(select(IngestionStage.id).where(
        IngestionStage.run_id == run.id, IngestionStage.stage_key.in_(("receive", "collect_web")),
        IngestionStage.status != "succeeded",
    ).limit(1)) is not None:
        return None
    event = await session.scalar(select(EventOutbox).where(
        EventOutbox.id == identifier, EventOutbox.workspace_id == scope.workspace_id,
        EventOutbox.actor_user_id == scope.actor_user_id, EventOutbox.membership_revision == scope.membership_revision,
        EventOutbox.status == "queued", EventOutbox.dispatched_at == expected.dispatched_at,
    ).with_for_update().execution_options(populate_existing=True))
    if (event is None or not valid_event_envelope(event, scope)
            or event.payload.get("run_id") != str(run.id) or event.payload.get("stage_id") != str(stage.id)):
        return None
    work = WorkerState(scope, locked.access_fence, source, projection, state, run, batch, stage, event)
    return work if _same_worker_claim(work, expected) else None


async def _lock_worker_event(
    session: AsyncSession, identifier: UUID, *, multi_workspace_enabled: bool,
    event_types: tuple[str, ...], expected: WorkerClaim | None = None,
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
    """
    from modules.settings.public import admit_write

    await admit_write(session, "ingestion_worker", str(identifier))
    scope = await ingestion_api.resolve_ingestion_event_scope(
        session, identifier, multi_workspace_enabled=multi_workspace_enabled,
    )
    if scope is None or (expected is not None and scope != expected.scope):
        return None
    access_fence = await read_access_fence(session, scope=scope,
                                          multi_workspace_enabled=multi_workspace_enabled)
    if expected is not None and access_fence != expected.access_fence:
        raise HTTPException(status_code=409, detail="Original worker admission changed")
    delivery = await ingestion_api.get_event_delivery(session, identifier, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled)
    if (delivery is None or delivery.type not in event_types or delivery.status != "queued"
            or delivery.dispatched_at is None):
        return None
    if not valid_event_envelope(delivery, scope):
        return None
    if expected is not None and (
        delivery.id != expected.event_id or delivery.type != expected.event_type
        or delivery.producer != expected.producer or delivery.dispatched_at != expected.dispatched_at
        or hashlib.sha256(json.dumps(delivery.payload, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False).encode()).hexdigest() != expected.payload_hash
    ):
        # Reject changed preparation identities before any earlier owner-resource lock.
        return None
    try:
        run_id = UUID(delivery.payload["run_id"])
        stage_id = UUID(delivery.payload["stage_id"])
    except (KeyError, ValueError, TypeError, AttributeError):
        return None
    run_scope = await ingestion_api.resolve_ingestion_run_scope(session, run_id,
        multi_workspace_enabled=multi_workspace_enabled)
    if run_scope != scope:
        return None
    locked = await sources.lock_source_set(session, (scope.source_id,), scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, expected_access_fence=access_fence)
    source = locked.fences[0]
    if source is None or source.status != "active" or source.generation != scope.source_generation:
        return None
    if expected is not None and source != expected.source_fence:
        return None
    projection = await sources.get_connector_source(session, source.id, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled)
    if projection is None or projection.status != source.status or projection.generation != source.generation:
        return None
    if delivery.type == "connector.crawl.requested":
        from modules.connectors import public as connectors
        revision = delivery.payload.get("connector_revision")
        if (connectors.is_native_provider(projection.provider) or type(revision) is not int
                or revision < 1 or not await connectors.require_collection_fence(session, projection,
                    CollectionFence(source_generation=source.generation, connector_revision=revision),
                    lock=True, scope=scope, multi_workspace_enabled=multi_workspace_enabled)):
            return None
    normalization_claim = None
    if delivery.type == "ingestion.normalize.requested":
        normalization_claim = await _capture_normalization_retry_claim_in_uow(
            session, delivery=delivery, scope=scope, access_fence=access_fence,
            source_fence=source, multi_workspace_enabled=multi_workspace_enabled,
        )
        if expected is not None and normalization_claim != expected:
            return None
        if normalization_claim is not None and normalization_claim.attempts >= MAX_STAGE_ATTEMPTS:
            raise NormalizationPreparationConflict("normalization_retry_exhausted", normalization_claim)
    try:
        if delivery.type == "document.file.uploaded":
            document_id = UUID(delivery.payload["document_id"])
            raw_uri, mime_type = delivery.payload.get("raw_uri"), delivery.payload.get("mime_type")
            if not isinstance(raw_uri, str) or not isinstance(mime_type, str):
                raise ValueError("Upload envelope lacks stored extraction identity")
            expected_prefix = f"workspaces/{scope.workspace_id}/documents/{document_id}/"
            legacy_prefix = f"documents/{document_id}/"
            if not raw_uri.startswith((expected_prefix, legacy_prefix)):
                raise ValueError("Upload storage key differs from retained document scope")
            # Every phase prepares the existing Document and its URI lifecycle before I roots.
            if not await documents.lock_document_for_extraction(session, document_id, source.id,
                scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
                source_fence=source, expected_raw_uri=raw_uri, expected_mime_type=mime_type):
                return None
        document_preparation = None
        observation_preparation = None
        normalization_rows = ()
        candidates = ()
        candidate_query = None
        discovered_batch_id = None
        if normalization_claim is not None:
            discovered_batch_id = normalization_claim.batch_id
            candidate_query = (select(
                ObservationNormalization.id, SourceObservation.id, SourceObservation.provider_id,
                SourceObservation.record_hash, ObservationNormalization.workspace_id,
                ObservationNormalization.source_id, ObservationNormalization.source_generation,
                ObservationNormalization.run_id, SourceObservation.batch_id,
                ObservationNormalization.stage_id, ObservationNormalization.normalization_version,
                SourceObservation.source_id,
            ).select_from(ObservationNormalization)
                .join(SourceObservation, SourceObservation.id == ObservationNormalization.observation_id).where(
                ObservationNormalization.workspace_id == scope.workspace_id,
                ObservationNormalization.source_id == source.id,
                ObservationNormalization.source_generation == source.generation,
                ObservationNormalization.run_id == run_id,
                ObservationNormalization.stage_id == stage_id,
                ObservationNormalization.normalization_version == NORMALIZATION_VERSION,
                ObservationNormalization.disposition == "pending",
                SourceObservation.source_id == source.id,
                SourceObservation.batch_id == discovered_batch_id,
            ).order_by(SourceObservation.provider_id, SourceObservation.id).limit(NORMALIZATION_BATCH_RECORDS))
            candidates = tuple(NormalizationCandidate(*row) for row in (await session.execute(candidate_query)).all())
            document_preparation = await documents.prepare_normalized_document_keys(
                session, source.id, tuple(sorted({item.external_id for item in candidates})),
                scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                access_fence=access_fence, source_fence=source,
            )
        state = await session.get(SourceIngestionState, source.id, with_for_update=True,
                                  populate_existing=True)
        run = await session.scalar(select(IngestionRun).where(
            IngestionRun.id == run_id, IngestionRun.workspace_id == scope.workspace_id,
            IngestionRun.actor_user_id == scope.actor_user_id,
            IngestionRun.membership_revision == scope.membership_revision,
            IngestionRun.source_id == source.id,
        ).with_for_update().execution_options(populate_existing=True))
        if run is None:
            return None
        batch = await session.scalar(select(IngestionBatch).where(
            IngestionBatch.id == run.batch_id, IngestionBatch.source_id == source.id,
            IngestionBatch.source_generation == source.generation,
        ).with_for_update().execution_options(populate_existing=True))
        stage_key = {"ingestion.stage.requested": "receive", "connector.crawl.requested": "collect_web",
                     "ingestion.normalize.requested": "normalize", "document.file.uploaded": "parse_file"}[delivery.type]
        stage = await session.scalar(select(IngestionStage).where(
            IngestionStage.id == stage_id, IngestionStage.run_id == run.id, IngestionStage.stage_key == stage_key,
        ).with_for_update().execution_options(populate_existing=True))
        if batch is None or stage is None:
            return None
        if candidate_query is not None:
            if run.batch_id != discovered_batch_id:
                raise NormalizationPreparationConflict("normalization_batch_changed", normalization_claim)
            # Prepare the complete discovered owner sets, including the O insert's accepted FK parents.
            await session.execute(select(ObservationNormalization).where(
                ObservationNormalization.id.in_(tuple(item.journal_id for item in candidates)),
                ObservationNormalization.workspace_id == scope.workspace_id,
                ObservationNormalization.source_id == source.id,
                ObservationNormalization.source_generation == source.generation,
                ObservationNormalization.run_id == run.id, ObservationNormalization.stage_id == stage.id,
                ObservationNormalization.normalization_version == NORMALIZATION_VERSION,
            ).order_by(ObservationNormalization.id).with_for_update().execution_options(populate_existing=True))
            await session.execute(select(SourceObservation).where(
                SourceObservation.id.in_(tuple(item.observation_id for item in candidates)),
                SourceObservation.source_id == source.id, SourceObservation.batch_id == batch.id,
            ).order_by(SourceObservation.id).with_for_update().execution_options(populate_existing=True))
            refreshed_candidates = tuple(NormalizationCandidate(*row)
                for row in (await session.execute(candidate_query)).all())
            if refreshed_candidates != candidates:
                raise NormalizationPreparationConflict("normalization_candidate_set_changed", normalization_claim)
            normalization_rows = tuple((progress, observation) for progress, observation in
                (await session.execute(candidate_query.with_only_columns(
                    ObservationNormalization, SourceObservation,
                ).execution_options(populate_existing=True))).all())
            # Content reload must still have the exact identities compared under those held rows.
            if tuple(NormalizationCandidate(
                progress.id, observation.id, observation.provider_id, observation.record_hash,
                progress.workspace_id, progress.source_id, progress.source_generation, progress.run_id,
                observation.batch_id, progress.stage_id, progress.normalization_version, observation.source_id,
            ) for progress, observation in normalization_rows) != candidates:
                raise NormalizationPreparationConflict("normalization_candidate_lineage_changed", normalization_claim)
            if projection.provider in {"alpha_vantage", "open_meteo"}:
                observation_preparation = await observations.prepare_ingestion_observation_keys(
                    session, source.id, tuple(sorted({(item.external_id, item.observation_id)
                        for item in candidates}, key=lambda item: (item[0], str(item[1])))),
                    scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                    access_fence=access_fence, source_fence=source,
                )
        event = await session.scalar(select(EventOutbox).where(
            EventOutbox.id == identifier, EventOutbox.workspace_id == scope.workspace_id,
            EventOutbox.actor_user_id == scope.actor_user_id,
            EventOutbox.membership_revision == scope.membership_revision,
            EventOutbox.status == "queued", EventOutbox.dispatched_at == delivery.dispatched_at,
        ).with_for_update().execution_options(populate_existing=True))
        if batch is None or stage is None or event is None or not valid_event_envelope(event, scope):
            return None
        if (event.type != delivery.type or event.version != delivery.version
                or event.producer != delivery.producer or event.payload != delivery.payload):
            # Earlier owner preparation was for this exact admitted detached envelope only.
            return None
        if event.payload.get("run_id") != str(run.id) or event.payload.get("stage_id") != str(stage.id):
            return None
        if event.type == "ingestion.normalize.requested" and await session.scalar(
            select(IngestionStage.id).where(IngestionStage.run_id == run.id,
                IngestionStage.stage_key.in_(("receive", "collect_web")),
                IngestionStage.status != "succeeded").limit(1)
        ) is not None:
            # Intake publishes both stages together. Normalization must not release the Source
            # run lease while its receive/browser predecessor still owns collection settlement.
            return None
        work = WorkerState(scope, locked.access_fence, source, projection, state, run, batch, stage, event,
                           normalization_rows, document_preparation, observation_preparation)
        if event.type == "ingestion.normalize.requested":
            if normalization_claim is None:
                # Only the existing terminal/idempotent path may return without D preparation.
                if stage.status not in {"succeeded", "failed"}:
                    return None
            elif not _same_worker_claim(work, normalization_claim):
                return None
        if expected is not None and not _same_worker_claim(work, expected):
            return None
        return work
    except (OperationalError, IntegrityError, RuntimeError) as exc:
        if normalization_claim is None:
            raise
        if isinstance(exc, NormalizationPreparationConflict):
            if exc.claim != normalization_claim:
                raise RuntimeError("Normalization preparation comparisons disagree") from exc
            raise
        raise NormalizationPreparationConflict("normalization_preparation_conflict", normalization_claim) from exc


async def _commit_ingestion_change(
    session: AsyncSession,
    run: IngestionRun,
    stage: IngestionStage,
    extras: tuple[ReplayDraft, ...] = (),
    *, scope: InternalJobScope, multi_workspace_enabled: bool, access_fence: AccessFence,
) -> None:
    """Commit scoped stage/outbox/replay once under the captured caller-held access fence.

    No earlier admission/Source lock is acquired late; replay must compare the retained
    principal and configuration before its head lock. The owner caller settles its claim.
    """
    await commit_with_replay(
        session,
        [make_ingestion_change(run.source_id, run.id, run.status, stage.stage_key, stage.status, scope=scope), *extras],
        scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
    )
    if stage.status in {"succeeded", "failed"}:
        count("ingestion_stages_total", stage=stage.stage_key, outcome=stage.status)
    if run.status in {"succeeded", "failed"}:
        count("ingestion_runs_total", outcome=run.status)


async def _refresh_run_status(
    session: AsyncSession, run: IngestionRun, *, scope: InternalJobScope,
    multi_workspace_enabled: bool, access_fence: AccessFence,
) -> None:
    """Derive one caller-locked scoped run's status, preserving its first stage failure.

    Nonlocking current admission must equal the captured fence and retained root
    principal before stage reads; no earlier locks, hidden commit or cross-run count.
    """
    current = await read_access_fence(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if (current != access_fence or run.workspace_id != scope.workspace_id
            or run.actor_user_id != scope.actor_user_id or run.membership_revision != scope.membership_revision
            or run.source_id != scope.source_id):
        raise HTTPException(status_code=409, detail="Original run admission changed")
    stages = list((await session.scalars(
        select(IngestionStage).where(IngestionStage.run_id == run.id)
    )).all())
    if any(stage.status == "failed" for stage in stages):
        run.status = "failed"
        run.error_code = next((stage.error_code for stage in stages if stage.status == "failed"), "stage_failed")
    elif stages and all(stage.status == "succeeded" for stage in stages):
        run.status = "succeeded"
        run.error_code = None
    else:
        run.status = "queued"
        run.error_code = None


class ConnectorRetryError(OSError):
    """Represent a retryable connector failure with an optional server delay."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        """Store the Retry-After hint alongside the transport error."""
        super().__init__(message)
        self.retry_after = retry_after


def _update_native_run_lease(state: SourceIngestionState | None, run_id: UUID, *, terminal: bool) -> None:
    """Renew or release only this native run's source lease at slice boundaries.

    A live fetch token or newer run is never changed; terminalization clears only
    a matching run owner, while bounded retry/slice work refreshes its expiry.
    """
    if state is None or state.lease_run_id != run_id:
        return
    if terminal:
        state.lease_run_id = None
        state.lease_expires_at = None
    else:
        state.lease_expires_at = datetime.now(UTC) + COLLECTION_LEASE


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse Retry-After seconds or date values, capped at 60 seconds."""
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, min(60.0, float(value)))
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=UTC)
            return max(0.0, min(60.0, (target - datetime.now(UTC)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return None


async def _collect_web_job(
    ctx: dict[str, object], factory: async_sessionmaker[AsyncSession], event: EventOutbox,
    run_id: UUID, stage_id: UUID, *, claim: WorkerClaim, multi_workspace_enabled: bool,
) -> None:
    """Send a generic browser request under the exact original durable stage claim.

    Re-resolve scoped access/Source/Connector/state/run/stage/outbox before egress and
    publication. Each short session closes before HTTP; no native routing, current-epoch
    lease reconstruction or second gateway retry layer is permitted. C2 owns later exact
    browser request/capability/slot binding; service secret alone is not that proof.
    """
    from modules.connectors import public as connectors

    settings = cast(Settings, ctx["settings"])
    token = settings.browser_shared_token.get_secret_value()
    if not token:
        raise ValueError("Browser collector is not configured")
    async with factory() as session:
        work = await _lock_worker_event(session, event.id, multi_workspace_enabled=multi_workspace_enabled,
            event_types=("connector.crawl.requested",), expected=claim)
        if (work is None or work.run.id != run_id or work.stage.id != stage_id
                or work.stage.status != "running" or work.stage.lease_expires_at is None
                or work.stage.lease_expires_at <= datetime.now(UTC)
                or work.state is None or work.state.lease_run_id != run_id
                or work.state.lease_expires_at is None or work.state.lease_expires_at <= datetime.now(UTC)):
            raise ValueError("Crawl original claim is no longer active")
        config = dict(work.event.payload["configuration"])
        payload = {
            "source_id": str(work.source.id), "source_generation": work.source.generation,
            "connector_revision": work.event.payload["connector_revision"],
            "url": config["url"], "mode": config["mode"], "max_pages": config["max_pages"],
            "max_depth": config["max_depth"], "timeout_seconds": config["timeout_seconds"],
        }
    try:
        async with httpx.AsyncClient(timeout=int(config["timeout_seconds"]) + 5) as client:
            response = await client.post(f"{str(settings.browser_service_url).rstrip('/')}/crawl",
                json=payload, headers={"Authorization": f"Bearer {token}"})
            if response.status_code in {408, 425, 429} or response.status_code >= 500:
                raise ConnectorRetryError(f"Browser collector returned HTTP {response.status_code}",
                                          _retry_after_seconds(response))
            if response.status_code >= 400:
                raise ValueError(f"Browser collector rejected the job with HTTP {response.status_code}")
    except httpx.TimeoutException as exc:
        raise TimeoutError("Browser collection timed out") from exc
    except httpx.NetworkError as exc:
        raise OSError("Browser collection transport failed") from exc
    if len(response.content) > 10 * 1024 * 1024:
        raise ValueError("Browser batch exceeds the intake bound")
    try:
        raw_records = response.json()
        if not isinstance(raw_records, list) or not raw_records or len(raw_records) > 500:
            raise ValueError("Browser job returned no pages or too many records")
        records = [ConnectorRecord.model_validate(item) for item in raw_records]
        if any({"provider_record", "world_data", "_owner_telegram_proof", "_native_telegram"}
               & record.metadata.keys() for record in records):
            raise ValueError("Browser metadata cannot provide native provenance")
    except (TypeError, ValueError) as exc:
        raise ValueError("Browser collector returned invalid records") from exc
    canonical_records = [record.model_dump(mode="json") for record in records]
    collected_at = datetime.now(UTC)
    payload_hash = hashlib.sha256(json.dumps(canonical_records, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    async with factory() as session:
        work = await _lock_worker_event(session, event.id, multi_workspace_enabled=multi_workspace_enabled,
            event_types=("connector.crawl.requested",), expected=claim)
        if (work is None or work.stage.status != "running" or work.state is None
                or work.state.lease_run_id != run_id or work.stage.lease_expires_at is None
                or work.stage.lease_expires_at <= datetime.now(UTC)
                or work.state.lease_expires_at is None or work.state.lease_expires_at <= datetime.now(UTC)):
            raise ValueError("Crawl original claim changed during HTTP")
        received_at = datetime.now(UTC)
        observations = []
        for record, data in zip(records, canonical_records, strict=True):
            observed_at = record.observed_at
            if observed_at.tzinfo is None:
                raise ValueError("Browser collector returned a naive observation time")
            record_hash = hashlib.sha256(json.dumps(
                {"version": data.get("version"), "content": data["content"], "metadata": data["metadata"]},
                sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
            observations.append({"source_id": work.source.id, "batch_id": work.batch.id,
                "provider_id": data["provider_id"], "record_hash": record_hash, "payload": data,
                "observed_at": observed_at.astimezone(UTC), "received_at": received_at,
                "collected_at": collected_at})
        await session.execute(pg_insert(SourceObservation).values(observations)
            .on_conflict_do_nothing(constraint="uq_source_observations_batch_record_observed"))
        work.batch.payload_hash = payload_hash
        cursor_after = max(record.observed_at.astimezone(UTC) for record in records).isoformat()
        if work.state.cursor:
            try:
                previous = datetime.fromisoformat(work.state.cursor.replace("Z", "+00:00"))
                if previous.tzinfo is not None and previous > datetime.fromisoformat(cursor_after):
                    cursor_after = work.state.cursor
            except ValueError:
                pass
        work.state.cursor = cursor_after
        await session.flush()
        normalize_stage = await ingestion_api.schedule_normalization(session, work.run, work.batch,
            work.source.generation, received_at, scope=work.scope, multi_workspace_enabled=multi_workspace_enabled)
        drafts = () if normalize_stage is None else (make_ingestion_change(work.source.id, work.run.id,
            work.run.status, normalize_stage.stage_key, normalize_stage.status, scope=work.scope),)
        await _commit_ingestion_change(session, work.run, work.stage, drafts, scope=work.scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=work.access_fence)


async def _fail_ingestion_stage(
    factory: async_sessionmaker[AsyncSession], event_id: UUID, run_id: UUID, stage_id: UUID,
    error_code: str, *, claim: WorkerClaim, multi_workspace_enabled: bool,
) -> None:
    """Fail only the original admitted dispatch/stage attempt, with flush-only Source health.

    Lost access/Source or a successor claim produces no cleanup using another principal.
    Earlier locks are acquired once by the scoped loader before state/run/stage/outbox.
    """
    async with factory() as session:
        work = await _lock_worker_event(session, event_id, multi_workspace_enabled=multi_workspace_enabled,
            event_types=(claim.event_type,), expected=claim)
        if work is None or work.run.id != run_id or work.stage.id != stage_id:
            return
        work.stage.status = "failed"
        work.stage.error_code = error_code
        work.stage.lease_expires_at = None
        work.run.status = "failed"
        work.run.error_code = error_code
        work.event.status = "failed"
        logger.warning("Ingestion stage failed run_id=%s stage_id=%s error_code=%s",
                       work.run.id, work.stage.id, error_code)
        changed = await sources.record_collection_result_in_uow(session, work.source.id,
            work.source.generation, datetime.now(UTC), error_code, scope=work.scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=work.access_fence, source_fence=work.source)
        _update_native_run_lease(work.state, work.run.id, terminal=True)
        extras = (make_source_change(work.source.id, work.source.generation, work.source.status,
                                    scope=work.scope),) if changed else ()
        await _commit_ingestion_change(session, work.run, work.stage, extras, scope=work.scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=work.access_fence)


async def _ingestion_enabled(session: AsyncSession, work: WorkerState, enabled: bool) -> bool:
    """Per-workspace module gate after admission; disabled leaves the durable event untouched."""
    from modules.settings.public import module_is_enabled

    if await module_is_enabled(session, "ingestion", scope=work.scope, multi_workspace_enabled=enabled):
        return True
    await session.rollback()
    return False


@timed("ingestion_stage_ms", stage="collect")
async def process_ingestion_event(ctx: dict[str, object], event_id: str) -> None:
    """Commit one original scoped stage lease, release SQL for work and CAS before settlement.

    Retain dispatch timestamp, full payload principal and stage attempt across all phases.
    Rollback handoffs use only detached claim IDs; expired ORM attributes are never read.
    At most five stage attempts preserve bounded browser recovery; no model-gateway retry
    layer is added. Exact durable managed request/slot/quota binding remains C2 ownership.
    """
    from modules.connectors import public as connectors

    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    enabled = _worker_flag(ctx)
    identifier = UUID(event_id)
    event_types = ("ingestion.stage.requested", "connector.crawl.requested")
    async with factory() as session:
        work = await _lock_worker_event(session, identifier, multi_workspace_enabled=enabled, event_types=event_types)
        if work is None or not await _ingestion_enabled(session, work, enabled):
            return
        run, stage, event, state = work.run, work.stage, work.event, work.state
        now = datetime.now(UTC)
        if stage.status == "succeeded":
            await ingestion_api.mark_event_delivered(session, identifier, scope=work.scope, multi_workspace_enabled=enabled)
            await session.commit()
            return
        if stage.status == "failed" or (stage.status == "running" and stage.lease_expires_at and stage.lease_expires_at > now):
            return
        if stage.attempts >= MAX_STAGE_ATTEMPTS:
            claim = _capture_worker_claim(work)
            await session.rollback()
            await _fail_ingestion_stage(factory, identifier, claim.run_id, claim.stage_id, "retry_exhausted",
                                       claim=claim, multi_workspace_enabled=enabled)
            return
        if state is None or state.lease_run_id != run.id or state.lease_expires_at is None or state.lease_expires_at <= now:
            claim = _capture_worker_claim(work)
            await session.rollback()
            await _fail_ingestion_stage(factory, identifier, claim.run_id, claim.stage_id, "lease_expired",
                                       claim=claim, multi_workspace_enabled=enabled)
            return
        stage.status = "running"
        stage.attempts += 1
        stage.lease_expires_at = now + timedelta(seconds=STAGE_TIMEOUT_SECONDS)
        stage.error_code = None
        state.lease_expires_at = now + COLLECTION_LEASE
        run.status = "running"
        claim = _capture_worker_claim(work)
        run_id, stage_id = run.id, stage.id
        set_trace(ingestion_run_id=str(run_id))
        logger.info("Ingestion stage started run_id=%s stage_id=%s attempt=%s", run_id, stage_id, stage.attempts)
        await _commit_ingestion_change(session, run, stage, scope=work.scope,
            multi_workspace_enabled=enabled, access_fence=work.access_fence)
    try:
        async with asyncio.timeout(STAGE_TIMEOUT_SECONDS):
            if claim.event_type == "connector.crawl.requested":
                await _collect_web_job(ctx, factory, event, run_id, stage_id,
                                       claim=claim, multi_workspace_enabled=enabled)
    except (TimeoutError, OSError, OperationalError) as exc:
        async with factory() as session:
            work = await _lock_worker_event(session, identifier, multi_workspace_enabled=enabled,
                                           event_types=event_types, expected=claim)
            if work is None:
                return
            if work.stage.attempts >= MAX_STAGE_ATTEMPTS:
                await session.rollback()
                await _fail_ingestion_stage(factory, identifier, run_id, stage_id, "retry_exhausted",
                                           claim=claim, multi_workspace_enabled=enabled)
                return
            delay = (max(0.5, exc.retry_after) if isinstance(exc, ConnectorRetryError) and exc.retry_after is not None
                     else random.uniform(0.5, min(60.0, 2.0 ** work.stage.attempts)))
            work.stage.status = "retrying"
            work.stage.error_code = "transient_failure"
            work.stage.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
            work.stage.lease_expires_at = None
            work.run.status = "queued"
            work.event.status = "pending"
            work.event.next_attempt_at = work.stage.next_attempt_at
            _update_native_run_lease(work.state, work.run.id, terminal=False)
            logger.warning("Ingestion stage retry scheduled run_id=%s stage_id=%s attempt=%s",
                           work.run.id, work.stage.id, work.stage.attempts)
            await _commit_ingestion_change(session, work.run, work.stage, scope=work.scope,
                multi_workspace_enabled=enabled, access_fence=work.access_fence)
        raise Retry(defer=delay) from exc
    except Exception:
        await _fail_ingestion_stage(factory, identifier, run_id, stage_id, "stage_failed",
                                   claim=claim, multi_workspace_enabled=enabled)
        return
    async with factory() as session:
        work = await _lock_worker_event(session, identifier, multi_workspace_enabled=enabled,
                                       event_types=event_types, expected=claim)
        if (work is None or work.stage.status != "running" or work.stage.lease_expires_at is None
                or work.stage.lease_expires_at <= datetime.now(UTC)):
            return
        observed = await session.scalar(select(func.count()).select_from(SourceObservation).where(
            SourceObservation.batch_id == work.batch.id, SourceObservation.source_id == work.source.id))
        if not observed:
            await session.rollback()
            await _fail_ingestion_stage(factory, identifier, run_id, stage_id, "missing_observations",
                                       claim=claim, multi_workspace_enabled=enabled)
            return
        work.stage.status = "succeeded"
        work.stage.error_code = None
        work.stage.lease_expires_at = None
        await _refresh_run_status(session, work.run, scope=work.scope,
            multi_workspace_enabled=enabled, access_fence=work.access_fence)
        changed = await sources.record_collection_result_in_uow(session, work.source.id, work.source.generation,
            datetime.now(UTC), None, scope=work.scope, multi_workspace_enabled=enabled,
            access_fence=work.access_fence, source_fence=work.source)
        native_source = connectors.is_native_provider(work.projection.provider)
        _update_native_run_lease(work.state, work.run.id,
            terminal=work.run.status in {"succeeded", "failed"} if native_source else True)
        await ingestion_api.mark_event_delivered(session, identifier, scope=work.scope, multi_workspace_enabled=enabled)
        logger.info("Ingestion stage completed run_id=%s stage_id=%s", work.run.id, work.stage.id)
        extras = (make_source_change(work.source.id, work.source.generation, work.source.status,
                                    scope=work.scope),) if changed else ()
        await _commit_ingestion_change(session, work.run, work.stage, extras, scope=work.scope,
            multi_workspace_enabled=enabled, access_fence=work.access_fence)


@timed("ingestion_stage_ms", stage="normalize")
async def process_normalize_event(ctx: dict[str, object], event_id: str) -> None:
    """Materialize one bounded slice and retain native collection ownership until terminal.

    PostgreSQL progress makes each slice resumable. For native providers, the
    matching source run lease is renewed while work remains and released only
    when normalization or its owning run becomes terminal; generic ingestion
    keeps its existing lease lifecycle. Provider validation uses the detached
    connector projection obtained under the source lock. Telegram selection
    time must agree across its persisted record, raw Bot API clock, and selected
    observation column before current-version ordering is authorized. Structured
    observations select by accepted time and ingestion identity; their winning
    immutable document version is projected through Documents before commit.
    If Observations is lifecycle-disabled, world normalization stays durably
    pending instead of consuming its accepted canonical records.
    The loader prepares sorted D roots before I roots/journals/accepted FK parents,
    then existing O roots before outbox. Exact32-candidate lineage/hash comparison
    precedes effects; owner-produced immutable successor snapshots flow through all
    repeated keys. Build/validate the normalized input before entering D. Only D's
    explicit pre-write NormalizedDocumentValidationRejected is journaled per record;
    unknown errors after owner entry and every post-D/O error roll back the attempt
    before original-claim recovery, never committing a partial invalid-record skip.
    Pre-D conflicts carry the same detached comparison. I-only recovery freshly locks
    admission/Source/roots/outbox, spends one durable failure attempt (maximum five),
    commits scheduling plus replay and returns; dispatcher is the only next schedule.
    Stale/denied/unknown-commit work never invents a counter, epoch or lease. This is
    no bound on all queue wakeups, crashes or infrastructure failures.
    Source ownership remains held and a newer token or run is never modified.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    enabled = _worker_flag(ctx)
    identifier = UUID(event_id)
    claim: WorkerClaim | None = None
    delay = 60.0
    try:
        async with factory() as session:
            work = await _lock_worker_event(session, identifier, multi_workspace_enabled=enabled,
                                           event_types=("ingestion.normalize.requested",))
            if work is None or not await _ingestion_enabled(session, work, enabled):
                return
            claim = _capture_worker_claim(work)
            source, source_projection = work.source, work.projection
            run, stage, event = work.run, work.stage, work.event
            run_id, stage_id = run.id, stage.id
            generation = source.generation
            document_preparation = work.document_preparation
            observation_preparation = work.observation_preparation
            from modules.connectors import public as connectors

            native_source = connectors.is_native_provider(source_projection.provider)
            if stage.status == "succeeded":
                await ingestion_api.mark_event_delivered(session, identifier, scope=work.scope,
                                                          multi_workspace_enabled=enabled)
                if native_source:
                    _update_native_run_lease(work.state, run.id, terminal=True)
                await session.commit()
                return
            if stage.status == "failed":
                return
            if document_preparation is None:
                raise NormalizationPreparationConflict("normalization_document_preparation_missing", claim)

            if source_projection.provider in {"alpha_vantage", "open_meteo"}:
                from modules.settings.public import module_is_enabled

                if not await module_is_enabled(session, "knowledge.observations", scope=work.scope, multi_workspace_enabled=enabled):
                    # The connector already accepted canonical provider records; retain their
                    # normalization progress until the owning observation projection is enabled.
                    deferred_until = datetime.now(UTC) + timedelta(seconds=60)
                    stage.status = "pending"
                    stage.next_attempt_at = deferred_until
                    stage.lease_expires_at = None
                    run.status = "queued"
                    run.error_code = None
                    event.status = "pending"
                    event.next_attempt_at = deferred_until
                    if native_source:
                        state = work.state
                        _update_native_run_lease(state, run.id, terminal=False)
                    await _commit_ingestion_change(session, run, stage, scope=work.scope, multi_workspace_enabled=enabled, access_fence=work.access_fence)
                    return

            stage.status = "running"
            stage.lease_expires_at = datetime.now(UTC) + timedelta(seconds=STAGE_TIMEOUT_SECONDS)
            stage.error_code = None
            run.status = "running"
            if native_source:
                # Keep the reservation alive across each bounded normalization slice.
                state = work.state
                _update_native_run_lease(state, run.id, terminal=False)
            rows = work.normalization_rows
            used_bytes = 0
            processed = 0
            processed_documents = 0
            failed_documents = 0
            knowledge_changes = []
            for progress, observation in rows:
                data_bytes = len(json.dumps(observation.payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                if processed and used_bytes + data_bytes > NORMALIZATION_BATCH_BYTES:
                    break
                if data_bytes > NORMALIZATION_BATCH_BYTES:
                    progress.disposition = "failed"
                    progress.error_code = "record_exceeds_normalization_limit"
                    processed += 1
                    continue
                used_bytes += data_bytes
                owner_application_started = False
                try:
                    if progress.source_generation != generation:
                        progress.disposition = "skipped"
                        progress.error_code = "source_generation_changed"
                        processed += 1
                        continue
                    persisted_payload = dict(observation.payload)
                    native_telegram_envelope = persisted_payload.pop("_native_telegram", None)
                    record = IngestionRecord.model_validate(persisted_payload)
                    if record.provider_id != observation.provider_id:
                        raise ValueError("Provider identity does not match accepted observation")
                    accepted_hash = hashlib.sha256(json.dumps(
                        {"version": record.version, "content": record.content, "metadata": record.metadata},
                        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                    ).encode("utf-8")).hexdigest()
                    if accepted_hash != observation.record_hash:
                        raise ValueError("Accepted observation hash does not match its payload")
                    raw_metadata = record.metadata
                    provider_record = None
                    telegram_order = None
                    provider_raw = raw_metadata.get("provider_record")
                    if provider_raw is not None:
                        from modules.knowledge.documents.schemas import (
                            ProviderRecordMetadata,
                            TelegramDocumentOrder,
                        )

                        provider_record = ProviderRecordMetadata.model_validate(provider_raw)
                        if (
                            provider_record.identity != record.provider_id
                            or provider_record.provider_version != record.version
                            or provider_record.provider != source_projection.provider
                        ):
                            raise ValueError("Typed provider provenance does not match the accepted source record")
                        provider_field_keys = {
                            "youtube": ("author", "summary", "title", "tags", "published_at", "provider_updated_at"),
                            "arxiv": ("author", "authors", "categories", "tags", "summary", "title", "published_at", "provider_updated_at"),
                            "huggingface": ("author", "tags", "pipeline_tag", "created_at", "last_modified"),
                            "github_releases": ("node_id", "name", "body", "html_url", "tag_name", "draft", "prerelease", "author", "created_at", "published_at"),
                            "github": ("record_type", "node_id", "html_url"),
                            "telegram": (),
                            "alpha_vantage": (), "open_meteo": (),
                        }[provider_record.provider]
                        source_fields = dict(provider_record.source_fields)
                        for key in provider_field_keys:
                            if key in raw_metadata and raw_metadata[key] is not None:
                                source_fields[key] = raw_metadata[key]
                        provider_record_data = provider_record.model_dump(mode="json")
                        provider_record_data["source_fields"] = source_fields
                        provider_record = ProviderRecordMetadata.model_validate(provider_record_data)
                        if provider_record.provider == "telegram":
                            detail = provider_record.telegram
                            from modules.ingestion.schemas import (
                                TelegramDeliveryProof,
                                TelegramRawDelivery,
                            )

                            if not isinstance(native_telegram_envelope, dict) or detail is None:
                                raise ValueError("Persisted Telegram raw delivery proof is missing")
                            proof = TelegramDeliveryProof.model_validate(native_telegram_envelope.get("proof"))
                            raw = TelegramRawDelivery.model_validate(native_telegram_envelope.get("raw_update"))
                            ingestion_api.validate_telegram_record_delivery(
                                record, provider_record, proof, raw,
                            )
                            if observation.observed_at != record.observed_at:
                                raise ValueError(
                                    "Persisted observation selection clock differs from verified Telegram clock"
                                )
                            provider_record_data = provider_record.model_dump(mode="json")
                            provider_record_data["telegram"]["raw_update_sha256"] = proof.raw_update_sha256
                            provider_record = ProviderRecordMetadata.model_validate(provider_record_data)
                            telegram_order = TelegramDocumentOrder(
                                bot_id=detail.bot_id, epoch=detail.epoch,
                                update_id=detail.update_id,
                            )
                        elif native_telegram_envelope is not None:
                            raise ValueError("Telegram delivery proof is not valid for this provider")
                    elif source_projection.provider in {"alpha_vantage", "open_meteo"}:
                        # Compatibility is limited to already persisted observations
                        # after their identity/hash checks. Public native ingress
                        # still requires a validated nested provider_record envelope.
                        from modules.knowledge.documents.schemas import (
                            ProviderRecordMetadata,
                            WorldDataMeasurement,
                        )

                        measurement = WorldDataMeasurement.model_validate(raw_metadata.get("world_data"))
                        if measurement.provider != source_projection.provider:
                            raise ValueError("Structured measurement does not match configured source provider")
                        provider_record = ProviderRecordMetadata(
                            provider=measurement.provider, identity=record.provider_id,
                            provider_version=record.version, timestamp_basis="collection",
                            coverage="returned_snapshot", content_truncated=False,
                            world_data=measurement,
                        )
                    elif source_projection.provider in {"youtube", "arxiv", "huggingface", "github_releases", "github", "telegram"}:
                        raise ValueError("Native provider record metadata is missing")
                    title_value = raw_metadata.get("title")
                    title = title_value.strip()[:500] if isinstance(title_value, str) and title_value.strip() else record.provider_id[:500]
                    raw_url = raw_metadata.get("canonical_url", raw_metadata.get("url"))
                    canonical_url = None
                    if isinstance(raw_url, str):
                        parsed = urlsplit(raw_url)
                        if parsed.scheme in {"http", "https"} and parsed.netloc and not parsed.username and not parsed.password:
                            canonical_url = raw_url[:2048]
                    published_at = None
                    published_value = raw_metadata.get("published_at")
                    if isinstance(published_value, str) and published_value:
                        try:
                            published_at = datetime.fromisoformat(published_value.replace("Z", "+00:00"))  # noqa: FURB162  # keeps exact parsing of 'Z' suffix; fromisoformat(Z) is not strictly equivalent
                            if published_at.tzinfo is None:
                                published_at = published_at.replace(tzinfo=UTC)
                            published_at = published_at.astimezone(UTC)
                        except ValueError:
                            published_at = None
                    content_type = raw_metadata.get("content_type")
                    content_type = content_type[:64] if isinstance(content_type, str) else None
                    safe_metadata: dict[str, object] = {}
                    for key, limit in (("author", 500), ("language", 32), ("summary", 10_000), ("description", 10_000)):
                        value = raw_metadata.get(key)
                        if isinstance(value, str):
                            safe_metadata[key] = value[:limit]
                    for key in ("tags", "categories"):
                        value = raw_metadata.get(key)
                        if isinstance(value, str):
                            safe_metadata[key] = value[:2_000]
                        elif isinstance(value, list):
                            safe_metadata[key] = [item[:500] for item in value[:100] if isinstance(item, str)]
                    from modules.connectors import public as connectors
                    provider_scope = await connectors.get_current_provider_scope(
                        session, observation.source_id, generation,
                        scope=work.scope, multi_workspace_enabled=enabled,
                    )
                    accepted_at = observation.received_at
                    if source_projection.provider in {"alpha_vantage", "open_meteo"} and (provider_scope is None or accepted_at is None):
                        raise ValueError("Accepted world observation is missing its scope or acceptance clock")
                    provenance = {
                        "title": title, "canonical_url": canonical_url,
                        "published_at": published_at.isoformat() if published_at else None,
                        "content_type": content_type, "metadata": safe_metadata,
                    }
                    if provider_record is not None:
                        provenance["provider_record"] = provider_record.model_dump(mode="json")
                    if provider_scope is not None:
                        provenance["provider_scope_discriminator"] = provider_scope.discriminator
                    normalized_input = NormalizedDocumentInput(
                        source_id=observation.source_id,
                        expected_source_generation=generation,
                        observation_id=observation.id,
                        provider_id=record.provider_id,
                        provider_version=record.version,
                        accepted_record_hash=observation.record_hash,
                        normalization_version=NORMALIZATION_VERSION,
                        observed_at=observation.observed_at,
                        received_at=observation.received_at,
                        collected_at=observation.collected_at,
                        title=title,
                        canonical_url=canonical_url,
                        published_at=published_at,
                        content_type=content_type,
                        content=record.content,
                        provenance=provenance,
                        telegram_order=telegram_order,
                    )
                    owner_application_started = True
                    try:
                        result, document_preparation = await documents.upsert_normalized_document_in_uow(
                            session, normalized_input, preparation=document_preparation,
                            scope=work.scope, multi_workspace_enabled=enabled,
                            access_fence=work.access_fence, source_fence=work.source,
                        )
                    except documents.NormalizedDocumentValidationRejected:
                        # D guarantees no effects for this record; preserve the latest sibling snapshot.
                        progress.disposition = "failed"
                        progress.error_code = "invalid_normalization_record"
                        failed_documents += 1
                        processed += 1
                        continue
                    observation_write = None
                    if (
                        source_projection.provider in {"alpha_vantage", "open_meteo"}
                        and result.disposition != "tombstoned"
                    ):
                        from modules.knowledge.observations.schemas import WorldMeasurement

                        if (result.document_id is None or result.document_version_id is None
                                or provider_record is None or provider_record.world_data is None):
                            raise NormalizationPreparationConflict("normalization_world_artifact_missing", claim)
                        if provider_scope is None or accepted_at is None:
                            raise ValueError("Accepted world observation is missing its scope or acceptance clock")
                        if observation_preparation is None:
                            raise NormalizationPreparationConflict("normalization_observation_preparation_missing", claim)
                        observation_write, observation_preparation = await observations.upsert_from_ingestion_in_uow(
                            session, source_id=observation.source_id,
                            source_generation=generation,
                            ingestion_observation_id=observation.id,
                            document_id=result.document_id,
                            document_version_id=result.document_version_id,
                            external_id=record.provider_id,
                            provider_version=record.version,
                            observed_at=record.observed_at,
                            collected_at=record.collected_at or observation.collected_at or datetime.now(UTC),
                            accepted_at=accepted_at,
                            provider_scope_discriminator=provider_scope.discriminator,
                            measurement=WorldMeasurement.model_validate(
                                provider_record.world_data.model_dump(mode="python")
                            ), preparation=observation_preparation, scope=work.scope, multi_workspace_enabled=enabled,
                            access_fence=work.access_fence, source_fence=work.source,
                        )
                        if observation_write is None:
                            raise NormalizationPreparationConflict("normalization_observation_rejected", claim)
                        if observation_write.selected_current:
                            selected_document = await documents.select_current_world_document_version_in_uow(
                                session, document_id=result.document_id,
                                document_version_id=result.document_version_id,
                                expected_source_generation=generation,
                                provider_scope_discriminator=provider_scope.discriminator,
                                preparation=document_preparation,
                                scope=work.scope, multi_workspace_enabled=enabled,
                                access_fence=work.access_fence, source_fence=work.source,
                            )
                            if not selected_document:
                                raise RuntimeError("Accepted current observation has no selectable document version")
                        if observation_write is not None:
                            result = result.model_copy(update={
                                "selected_current": observation_write.selected_current,
                            })
                        progress.selected_current = (
                            observation_write.selected_current if observation_write is not None else False
                        )
                    progress.disposition = {
                        "normalized": "normalized", "duplicate": "duplicate", "tombstoned": "skipped",
                    }[result.disposition]
                    if progress.disposition == "failed":
                        failed_documents += 1
                    else:
                        processed_documents += 1
                    progress.error_code = "document_deleted" if result.disposition == "tombstoned" else None
                    progress.document_id = result.document_id
                    progress.document_version_id = result.document_version_id
                    progress.chunk_count = result.chunk_count if result.created_version else 0
                    if result.created_version and result.chunk_count:
                        ready = DomainEvent(
                            id=uuid4(), type="document.version.ready", version=1,
                            occurred_at=datetime.now(UTC), producer="modules.ingestion",
                            payload={
                                "workspace_id": str(work.scope.workspace_id),
                                "actor_user_id": work.scope.actor_user_id,
                                "membership_revision": work.scope.membership_revision,
                                "source_id": str(observation.source_id),
                                "document_id": str(result.document_id),
                                "document_version_id": str(result.document_version_id),
                                "source_generation": generation,
                                "version_number": result.version_number,
                            },
                        )
                        # Publication proves the exact journal/run/batch/accepted observation.
                        await session.flush()
                        await ingestion_api.publish_event(session, ready, scope=work.scope,
                                                          multi_workspace_enabled=enabled)
                    if source_projection.provider in {"alpha_vantage", "open_meteo"}:
                        # Structured series can change even when the normalized document version is reused.
                        knowledge_changes.append(make_knowledge_change(observation.source_id, scope=work.scope))
                    elif result.selected_current and result.created_version:
                        knowledge_changes.append(make_knowledge_change(
                            observation.source_id, result.document_id, result.version_number, scope=work.scope
                        ))
                except (ValueError, TypeError) as exc:
                    if owner_application_started:
                        raise NormalizationPreparationConflict("normalization_owner_application_failed", claim) from exc
                    progress.disposition = "failed"
                    progress.error_code = "invalid_normalization_record"
                    failed_documents += 1
                processed += 1

            pending_count = int(await session.scalar(
                select(func.count()).select_from(ObservationNormalization).where(
                    ObservationNormalization.workspace_id == work.scope.workspace_id,
                    ObservationNormalization.source_id == source.id,
                    ObservationNormalization.source_generation == generation,
                    ObservationNormalization.run_id == run.id,
                    ObservationNormalization.normalization_version == NORMALIZATION_VERSION,
                    ObservationNormalization.stage_id == stage.id,
                    ObservationNormalization.disposition == "pending",
                )
            ) or 0)
            failed_count = int(await session.scalar(
                select(func.count()).select_from(ObservationNormalization).where(
                    ObservationNormalization.workspace_id == work.scope.workspace_id,
                    ObservationNormalization.source_id == source.id,
                    ObservationNormalization.source_generation == generation,
                    ObservationNormalization.run_id == run.id,
                    ObservationNormalization.normalization_version == NORMALIZATION_VERSION,
                    ObservationNormalization.stage_id == stage.id,
                    ObservationNormalization.disposition == "failed",
                )
            ) or 0)
            chunk_total = int(await session.scalar(
                select(func.coalesce(func.sum(ObservationNormalization.chunk_count), 0))
                .where(ObservationNormalization.workspace_id == work.scope.workspace_id,
                       ObservationNormalization.source_id == source.id,
                       ObservationNormalization.source_generation == generation,
                       ObservationNormalization.run_id == run.id,
                       ObservationNormalization.normalization_version == NORMALIZATION_VERSION,
                       ObservationNormalization.stage_id == stage.id)
            ) or 0)
            stage.result_count = chunk_total
            stage.lease_expires_at = None
            if pending_count:
                stage.status = "pending"
                run.status = "queued"
                event.status = "pending"
                event.next_attempt_at = datetime.now(UTC) + timedelta(seconds=1)
            else:
                stage.status = "failed" if failed_count else "succeeded"
                stage.error_code = "normalization_failed" if failed_count else None
                stages = list((await session.scalars(
                    select(IngestionStage).where(IngestionStage.run_id == run.id)
                )).all())
                if any(item.status == "failed" for item in stages):
                    run.status = "failed"
                    run.error_code = next((item.error_code for item in stages if item.status == "failed"), "stage_failed")
                elif all(item.status == "succeeded" for item in stages):
                    run.status = "succeeded"
                    run.error_code = None
                else:
                    run.status = "queued"
                    run.error_code = None
                event.status = "failed" if failed_count else "delivered"
                if failed_count:
                    await sources.record_processing_result_in_uow(
                        session, source.id, source.generation, datetime.now(UTC), "normalization_failed",
                        scope=work.scope, multi_workspace_enabled=enabled,
                        access_fence=work.access_fence, source_fence=work.source,
                    )
                else:
                    await sources.record_processing_result_in_uow(
                        session, source.id, source.generation, datetime.now(UTC), None,
                        scope=work.scope, multi_workspace_enabled=enabled,
                        access_fence=work.access_fence, source_fence=work.source,
                    )
            extras = [*knowledge_changes]
            if native_source:
                state = work.state
                _update_native_run_lease(
                    state, run.id,
                    terminal=(not pending_count or run.status in {"succeeded", "failed"}),
                )
            if processed or not pending_count:
                await _commit_ingestion_change(session, run, stage, tuple(extras), scope=work.scope, multi_workspace_enabled=enabled, access_fence=work.access_fence)
            else:
                await _commit_ingestion_change(session, run, stage, scope=work.scope, multi_workspace_enabled=enabled, access_fence=work.access_fence)
            if processed_documents:
                count("ingestion_documents_total", processed_documents, outcome="processed")
            if failed_documents:
                count("ingestion_documents_total", failed_documents, outcome="failed")
    except (OperationalError, IntegrityError, RuntimeError) as exc:
        # The failed factory context has exited/rolled back before this fresh transaction.
        if isinstance(exc, NormalizationPreparationConflict):
            if claim is None:
                claim = exc.claim
            elif claim != exc.claim:
                raise RuntimeError("Normalization preparation comparisons disagree") from exc
        if claim is None:
            raise Retry(defer=delay) from exc
        try:
            async with factory() as session:
                work = await _lock_normalization_retry_event(session, identifier,
                    multi_workspace_enabled=enabled, expected=claim)
                if work is None:
                    return
                run, stage, event = work.run, work.stage, work.event
                if stage.attempts < MAX_STAGE_ATTEMPTS:
                    stage.attempts += 1
                if stage.attempts >= MAX_STAGE_ATTEMPTS:
                    stage.status = "failed"
                    stage.error_code = "retry_exhausted"
                    run.status = "failed"
                    run.error_code = "retry_exhausted"
                    event.status = "failed"
                else:
                    delay = random.uniform(0.5, min(60.0, 2.0 ** stage.attempts))
                    next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
                    stage.status = "retrying"
                    stage.error_code = "transient_failure"
                    stage.next_attempt_at = next_attempt_at
                    event.status = "pending"
                    event.next_attempt_at = next_attempt_at
                    run.status = "queued"
                stage.lease_expires_at = None
                from modules.connectors import public as connectors

                if connectors.is_native_provider(work.projection.provider):
                    _update_native_run_lease(work.state, run.id,
                        terminal=stage.status == "failed" or run.status in {"succeeded", "failed"})
                await _commit_ingestion_change(session, run, stage, scope=work.scope,
                    multi_workspace_enabled=enabled, access_fence=work.access_fence)
            return
        except HTTPException:
            # Original admission loss grants no counter or scheduling authority.
            return
        except (OperationalError, IntegrityError, RuntimeError):
            # Preserve the old comparison on uncertain settlement; durable outbox recovery
            # decides pending/queued/failed later. Do not restart with a newly captured claim.
            return


@bounded_heavy_work
@timed("ingestion_stage_ms", stage="extract")
async def process_uploaded_file(ctx: dict[str, object], event_id: str) -> None:
    """Parse a proven stored upload outside SQL, then settle its original dispatch/stage CAS.

    The loader prepares exact Document/URI/MIME before Ingestion roots/outbox on initial,
    publication and failure phases; late status/save consume those caller-held locks.
    Documents verifies stored URI/MIME/Source/document under the held admission
    before raw bytes are read. Legacy blobs remain usable only through that owner proof;
    new blobs require this workspace prefix. Publication reacquires the original fences,
    compares the original attempt and uses only transaction-local owner mutations.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    settings = cast(Settings, ctx["settings"])
    enabled = _worker_flag(ctx)
    identifier = UUID(event_id)
    async with factory() as session:
        work = await _lock_worker_event(session, identifier, multi_workspace_enabled=enabled,
                                       event_types=("document.file.uploaded",))
        if work is None or not await _ingestion_enabled(session, work, enabled):
            return
        run, stage, event = work.run, work.stage, work.event
        source_id = work.source.id
        document_id = UUID(event.payload["document_id"])
        raw_uri, mime_type = event.payload.get("raw_uri"), event.payload.get("mime_type")
        # The loader proved these detached values under the early Document/URI locks.
        raw_uri, mime_type = cast(str, raw_uri), cast(str, mime_type)
        now = datetime.now(UTC)
        if stage.status == "succeeded":
            await ingestion_api.mark_event_delivered(session, identifier, scope=work.scope, multi_workspace_enabled=enabled)
            await session.commit()
            return
        if stage.status == "failed" or (stage.status == "running" and stage.lease_expires_at and stage.lease_expires_at > now):
            return
        stage.status = "running"
        stage.attempts += 1
        stage.lease_expires_at = now + timedelta(seconds=settings.parser_timeout_seconds + 30)
        stage.error_code = None
        run.status = "running"
        if not await documents.set_extraction_status(session, document_id, source_id, "processing",
            scope=work.scope, multi_workspace_enabled=enabled, access_fence=work.access_fence, source_fence=work.source,
            expected_raw_uri=raw_uri, expected_mime_type=mime_type):
            return
        claim = _capture_worker_claim(work)
        await _commit_ingestion_change(session, run, stage,
            (make_knowledge_change(source_id, document_id, scope=work.scope),), scope=work.scope,
            multi_workspace_enabled=enabled, access_fence=work.access_fence)
    try:
        # The row/URI proof and original dispatch were captured before closing the session.
        parsed = await parse_file_bounded(storage_path(settings.data_dir, raw_uri), mime_type,
            settings.parser_timeout_seconds, settings.docx_expanded_max_bytes, settings.pdf_page_max)
        drafts = chunk_text(parsed.text)
        extraction_status = "needs_ocr" if parsed.warnings and not parsed.text else "succeeded"
        async with factory() as session:
            work = await _lock_worker_event(session, identifier, multi_workspace_enabled=enabled,
                                           event_types=("document.file.uploaded",), expected=claim)
            if work is None or work.stage.status != "running" or work.stage.lease_expires_at is None or work.stage.lease_expires_at <= datetime.now(UTC):
                return
            saved_document_id = await documents.save_extraction(session, document_id, source_id, parsed.text,
                [{"content": draft.content, "token_count": draft.token_count, "metadata": draft.metadata} for draft in drafts],
                extraction_status, parsed.metadata, parsed.warnings, "p02-t2-v1",
                scope=work.scope, multi_workspace_enabled=enabled, access_fence=work.access_fence,
                source_fence=work.source, expected_raw_uri=raw_uri, expected_mime_type=mime_type)
            if saved_document_id != document_id:
                return
            work.stage.status = "succeeded"
            work.stage.result_count = len(drafts)
            work.stage.lease_expires_at = None
            work.stage.error_code = None
            work.run.status = extraction_status
            work.run.error_code = None
            await ingestion_api.mark_event_delivered(session, identifier, scope=work.scope, multi_workspace_enabled=enabled)
            changed = await sources.record_processing_result_in_uow(session, source_id, work.source.generation,
                datetime.now(UTC), None, scope=work.scope, multi_workspace_enabled=enabled,
                access_fence=work.access_fence, source_fence=work.source)
            extras = [make_knowledge_change(source_id, document_id, scope=work.scope)]
            if changed:
                extras.append(make_source_change(source_id, work.source.generation, work.source.status, scope=work.scope))
            await _commit_ingestion_change(session, work.run, work.stage, tuple(extras), scope=work.scope,
                multi_workspace_enabled=enabled, access_fence=work.access_fence)
            count("ingestion_documents_total", outcome="processed")
    except Exception as exc:
        async with factory() as session:
            work = await _lock_worker_event(session, identifier, multi_workspace_enabled=enabled,
                                           event_types=("document.file.uploaded",), expected=claim)
            if work is None:
                return
            code = "parser_timeout" if isinstance(exc, TimeoutError) else "parse_failed"
            work.stage.status = "failed"
            work.stage.error_code = code
            work.stage.lease_expires_at = None
            work.run.status = "failed"
            work.run.error_code = code
            work.event.status = "failed"
            if not await documents.set_extraction_status(session, document_id, source_id, "failed",
                scope=work.scope, multi_workspace_enabled=enabled, access_fence=work.access_fence, source_fence=work.source,
                expected_raw_uri=raw_uri, expected_mime_type=mime_type):
                return
            changed = await sources.record_processing_result_in_uow(session, source_id, work.source.generation,
                datetime.now(UTC), code, scope=work.scope, multi_workspace_enabled=enabled,
                access_fence=work.access_fence, source_fence=work.source)
            extras = [make_knowledge_change(source_id, document_id, scope=work.scope)]
            if changed:
                extras.append(make_source_change(source_id, work.source.generation, work.source.status, scope=work.scope))
            await _commit_ingestion_change(session, work.run, work.stage, tuple(extras), scope=work.scope,
                multi_workspace_enabled=enabled, access_fence=work.access_fence)
            count("ingestion_documents_total", outcome="failed")


async def cleanup_storage_orphans(ctx: dict[str, object]) -> int:
    """Sweep global legacy/scoped storage under deliberate bootstrap maintenance admission.

    An active bootstrap account and backup activity receipt admit this internal operation;
    no selected workspace/member endpoint or per-job fallback is introduced. Documents
    supplies complete global reference proof before the dual-prefix grace-period sweep.
    SQL is closed before filesystem work; the activity survives it and closes on exit.
    """
    from core.auth.public import get_active_account
    from modules.settings.public import finish_activity, register_activity

    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    settings = cast(Settings, ctx["settings"])
    enabled = _worker_flag(ctx)
    if enabled:
        # ponytail: multiworkspace orphan sweep disabled until O instance-operator admission exists (ruling §6)
        return 0
    async with factory() as session:
        if await get_active_account(session, 1, multi_workspace_enabled=enabled) is None:
            return 0
        activity = await register_activity(session, "cleanup_storage_orphans")
        await session.commit()
    try:
        async with factory() as session:
            referenced = await documents.raw_uris(
                session, instance_operator=True, multi_workspace_enabled=enabled,
            )
        return cleanup_orphaned_files(settings.data_dir, referenced, settings.storage_orphan_grace_seconds)
    finally:
        async with factory() as session:
            await finish_activity(session, activity)
            await session.commit()
