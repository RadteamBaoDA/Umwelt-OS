from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    func,
    literal_column,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from core.database import Base


class RelationshipSnapshotHistory(Base):
    """Retain owner-observed canonical states without cascading away deletion evidence.

    State excludes citation text/title/URLs. Exact support IDs authorize fresh
    citation reads; source purge clears derived state rather than preserving raw
    removed text. Empty state means unavailable, never a fabricated past value.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """
    __tablename__ = "relationship_snapshot_history"
    __table_args__ = (
        Index("ix_relationship_snapshot_history_relationship", "relationship_id", "recorded_at", "id"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_relationship_snapshot_history_workspace", ondelete="RESTRICT"),
        Index("ix_w2_relationship_snapshot_history_scope", 'workspace_id', 'id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    relationship_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    support: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")



class Relationship(Base):
    """Persist a directed entity relationship with origin and optional validity.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """
    __tablename__ = "relationships"
    __table_args__ = (
        CheckConstraint("source_entity_id <> target_entity_id", name="ck_relationships_distinct_entities"),
        CheckConstraint("origin IN ('owner', 'derived')", name="ck_relationships_origin"),
        CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="ck_relationships_confidence"),
        Index("ix_relationships_source_type", "source_entity_id", "type"),
        Index("ix_relationships_target_type", "target_entity_id", "type"),
        Index("ix_relationships_created_at_id", "created_at", "id"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_relationships_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "source_entity_id"], ["entities.workspace_id", "entities.id"], name="fk_w2_relationships_source_entity_id", ondelete="CASCADE"),
        ForeignKeyConstraint(["workspace_id", "target_entity_id"], ["entities.workspace_id", "entities.id"], name="fk_w2_relationships_target_entity_id", ondelete="CASCADE"),
        Index("ix_w2_relationships_scope", 'workspace_id', 'id'),
        Index("ix_w2_relationships_work", 'workspace_id', 'created_at', 'id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    source_entity_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    target_entity_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float | None]
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())



class RelationshipEvidence(Base):
    """Persist versioned chunk support and optional entity-membership endpoints."""
    __tablename__ = "relationship_evidence"
    __table_args__ = (
        Index(
            "uq_relationship_evidence_fact_endpoint_pair", "relationship_id", "document_version_id", "chunk_id",
            func.coalesce(literal_column("source_membership_id"), literal_column("'00000000-0000-0000-0000-000000000000'::uuid")),
            func.coalesce(literal_column("target_membership_id"), literal_column("'00000000-0000-0000-0000-000000000000'::uuid")),
            unique=True,
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_relationship_evidence_confidence"),
        Index("ix_relationship_evidence_version", "document_version_id"),
        Index("ix_relationship_evidence_chunk", "chunk_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    relationship_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("relationships.id", ondelete="CASCADE"), nullable=False)
    document_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False)
    chunk_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("document_chunks.id", ondelete="CASCADE"), nullable=False)
    document_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"))
    source_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("sources.id", ondelete="SET NULL"))
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    confidence: Mapped[float] = mapped_column(nullable=False)
    source_membership_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("entity_evidence_memberships.id", ondelete="SET NULL")
    )
    target_membership_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("entity_evidence_memberships.id", ondelete="SET NULL")
    )
