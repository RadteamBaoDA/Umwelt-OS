"""Observation normalization slices and the process_normalize_event job."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.errors import DomainError
from core.events import DomainEvent
from core.ids import uuid7
from core.realtime import (
    ReplayDraft,
    make_knowledge_change,
)
from core.telemetry import count, timed
from core.worker_context import worker_multi_workspace_flag as _worker_flag
from core.worker_context import worker_session_factory
from core.worker_retry import retry_with_backoff
from modules.connectors import public as connectors
from modules.ingestion import public as ingestion_api
from modules.ingestion._normalize_metadata import (
    ExtractionInput,
    _normalize_common_metadata,
    metadata_extractor_for,
)
from modules.ingestion._worker.collect import (
    _commit_ingestion_change,
    _update_native_run_lease,
    _wake_dispatcher_after_commit,
)
from modules.ingestion._worker.lock import _lock_normalization_retry_event, _lock_worker_event
from modules.ingestion._worker.stage import _ingestion_enabled
from modules.ingestion._worker.state import (
    MAX_STAGE_ATTEMPTS,
    NORMALIZATION_BATCH_BYTES,
    NORMALIZATION_VERSION,
    STAGE_TIMEOUT_SECONDS,
    NormalizationPreparationConflict,
    WorkerClaim,
    WorkerState,
    _capture_worker_claim,
    logger,
)
from modules.ingestion.models import (
    IngestionStage,
    ObservationNormalization,
    SourceObservation,
)
from modules.ingestion.schemas import IngestionRecord
from modules.knowledge.documents import public as documents
from modules.knowledge.documents.public import MEASUREMENT_PROVIDER_IDS, NormalizedDocumentInput
from modules.knowledge.observations import public as observations
from modules.sources import public as sources


@dataclass
class _ClaimBox:
    """Hold the original worker claim so the failure handler sees it even if the slice raised mid-way."""

    claim: WorkerClaim | None = None


@dataclass
class _SliceState:
    """Mutable accumulators of one normalization slice (explicit state instead of closure ``nonlocal``).

    ``document_preparation``/``observation_preparation`` are the owner snapshots threaded through every
    repeated key; counters feed the stage settlement and the telemetry emitted after the commit.
    """

    document_preparation: Any
    observation_preparation: Any
    used_bytes: int = 0
    processed: int = 0
    processed_documents: int = 0
    failed_documents: int = 0
    knowledge_changes: list[ReplayDraft] = field(default_factory=list)


@dataclass(frozen=True)
class _PreparedRow:
    """One validated accepted observation ready to enter the Documents owner."""

    record: IngestionRecord
    normalized_input: NormalizedDocumentInput
    provider_record: Any
    provider_scope: Any
    accepted_at: datetime | None


async def _prepare_row(
    session: AsyncSession,
    work: WorkerState,
    enabled: bool,
    observation: SourceObservation,
) -> _PreparedRow:
    """Validate one accepted observation and build its normalized document input; performs no owner writes.

    Order: payload parse -> identity -> accepted-hash comparison -> provider extractor -> common metadata ->
    provider scope read (the only I/O) -> measurement scope/clock check -> input construction. Any
    ``ValueError``/``TypeError`` means the record is invalid and the owner is never entered.
    """
    projection, generation = work.projection, work.source.generation
    persisted_payload = dict(observation.payload)
    native_telegram_envelope = persisted_payload.pop("_native_telegram", None)
    record = IngestionRecord.model_validate(persisted_payload)
    if record.provider_id != observation.provider_id:
        raise ValueError("Provider identity does not match accepted observation")
    accepted_hash = hashlib.sha256(
        json.dumps(
            {"version": record.version, "content": record.content, "metadata": record.metadata},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    if accepted_hash != observation.record_hash:
        raise ValueError("Accepted observation hash does not match its payload")
    extraction = metadata_extractor_for(projection.provider).extract(
        ExtractionInput(
            record=record,
            observed_at=observation.observed_at,
            source_provider=projection.provider,
            telegram_envelope=native_telegram_envelope,
        )
    )
    common = _normalize_common_metadata(record.metadata, record.provider_id)
    scope_key = (observation.source_id, generation)
    if scope_key not in work.provider_scope_cache:
        # Same for every row of the slice (one source, one generation): read once, cache None results too.
        work.provider_scope_cache[scope_key] = await connectors.get_current_provider_scope(
            session,
            observation.source_id,
            generation,
            scope=work.scope,
            multi_workspace_enabled=enabled,
        )
    provider_scope = work.provider_scope_cache[scope_key]
    accepted_at = observation.received_at
    if projection.provider in MEASUREMENT_PROVIDER_IDS and (
        provider_scope is None or accepted_at is None
    ):
        raise ValueError("Accepted world observation is missing its scope or acceptance clock")
    provenance: dict[str, Any] = {
        "title": common.title,
        "canonical_url": common.canonical_url,
        "published_at": common.published_at.isoformat() if common.published_at else None,
        "content_type": common.content_type,
        "metadata": common.safe_metadata,
    }
    if extraction.provider_record is not None:
        provenance["provider_record"] = extraction.provider_record.model_dump(mode="json")
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
        title=common.title,
        canonical_url=common.canonical_url,
        published_at=common.published_at,
        content_type=common.content_type,
        content=record.content,
        provenance=provenance,
        telegram_order=extraction.telegram_order,
    )
    return _PreparedRow(
        record, normalized_input, extraction.provider_record, provider_scope, accepted_at
    )


async def _write_world_observation(
    session: AsyncSession,
    work: WorkerState,
    state: _SliceState,
    claim: WorkerClaim,
    enabled: bool,
    *,
    progress: ObservationNormalization,
    observation: SourceObservation,
    prepared: _PreparedRow,
    result: Any,
) -> Any:
    """Project one structured measurement into Observations and select the winning document version.

    Runs only after Documents accepted the row, in the same transaction. Missing artifacts or preparation are
    retryable preparation conflicts; a rejected observation write is too. Returns ``result`` copied with the
    observation's ``selected_current`` and records it on ``progress``.
    """
    from modules.knowledge.observations.public import WorldMeasurement

    provider_record, provider_scope = prepared.provider_record, prepared.provider_scope
    generation = work.source.generation
    if (
        result.document_id is None
        or result.document_version_id is None
        or provider_record is None
        or provider_record.world_data is None
    ):
        raise NormalizationPreparationConflict("normalization_world_artifact_missing", claim)
    if provider_scope is None or prepared.accepted_at is None:
        raise ValueError("Accepted world observation is missing its scope or acceptance clock")
    if state.observation_preparation is None:
        raise NormalizationPreparationConflict(
            "normalization_observation_preparation_missing", claim
        )
    record = prepared.record
    (
        observation_write,
        state.observation_preparation,
    ) = await observations.upsert_from_ingestion_in_uow(
        session,
        source_id=observation.source_id,
        source_generation=generation,
        ingestion_observation_id=observation.id,
        document_id=result.document_id,
        document_version_id=result.document_version_id,
        external_id=record.provider_id,
        provider_version=record.version,
        observed_at=record.observed_at,
        collected_at=record.collected_at or observation.collected_at or datetime.now(UTC),
        accepted_at=prepared.accepted_at,
        provider_scope_discriminator=provider_scope.discriminator,
        measurement=WorldMeasurement.model_validate(
            provider_record.world_data.model_dump(mode="python")
        ),
        preparation=state.observation_preparation,
        scope=work.scope,
        multi_workspace_enabled=enabled,
        access_fence=work.access_fence,
        source_fence=work.source,
    )
    if observation_write is None:
        raise NormalizationPreparationConflict("normalization_observation_rejected", claim)
    if observation_write.selected_current:
        selected_document = await documents.select_current_world_document_version_in_uow(
            session,
            document_id=result.document_id,
            document_version_id=result.document_version_id,
            expected_source_generation=generation,
            provider_scope_discriminator=provider_scope.discriminator,
            preparation=state.document_preparation,
            scope=work.scope,
            multi_workspace_enabled=enabled,
            access_fence=work.access_fence,
            source_fence=work.source,
        )
        if not selected_document:
            raise RuntimeError("Accepted current observation has no selectable document version")
    progress.selected_current = observation_write.selected_current
    return result.model_copy(update={"selected_current": observation_write.selected_current})


async def _publish_ready_events(
    session: AsyncSession,
    work: WorkerState,
    enabled: bool,
    observation: SourceObservation,
    result: Any,
) -> None:
    """Publish ``document.version.ready`` and ``news.document.ready`` for a newly created chunked version.

    The journal is flushed first so publication proves the exact run/batch/accepted observation. Connector
    documents never pass the Documents extraction publisher, so News clustering needs its own event; both
    share one journal proof.
    """
    ready_payload = {
        "workspace_id": str(work.scope.workspace_id),
        "actor_user_id": work.scope.actor_user_id,
        "membership_revision": work.scope.membership_revision,
        "source_id": str(observation.source_id),
        "document_id": str(result.document_id),
        "document_version_id": str(result.document_version_id),
        "source_generation": work.source.generation,
        "version_number": result.version_number,
    }
    await session.flush()
    for ready_type in ("document.version.ready", "news.document.ready"):
        await ingestion_api.publish_event(
            session,
            DomainEvent(
                id=uuid7(),
                type=ready_type,
                version=1,
                occurred_at=datetime.now(UTC),
                producer="modules.ingestion",
                payload=dict(ready_payload),
            ),
            scope=work.scope,
            multi_workspace_enabled=enabled,
        )


async def _apply_owner_effects(
    session: AsyncSession,
    work: WorkerState,
    state: _SliceState,
    claim: WorkerClaim,
    enabled: bool,
    *,
    progress: ObservationNormalization,
    observation: SourceObservation,
    prepared: _PreparedRow,
) -> None:
    """Enter Documents (and Observations for measurements) for one prepared row and record its disposition.

    D's explicit pre-write ``NormalizedDocumentValidationRejected`` is journaled for this record only and the
    slice continues; every other error after owner entry propagates so the whole attempt rolls back.
    """
    try:
        result, state.document_preparation = await documents.upsert_normalized_document_in_uow(
            session,
            prepared.normalized_input,
            preparation=state.document_preparation,
            scope=work.scope,
            multi_workspace_enabled=enabled,
            access_fence=work.access_fence,
            source_fence=work.source,
        )
    except documents.NormalizedDocumentValidationRejected:
        # D guarantees no effects for this record; preserve the latest sibling snapshot.
        progress.disposition = "failed"
        progress.error_code = "invalid_normalization_record"
        state.failed_documents += 1
        return
    is_measurement = work.projection.provider in MEASUREMENT_PROVIDER_IDS
    if is_measurement and result.disposition != "tombstoned":
        result = await _write_world_observation(
            session,
            work,
            state,
            claim,
            enabled,
            progress=progress,
            observation=observation,
            prepared=prepared,
            result=result,
        )
    progress.disposition = {
        "normalized": "normalized",
        "duplicate": "duplicate",
        "tombstoned": "skipped",
    }[result.disposition]
    state.processed_documents += 1
    progress.error_code = "document_deleted" if result.disposition == "tombstoned" else None
    progress.document_id = result.document_id
    progress.document_version_id = result.document_version_id
    progress.chunk_count = result.chunk_count if result.created_version else 0
    if result.created_version and result.chunk_count:
        await _publish_ready_events(session, work, enabled, observation, result)
    if is_measurement:
        # Structured series can change even when the normalized document version is reused.
        state.knowledge_changes.append(
            make_knowledge_change(observation.source_id, scope=work.scope)
        )
    elif result.selected_current and result.created_version:
        state.knowledge_changes.append(
            make_knowledge_change(
                observation.source_id, result.document_id, result.version_number, scope=work.scope
            )
        )


async def _normalize_row(
    session: AsyncSession,
    work: WorkerState,
    state: _SliceState,
    claim: WorkerClaim,
    enabled: bool,
    progress: ObservationNormalization,
    observation: SourceObservation,
) -> None:
    """Normalize one accepted observation and set its journal disposition; the caller counts it as processed.

    A row from another source generation is skipped. Validation failures before owner entry fail only this
    row. After owner entry, ``ValueError``/``TypeError`` become a retryable preparation conflict so no partial
    invalid-record skip is ever committed.
    """
    owner_application_started = False
    try:
        if progress.source_generation != work.source.generation:
            progress.disposition = "skipped"
            progress.error_code = "source_generation_changed"
            return
        prepared = await _prepare_row(session, work, enabled, observation)
        owner_application_started = True
        await _apply_owner_effects(
            session,
            work,
            state,
            claim,
            enabled,
            progress=progress,
            observation=observation,
            prepared=prepared,
        )
    except (ValueError, TypeError) as exc:
        if owner_application_started:
            raise NormalizationPreparationConflict(
                "normalization_owner_application_failed", claim
            ) from exc
        progress.disposition = "failed"
        progress.error_code = "invalid_normalization_record"
        state.failed_documents += 1


async def _normalize_rows(
    session: AsyncSession,
    work: WorkerState,
    state: _SliceState,
    claim: WorkerClaim,
    enabled: bool,
) -> None:
    """Normalize the slice's rows in accepted order within the byte budget.

    The slice stops before the row that would exceed ``NORMALIZATION_BATCH_BYTES`` (after at least one row);
    a single record larger than the whole budget is failed on its own. Remaining rows stay pending.
    """
    for progress, observation in work.normalization_rows:
        data_bytes = len(
            json.dumps(observation.payload, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        if state.processed and state.used_bytes + data_bytes > NORMALIZATION_BATCH_BYTES:
            break
        if data_bytes > NORMALIZATION_BATCH_BYTES:
            progress.disposition = "failed"
            progress.error_code = "record_exceeds_normalization_limit"
            state.processed += 1
            continue
        state.used_bytes += data_bytes
        await _normalize_row(session, work, state, claim, enabled, progress, observation)
        state.processed += 1


def _normalization_filters(work: WorkerState) -> tuple[Any, ...]:
    """Predicates selecting this stage's normalization journal rows (workspace, source, run, version)."""
    return (
        ObservationNormalization.workspace_id == work.scope.workspace_id,
        ObservationNormalization.source_id == work.source.id,
        ObservationNormalization.source_generation == work.source.generation,
        ObservationNormalization.run_id == work.run.id,
        ObservationNormalization.normalization_version == NORMALIZATION_VERSION,
        ObservationNormalization.stage_id == work.stage.id,
    )


async def _count_disposition(session: AsyncSession, work: WorkerState, disposition: str) -> int:
    """Count this stage's journal rows with ``disposition`` (``0`` when the scalar is ``None``)."""
    return int(
        await session.scalar(
            select(func.count())
            .select_from(ObservationNormalization)
            .where(
                *_normalization_filters(work),
                ObservationNormalization.disposition == disposition,
            )
        )
        or 0
    )


async def _settle_terminal_run(
    session: AsyncSession, work: WorkerState, enabled: bool, failed_count: int
) -> None:
    """Derive the run status from its stages, mark the event and record the source processing result.

    Called only when no row is pending: any failed stage fails the run, all-succeeded succeeds it, otherwise it
    stays queued. The event is delivered, or failed when this stage failed.
    """
    run, stage, event, source = work.run, work.stage, work.event, work.source
    if failed_count:
        # One bad record must not fail the run or flag the source: only a slice where nothing was usable fails.
        # The runs/stages status checks have no success-with-warnings value, so a partial success stays
        # 'succeeded'; the failed rows remain visible as disposition='failed' in the normalization journal.
        total = int(
            await session.scalar(
                select(func.count())
                .select_from(ObservationNormalization)
                .where(*_normalization_filters(work))
            )
            or 0
        )
        failed_count = failed_count if failed_count >= total else 0
    stage.status = "failed" if failed_count else "succeeded"
    stage.error_code = "normalization_failed" if failed_count else None
    stages = list(
        (await session.scalars(select(IngestionStage).where(IngestionStage.run_id == run.id))).all()
    )
    if any(item.status == "failed" for item in stages):
        run.status = "failed"
        run.error_code = next(
            (item.error_code for item in stages if item.status == "failed"), "stage_failed"
        )
    elif all(item.status == "succeeded" for item in stages):
        run.status = "succeeded"
        run.error_code = None
    else:
        run.status = "queued"
        run.error_code = None
    event.status = "failed" if failed_count else "delivered"
    await sources.record_processing_result_in_uow(
        session,
        source.id,
        source.generation,
        datetime.now(UTC),
        "normalization_failed" if failed_count else None,
        scope=work.scope,
        multi_workspace_enabled=enabled,
        access_fence=work.access_fence,
        source_fence=work.source,
    )


async def _settle_stage(
    session: AsyncSession,
    work: WorkerState,
    state: _SliceState,
    enabled: bool,
    *,
    native_source: bool,
) -> None:
    """Settle the slice: counts -> stage/run/event status -> native lease -> single commit -> telemetry.

    Pending rows requeue the stage one second out; otherwise the stage is terminal. The native run lease is
    released only when no work remains or the run is terminal. The slice's knowledge changes are published
    with the commit only when rows were processed or the stage finished.
    """
    run, stage, event = work.run, work.stage, work.event
    pending_count = await _count_disposition(session, work, "pending")
    failed_count = await _count_disposition(session, work, "failed")
    stage.result_count = int(
        await session.scalar(
            select(func.coalesce(func.sum(ObservationNormalization.chunk_count), 0)).where(
                *_normalization_filters(work)
            )
        )
        or 0
    )
    stage.lease_expires_at = None
    if pending_count:
        stage.status = "pending"
        run.status = "queued"
        event.status = "pending"
        event.next_attempt_at = datetime.now(UTC) + timedelta(seconds=1)
        _wake_dispatcher_after_commit(session, 1.0)
    else:
        await _settle_terminal_run(session, work, enabled, failed_count)
    if native_source:
        _update_native_run_lease(
            work.state,
            run.id,
            terminal=(not pending_count or run.status in {"succeeded", "failed"}),
        )
    extras = tuple(state.knowledge_changes) if state.processed or not pending_count else ()
    await _commit_ingestion_change(
        session,
        run,
        stage,
        extras,
        scope=work.scope,
        multi_workspace_enabled=enabled,
        access_fence=work.access_fence,
    )
    if state.processed_documents:
        count("ingestion_documents_total", state.processed_documents, outcome="processed")
    if state.failed_documents:
        count("ingestion_documents_total", state.failed_documents, outcome="failed")


async def _defer_until_observations_enabled(
    session: AsyncSession,
    work: WorkerState,
    enabled: bool,
    *,
    native_source: bool,
) -> bool:
    """Defer a measurement slice while the Observations module is lifecycle-disabled; return ``True`` if deferred.

    The connector already accepted canonical provider records, so their normalization progress is retained
    (stage/event pending 60 s out, run queued, native lease renewed) instead of being consumed.
    """
    if work.projection.provider not in MEASUREMENT_PROVIDER_IDS:
        return False
    from modules.settings.public import module_is_enabled

    if await module_is_enabled(
        session, "knowledge.observations", scope=work.scope, multi_workspace_enabled=enabled
    ):
        return False
    run, stage, event = work.run, work.stage, work.event
    deferred_until = datetime.now(UTC) + timedelta(seconds=60)
    stage.status = "pending"
    stage.next_attempt_at = deferred_until
    stage.lease_expires_at = None
    run.status = "queued"
    run.error_code = None
    event.status = "pending"
    event.next_attempt_at = deferred_until
    _wake_dispatcher_after_commit(session, 60.0)
    if native_source:
        _update_native_run_lease(work.state, run.id, terminal=False)
    await _commit_ingestion_change(
        session,
        run,
        stage,
        scope=work.scope,
        multi_workspace_enabled=enabled,
        access_fence=work.access_fence,
    )
    return True


async def _normalize_slice(
    session: AsyncSession, identifier: UUID, enabled: bool, box: _ClaimBox
) -> None:
    """Run one bounded normalization slice inside ``session`` (the caller's single transaction).

    Locks and gates the event, captures the original claim into ``box`` before any effect, short-circuits
    terminal stages, then normalizes rows and settles the stage with exactly one commit. A succeeded stage only
    delivers the event and releases the native lease; a failed stage is left untouched.
    """
    work = await _lock_worker_event(
        session,
        identifier,
        multi_workspace_enabled=enabled,
        event_types=("ingestion.normalize.requested",),
    )
    if work is None or not await _ingestion_enabled(session, work, enabled):
        return
    box.claim = claim = _capture_worker_claim(work)
    run, stage = work.run, work.stage
    native_source = connectors.is_native_provider(work.projection.provider)
    if stage.status == "succeeded":
        await ingestion_api.mark_event_delivered(
            session, identifier, scope=work.scope, multi_workspace_enabled=enabled
        )
        if native_source:
            _update_native_run_lease(work.state, run.id, terminal=True)
        await session.commit()
        return
    if stage.status == "failed":
        return
    if work.document_preparation is None:
        raise NormalizationPreparationConflict("normalization_document_preparation_missing", claim)
    if await _defer_until_observations_enabled(session, work, enabled, native_source=native_source):
        return
    stage.status = "running"
    stage.lease_expires_at = datetime.now(UTC) + timedelta(seconds=STAGE_TIMEOUT_SECONDS)
    stage.error_code = None
    run.status = "running"
    if native_source:
        # Keep the reservation alive across each bounded normalization slice.
        _update_native_run_lease(work.state, run.id, terminal=False)
    state = _SliceState(work.document_preparation, work.observation_preparation)
    await _normalize_rows(session, work, state, claim, enabled)
    await _settle_stage(session, work, state, enabled, native_source=native_source)


async def _settle_failure(
    factory: async_sessionmaker[AsyncSession],
    identifier: UUID,
    enabled: bool,
    box: _ClaimBox,
    exc: Exception,
    event_id: str,
    ctx: dict[str, object] | None = None,
) -> None:
    """Spend one durable failure attempt for a normalization slice that failed, in a fresh transaction.

    The failed slice's transaction has already rolled back. Without a captured claim the whole job is retried
    with jittered exponential backoff (``retry_with_backoff``, base 15 s, cap 60 s); a preparation conflict must agree with the already captured claim. Under the original claim the
    retry-only loader freshly locks admission/Source/roots/outbox; attempts are capped at ``MAX_STAGE_ATTEMPTS``
    (then stage/run/event fail) and the dispatcher is the only next schedule. Lost admission or uncertain
    settlement grants no counter, epoch or lease and is swallowed.
    """
    claim = box.claim
    # The failed factory context has exited/rolled back before this fresh transaction.
    # Without this the retry loop below fails silently (e.g. a CHECK violation retried until exhausted).
    logger.warning(
        "Normalize attempt failed event_id=%s run_id=%s stage_id=%s error_type=%s",
        event_id,
        getattr(claim, "run_id", None),
        getattr(claim, "stage_id", None),
        type(exc).__name__,
        exc_info=exc,
    )
    if isinstance(exc, NormalizationPreparationConflict):
        if claim is None:
            claim = exc.claim
        elif claim != exc.claim:
            raise RuntimeError("Normalization preparation comparisons disagree") from exc
    if claim is None:
        raise retry_with_backoff(ctx, base=15.0, cap=60.0) from exc
    try:
        async with factory() as session:
            work = await _lock_normalization_retry_event(
                session, identifier, multi_workspace_enabled=enabled, expected=claim
            )
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
                delay = random.uniform(0.5, min(60.0, 2.0**stage.attempts))  # noqa: S311  # retry backoff jitter, not a security value
                next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
                stage.status = "retrying"
                stage.error_code = "transient_failure"
                stage.next_attempt_at = next_attempt_at
                event.status = "pending"
                event.next_attempt_at = next_attempt_at
                run.status = "queued"
                _wake_dispatcher_after_commit(session, delay)
            stage.lease_expires_at = None
            if connectors.is_native_provider(work.projection.provider):
                _update_native_run_lease(
                    work.state,
                    run.id,
                    terminal=stage.status == "failed" or run.status in {"succeeded", "failed"},
                )
            await _commit_ingestion_change(
                session,
                run,
                stage,
                scope=work.scope,
                multi_workspace_enabled=enabled,
                access_fence=work.access_fence,
            )
    except (HTTPException, DomainError):
        # Original admission loss grants no counter or scheduling authority.
        return
    except (OperationalError, IntegrityError, RuntimeError):
        # Preserve the old comparison on uncertain settlement; durable outbox recovery
        # decides pending/queued/failed later. Do not restart with a newly captured claim.
        return


@timed("ingestion_stage_ms", stage="normalize")
async def process_normalize_event(ctx: dict[str, object], event_id: str) -> None:
    """Materialize one bounded slice and retain native collection ownership until terminal.

    PostgreSQL progress makes each slice resumable (one transaction per slice). For native providers, the
    matching source run lease is renewed while work remains and released only
    when normalization or its owning run becomes terminal; generic ingestion
    keeps its existing lease lifecycle. Provider validation uses the detached
    connector projection obtained under the source lock. Each registered provider's
    bounded raw metadata fields are merged only through its explicit normalization allowlist
    (see ``ProviderMetadataExtractor``). Telegram selection
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
    explicit pre-write NormalizedDocumentValidationRejected is journaled per record
    (the slice continues and keeps earlier rows); unknown errors after owner entry and
    every post-D/O error roll back the attempt
    before original-claim recovery, never committing a partial invalid-record skip.
    Pre-D conflicts carry the same detached comparison. I-only recovery freshly locks
    admission/Source/roots/outbox, spends one durable failure attempt (maximum five),
    commits scheduling plus replay and returns; dispatcher is the only next schedule.
    Stale/denied/unknown-commit work never invents a counter, epoch or lease. This is
    no bound on all queue wakeups, crashes or infrastructure failures.
    Source ownership remains held and a newer token or run is never modified.
    """
    factory = worker_session_factory(ctx)
    enabled = _worker_flag(ctx)
    identifier = UUID(event_id)
    box = _ClaimBox()
    try:
        async with factory() as session:
            await _normalize_slice(session, identifier, enabled, box)
    except (OperationalError, IntegrityError, RuntimeError) as exc:
        await _settle_failure(factory, identifier, enabled, box, exc, event_id, ctx)
