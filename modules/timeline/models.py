"""Private persistence models for canonical timeline events and extraction."""

from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from core.database import Base


class Event(Base):
    """Store one canonical manual or evidence-derived event and its revision."""
    __tablename__ = "timeline_events"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_timeline_events_revision"),
        UniqueConstraint("extraction_identity", "candidate_hash", name="uq_timeline_event_candidate_identity"),
        CheckConstraint("origin IN ('manual', 'derived')", name="ck_timeline_events_origin"),
        CheckConstraint("date_precision IN ('timed', 'date', 'unknown')", name="ck_timeline_events_precision"),
        CheckConstraint("importance_score IS NULL OR (importance_score >= 0 AND importance_score <= 1)", name="ck_timeline_events_importance"),
        CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="ck_timeline_events_confidence"),
        CheckConstraint("(date_precision = 'timed' AND started_at IS NOT NULL AND occurred_date IS NULL AND end_date IS NULL) OR (date_precision = 'date' AND started_at IS NULL AND occurred_date IS NOT NULL) OR (date_precision = 'unknown' AND started_at IS NULL AND occurred_date IS NULL AND end_date IS NULL)", name="ck_timeline_events_time_shape"),
        CheckConstraint("ended_at IS NULL OR (started_at IS NOT NULL AND ended_at >= started_at)", name="ck_timeline_events_timed_range"),
        CheckConstraint("end_date IS NULL OR (occurred_date IS NOT NULL AND end_date >= occurred_date)", name="ck_timeline_events_date_range"),
        CheckConstraint("valid_to IS NULL OR (valid_from IS NOT NULL AND valid_to > valid_from)", name="ck_timeline_events_validity"),
        Index("ix_timeline_events_timed", "started_at", "id"),
        Index("ix_timeline_events_date", "occurred_date", "id"),
        Index("ix_timeline_events_unknown", "created_at", "id"),
        Index("ix_timeline_events_source", "source_id", "created_at"),
        Index("ix_timeline_events_type", "type", "subtype"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    source_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("sources.id", ondelete="SET NULL"))
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    subtype: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    importance_score: Mapped[float | None]
    confidence: Mapped[float | None]
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    extraction_identity: Mapped[str | None] = mapped_column(String(256))
    candidate_hash: Mapped[str | None] = mapped_column(String(64))
    date_precision: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    occurred_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    occurrence_timezone: Mapped[str | None] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    owner_fields: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class EventParticipant(Base):
    """Link a canonical entity to an event with exact evidence-backed roles."""
    __tablename__ = "timeline_event_participants"
    __table_args__ = (
        UniqueConstraint("event_id", "entity_id", "role", name="uq_timeline_event_participant_role"),
        CheckConstraint("origin IN ('manual', 'derived')", name="ck_timeline_participant_origin"),
        Index("ix_timeline_participant_entity", "entity_id", "event_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("timeline_events.id", ondelete="CASCADE"), nullable=False)
    entity_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("entities.id", ondelete="RESTRICT"), nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")
    origin: Mapped[str] = mapped_column(String(16), nullable=False)


class EventEvidence(Base):
    """Retain exact document-version chunk provenance supporting an event."""
    __tablename__ = "timeline_event_evidence"
    __table_args__ = (
        UniqueConstraint("event_id", "document_version_id", "chunk_id", name="uq_timeline_event_evidence"),
        Index("ix_timeline_event_evidence_source", "source_id", "document_id"),
        Index("ix_timeline_event_evidence_version", "document_version_id", "chunk_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("timeline_events.id", ondelete="CASCADE"), nullable=False)
    source_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("sources.id", ondelete="SET NULL"))
    document_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL"))
    document_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("document_versions.id", ondelete="SET NULL"))
    chunk_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("document_chunks.id", ondelete="SET NULL"))
    version_number: Mapped[int | None] = mapped_column(Integer)
    source_generation: Mapped[int | None] = mapped_column(Integer)
    extraction_identity: Mapped[str | None] = mapped_column(String(256))
    candidate_hash: Mapped[str | None] = mapped_column(String(64))
    confidence: Mapped[float | None]
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    title_snapshot: Mapped[str | None] = mapped_column(String(500))
    url_snapshot: Mapped[str | None] = mapped_column(Text)
    evidence_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    excerpt: Mapped[str | None] = mapped_column(Text)
    metadata_is_version_snapshot: Mapped[bool] = mapped_column(nullable=False, server_default="false")


class ParticipantEvidence(Base):
    """Bind a derived participant role to its exact event evidence chunk."""
    __tablename__ = "timeline_participant_evidence"
    __table_args__ = (UniqueConstraint("participant_id", "event_evidence_id", name="uq_timeline_participant_evidence"),)
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    participant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("timeline_event_participants.id", ondelete="CASCADE"), nullable=False)
    event_evidence_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("timeline_event_evidence.id", ondelete="CASCADE"), nullable=False)


class EventAudit(Base):
    """Record one owner revision with the exact changed values and reason."""
    __tablename__ = "timeline_event_audits"
    __table_args__ = (Index("ix_timeline_event_audits_event", "event_id", "created_at"),)
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("timeline_events.id", ondelete="CASCADE"), nullable=False)
    actor_id: Mapped[int] = mapped_column(ForeignKey("owner.id", ondelete="CASCADE"), nullable=False)
    reason: Mapped[str] = mapped_column(String(300), nullable=False)
    prior_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    resulting_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    changed_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EventSuppression(Base):
    """Prevent an owner-deleted derived proposal from returning on identical retry."""
    __tablename__ = "timeline_event_suppressions"
    __table_args__ = (UniqueConstraint("document_version_id", "candidate_hash", name="uq_timeline_event_suppression_identity"),)
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    document_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    source_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    source_generation: Mapped[int | None] = mapped_column(Integer)
    candidate_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class TimelineExtractionWork(Base):
    """Persist event extraction lease, retries, and source-version fence."""
    __tablename__ = "timeline_extraction_work"
    __table_args__ = (
        UniqueConstraint("document_version_id", "source_generation", "extractor_version", "prompt_version", name="uq_timeline_extraction_work_identity"),
        CheckConstraint("status IN ('pending', 'running', 'succeeded', 'blocked', 'failed')", name="ck_timeline_extraction_work_status"),
        CheckConstraint("source_generation >= 1 AND attempt >= 0", name="ck_timeline_extraction_work_bounds"),
        Index("ix_timeline_extraction_recovery", "status", "next_attempt_at", "lease_expires_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    document_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False)
    source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False)
    source_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    dependency_fingerprint: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class TimelineExtractionResult(Base):
    """Retain bounded proposals, exact support IDs, and skipped-refresh fields, never raw chunks."""
    __tablename__ = "timeline_extraction_results"
    __table_args__ = (UniqueConstraint("work_id", name="uq_timeline_extraction_result_work"),)
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    work_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("timeline_extraction_work.id", ondelete="CASCADE"), nullable=False)
    model: Mapped[str | None] = mapped_column(String(200))
    proposals_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
