from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
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


class Entity(Base):
    """Persist canonical entity fields, provenance origins, and owner revision."""
    __tablename__ = "entities"
    __table_args__ = (
        CheckConstraint(
            "type IN ('person', 'organization', 'company', 'project', 'repository', 'place', 'country', 'product', 'topic', 'technology', 'asset', 'device', 'website', 'event_subject', 'other')",
            name="ck_entities_type",
        ),
        CheckConstraint("revision >= 1", name="ck_entities_revision"),
        Index("ix_entities_type_canonical_name", "type", "canonical_name"),
        Index("ix_entities_created_at_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str | None] = mapped_column(String(300))
    canonical_name: Mapped[str | None] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    name_origin: Mapped[str | None] = mapped_column(String(16))
    description_origin: Mapped[str | None] = mapped_column(String(16))
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class EntityAlias(Base):
    """Persist a normalized alias with confirmation and derivation provenance."""
    __tablename__ = "entity_aliases"
    __table_args__ = (
        UniqueConstraint("entity_id", "normalized_alias", name="uq_entity_aliases_entity_normalized"),
        Index("ix_entity_aliases_normalized", "normalized_alias"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    entity_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    alias: Mapped[str] = mapped_column(String(300), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(300), nullable=False)
    source_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("sources.id", ondelete="SET NULL"))
    confirmed: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    origin: Mapped[str | None] = mapped_column(String(16))
    confidence: Mapped[float | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EntityEvidenceMembership(Base):
    """Link an entity to exact document-version evidence with retry identity."""
    __tablename__ = "entity_evidence_memberships"
    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_entity_evidence_confidence"),
        UniqueConstraint("extraction_identity", "candidate_key", "chunk_id", name="uq_entity_evidence_retry"),
        Index("ix_entity_evidence_entity", "entity_id", "id"),
        Index("ix_entity_evidence_version", "document_version_id"),
        Index("ix_entity_evidence_chunk", "chunk_id"),
        Index("ix_entity_evidence_document", "document_id"),
        Index("ix_entity_evidence_source", "source_id"),
        Index("ix_entity_evidence_match_fingerprint", "match_fingerprint"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    entity_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    document_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False)
    document_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False)
    chunk_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("document_chunks.id", ondelete="CASCADE"), nullable=False)
    extraction_identity: Mapped[str | None] = mapped_column(String(256))
    candidate_key: Mapped[str | None] = mapped_column(String(256))
    match_fingerprint: Mapped[str | None] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    confidence: Mapped[float] = mapped_column(nullable=False)


class EntityAliasEvidence(Base):
    """Associate alias support with the entity membership that justifies it."""
    __tablename__ = "entity_alias_evidence"
    __table_args__ = (
        UniqueConstraint("alias_id", "membership_id", name="uq_entity_alias_evidence"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_entity_alias_evidence_confidence"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    alias_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("entity_aliases.id", ondelete="CASCADE"), nullable=False)
    membership_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("entity_evidence_memberships.id", ondelete="CASCADE"), nullable=False)
    confidence: Mapped[float] = mapped_column(nullable=False)


class EntityFieldEvidence(Base):
    """Memberships supporting the exact currently published derived field value."""

    __tablename__ = "entity_field_evidence"
    __table_args__ = (
        CheckConstraint("field_name IN ('name', 'description')", name="ck_entity_field_evidence_field"),
        UniqueConstraint(
            "entity_id", "field_name", "value_hash", "membership_id",
            name="uq_entity_field_evidence_support",
        ),
        Index("ix_entity_field_evidence_current", "entity_id", "field_name", "value_hash"),
        Index("ix_entity_field_evidence_membership", "membership_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    entity_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    field_name: Mapped[str] = mapped_column(String(16), nullable=False)
    value_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    membership_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("entity_evidence_memberships.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EntityOwnerAction(Base):
    """Record an owner correction and affected IDs and revisions for audit."""
    __tablename__ = "entity_owner_actions"
    __table_args__ = (Index("ix_entity_owner_actions_created", "created_at", "id"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    actor_id: Mapped[int] = mapped_column(ForeignKey("owner.id", ondelete="CASCADE"), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(300), nullable=False)
    affected_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    revisions: Mapped[dict[str, int | None]] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EntityRedirect(Base):
    """Retain the owner-authored redirect from a merged entity to its target."""
    __tablename__ = "entity_redirects"
    __table_args__ = (Index("ix_entity_redirect_target", "target_entity_id"),)

    old_entity_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True)
    target_entity_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("entities.id", ondelete="SET NULL"))
    actor_id: Mapped[int] = mapped_column(ForeignKey("owner.id", ondelete="CASCADE"), nullable=False)
    reason: Mapped[str] = mapped_column(String(300), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EntityCorrectionDecision(Base):
    """Persist an owner assignment or suppression at evidence or document scope."""
    __tablename__ = "entity_correction_decisions"
    __table_args__ = (
        CheckConstraint("decision IN ('assign', 'suppress')", name="ck_entity_correction_decision_kind"),
        CheckConstraint("scope = 'evidence' OR (scope = 'document' AND document_id IS NOT NULL)", name="ck_entity_correction_decision_scope"),
        Index("ix_entity_correction_decision_match", "document_id", "match_fingerprint", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    entity_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("entities.id", ondelete="CASCADE"))
    document_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"))
    membership_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("entity_evidence_memberships.id", ondelete="CASCADE"))
    match_fingerprint: Mapped[str | None] = mapped_column(String(64))
    actor_id: Mapped[int] = mapped_column(ForeignKey("owner.id", ondelete="CASCADE"), nullable=False)
    reason: Mapped[str] = mapped_column(String(300), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EntityExtractionWork(Base):
    """Track versioned extraction attempts, leases, retry timing, and dependencies."""
    __tablename__ = "entity_extraction_work"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'running', 'succeeded', 'blocked', 'failed')", name="ck_entity_extraction_work_status"),
        CheckConstraint("attempt >= 0", name="ck_entity_extraction_work_attempt"),
        CheckConstraint("source_generation >= 1", name="ck_entity_extraction_work_generation"),
        UniqueConstraint("document_version_id", "extractor_version", "prompt_version", name="uq_entity_extraction_work_identity"),
        Index("ix_entity_extraction_work_recovery", "status", "next_attempt_at", "lease_expires_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    document_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False)
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


class EntityExtractionResult(Base):
    """Persist bounded extraction facts and review candidates for completed work."""
    __tablename__ = "entity_extraction_results"
    __table_args__ = (UniqueConstraint("work_id", name="uq_entity_extraction_results_work"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    work_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("entity_extraction_work.id", ondelete="CASCADE"), nullable=False)
    model: Mapped[str | None] = mapped_column(String(200))
    usage_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    facts_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    review_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default="[]")
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
