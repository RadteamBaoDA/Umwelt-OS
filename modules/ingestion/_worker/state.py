"""Worker constants, claim/candidate dataclasses and the detached claim capture helpers."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select

from core.workspaces.schemas import AccessFence, InternalJobScope
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
from modules.knowledge.observations import public as observations
from modules.sources.public import ConnectorSource, SourceFence

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


def _capture_worker_claim(work: WorkerState) -> WorkerClaim:
    """Detach the exact queued timestamp and current stage attempt before releasing locks."""
    if work.event.dispatched_at is None:
        raise ValueError("A durable dispatch claim is required")
    return WorkerClaim(
        scope=work.scope,
        access_fence=work.access_fence,
        source_fence=work.source,
        event_id=work.event.id,
        event_type=work.event.type,
        producer=work.event.producer,
        dispatched_at=work.event.dispatched_at,
        payload_hash=hashlib.sha256(
            json.dumps(
                work.event.payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        ).hexdigest(),
        run_id=work.run.id,
        batch_id=work.batch.id,
        stage_id=work.stage.id,
        attempts=work.stage.attempts,
        stage_status=work.stage.status,
        stage_lease_expires_at=work.stage.lease_expires_at,
        state_lease_run_id=work.state.lease_run_id if work.state is not None else None,
        state_collection_token=work.state.collection_lease_token
        if work.state is not None
        else None,
    )


def _same_worker_claim(work: WorkerState, claim: WorkerClaim) -> bool:
    """Compare the complete immutable original dispatch/stage snapshot; never rebase it."""
    return _capture_worker_claim(work) == claim


@dataclass(frozen=True)
class _Admitted:
    """Detached result of admitting one queued event: principal, original access fence and envelope identity."""

    scope: InternalJobScope
    access_fence: AccessFence
    delivery: EventDelivery
    run_id: UUID
    stage_id: UUID


@dataclass(frozen=True)
class _Discovery:
    """Nonlocking normalization discovery: the bounded candidate query, its identities and D's preparation."""

    query: Select[*tuple[Any, ...]]
    candidates: tuple[NormalizationCandidate, ...]
    batch_id: UUID
    document_preparation: Any
