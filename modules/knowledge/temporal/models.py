"""PostgreSQL-owned temporal reservations and cleanup; detached IDs survive owner deletion."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base, CreatedAtMixin


class GraphAllocation(Base):
    """Serialize immutable bucket allocation within a source generation, without cascading FKs."""
    __tablename__ = "temporal_allocations"
    source_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, primary_key=True)
    next_bucket: Mapped[int] = mapped_column(Integer, default=0)


class GraphPartition(Base, CreatedAtMixin):
    """Reserve bounded history and one exclusive writer; uncertainty survives lease expiry."""
    __tablename__ = "temporal_partitions"
    __table_args__ = (
        UniqueConstraint("source_id", "generation", "ordinal"),
        CheckConstraint(
            "reservations BETWEEN 0 AND 100 AND evidence_reservations BETWEEN 0 AND 100",
            name="ck_temporal_partition_bound",
        ),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    source_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    ordinal: Mapped[int] = mapped_column(Integer)
    reservations: Mapped[int] = mapped_column(Integer, default=0)
    evidence_reservations: Mapped[int] = mapped_column(Integer, default=0)
    sealed: Mapped[bool] = mapped_column(Boolean, default=False)
    lease_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    uncertain_operation_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))


class GraphMapping(Base, CreatedAtMixin):
    """Own a stable version episode and desired/applied revisions independent of graph availability."""
    __tablename__ = "temporal_mappings"
    __table_args__ = (UniqueConstraint("document_version_id", "source_generation"),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    episode_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), unique=True, default=uuid4)
    partition_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    source_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    source_generation: Mapped[int] = mapped_column(Integer)
    document_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    document_version_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    local_only: Mapped[bool] = mapped_column(Boolean)
    desired_revision: Mapped[int] = mapped_column(Integer, default=1)
    applied_revision: Mapped[int] = mapped_column(Integer, default=0)
    desired_digest: Mapped[str] = mapped_column(String(64))
    applied_digest: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    error_code: Mapped[str | None] = mapped_column(String(64))
    tombstoned: Mapped[bool] = mapped_column(Boolean, default=False)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canonical_state: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    embedding_identity: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    external_state: Mapped[str] = mapped_column(String(16), default="absent")


class GraphSupport(Base):
    """Retain exact identifiers needed to authorize or purge a mapping before evidence cascades."""
    __tablename__ = "temporal_supports"
    mapping_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    document_version_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    chunk_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    document_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    source_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    source_generation: Mapped[int] = mapped_column(Integer)
    removed: Mapped[bool] = mapped_column(Boolean, default=False)


class GraphOperation(Base, CreatedAtMixin):
    """Durably queue one exact episode attempt; receipt identity is stable across recovery leases."""
    __tablename__ = "temporal_operations"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    mapping_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    partition_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    desired_revision: Mapped[int] = mapped_column(Integer)
    desired_digest: Mapped[str] = mapped_column(String(64))
    receipt_token: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), default=uuid4)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    phase: Mapped[str] = mapped_column(String(40), default="intent_committed")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatch_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cessation_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cessation_reason: Mapped[str | None] = mapped_column(String(64))
    dispatch_server_run_id: Mapped[str | None] = mapped_column(String(64))
    dispatch_client_id: Mapped[int | None] = mapped_column(BigInteger)
    cleanup_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    dependency_fingerprint: Mapped[str | None] = mapped_column(String(64))
    replacement_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GraphDispatch(Base, CreatedAtMixin):
    """Journal every immutable synchronous transport independently of operation retries.

    Identity columns are insert-only owner data. Completion or exact-client
    cessation updates only that dispatch row under the current operation lease;
    neither timestamp certifies graph convergence. Detached IDs survive evidence
    deletion, and a later inspection scope never overwrites an earlier unknown
    transport. No graph text, prompts or source foreign keys are retained.
    """

    __tablename__ = "temporal_dispatches"
    __table_args__ = (UniqueConstraint("operation_id", "server_run_id", "client_id"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    operation_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    group_id: Mapped[str] = mapped_column(String(200))
    lease_owner: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    server_run_id: Mapped[str] = mapped_column(String(40))
    client_id: Mapped[int] = mapped_column(BigInteger)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cessation_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cessation_reason: Mapped[str | None] = mapped_column(String(64))


class GraphReceipt(Base, CreatedAtMixin):
    """Append every prewrite exact-ID/hash receipt in operation order; never retain narrative text."""
    __tablename__ = "temporal_receipts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    operation_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("operation_id", "sequence"),)


class GraphChange(Base, CreatedAtMixin):
    """Record identifier/field change history from this revision onward without deleted value snapshots."""
    __tablename__ = "temporal_changes"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)
    canonical_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    revision: Mapped[int | None] = mapped_column(Integer)
    fingerprint: Mapped[str | None] = mapped_column(String(64))
    changed_fields: Mapped[list[str]] = mapped_column(JSONB)
    origin: Mapped[str] = mapped_column(String(16))
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    support: Mapped[list[list[str]]] = mapped_column(JSONB, default=list)


class GraphReconcileRun(Base, CreatedAtMixin):
    """Resume a bounded owner-selected reconciliation slice with immutable coverage boundary."""
    __tablename__ = "temporal_reconcile_runs"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB)
    upper_mapping_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    cursor: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    scanned: Mapped[int] = mapped_column(Integer, default=0)
    queued: Mapped[int] = mapped_column(Integer, default=0)
    converged: Mapped[int] = mapped_column(Integer, default=0)
    blocked: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)


class GraphReconcileMember(Base):
    """Retain each run's exact selected desired revision through cleanup and later canonical corrections."""

    __tablename__ = "temporal_reconcile_members"
    run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    mapping_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    desired_revision: Mapped[int] = mapped_column(Integer)
    desired_digest: Mapped[str] = mapped_column(String(64))
    tombstoned: Mapped[bool] = mapped_column(Boolean)


class GraphRebuildDependency(Base, CreatedAtMixin):
    """Retain original destructive-operation proof and exact dependent effects through later projection rebuilds."""

    __tablename__ = "temporal_rebuild_dependencies"
    operation_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    mapping_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    source_generation: Mapped[int] = mapped_column(Integer)
    effect_ids: Mapped[list[list[str]]] = mapped_column(JSONB)
