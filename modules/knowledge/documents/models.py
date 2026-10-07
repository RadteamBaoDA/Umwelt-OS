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
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from core.database import Base


class Document(Base):
    """Persist current document metadata and its selected immutable revision.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_documents_source_external"),
        Index("ix_documents_source_id", "source_id"),
        Index("ix_documents_external_id", "external_id"),
        Index("ix_documents_published_at", "published_at"),
        Index("ix_documents_created_at_id", "created_at", "id"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_documents_workspace", ondelete="RESTRICT"),
        UniqueConstraint("workspace_id", "id", name="uq_w2_documents_id"),
        ForeignKeyConstraint(["workspace_id", "source_id"], ["sources.workspace_id", "sources.id"], name="fk_w2_documents_source_id", ondelete="RESTRICT"),
        Index("ix_w2_documents_scope", 'workspace_id', 'id'),
        Index("ix_w2_documents_work", 'workspace_id', 'created_at', 'id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    source_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    external_id: Mapped[str | None] = mapped_column(String(512))
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(64))
    mime_type: Mapped[str | None] = mapped_column(String(255))
    raw_uri: Mapped[str | None] = mapped_column(Text)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String(500))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default="{}"
    )
    current_version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    extraction_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="ready")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    language: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )



class DocumentVersion(Base):
    """Persist one immutable document content revision and its content hash."""
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_number", name="uq_document_versions_number"),
        Index("ix_document_versions_created_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DocumentInteraction(Base):
    """Persist owner read and bookmark state against one immutable document version."""
    __tablename__ = "document_interactions"
    __table_args__ = (
        CheckConstraint("read_at IS NOT NULL OR bookmarked_at IS NOT NULL", name="ck_document_interactions_nonempty"),
        Index("ix_document_interactions_owner_read", "owner_id", "read_at"),
    )

    owner_id: Mapped[int] = mapped_column(ForeignKey("owner.id", ondelete="CASCADE"), primary_key=True)
    document_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("document_versions.id", ondelete="CASCADE"), primary_key=True
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bookmarked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class NormalizedDocumentIdentity(Base):
    """Reserve a source/external-ID mapping, including deleted-identity tombstones.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """
    __tablename__ = "normalized_document_identities"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_normalized_document_identities_source_external"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_normalized_document_identities_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "source_id"], ["sources.workspace_id", "sources.id"], name="fk_w2_normalized_document_identities_source_id", ondelete="CASCADE"),
        # Scalar SET NULL clears only the parent ID; this deferred FK retains workspace.
        ForeignKeyConstraint(["workspace_id", "document_id"], ["documents.workspace_id", "documents.id"], name="fk_w2_normalized_document_identities_document_id", ondelete="NO ACTION", deferrable=True, initially="DEFERRED"),
        Index("ix_w2_normalized_document_identities_scope", 'workspace_id', 'id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    external_id: Mapped[str] = mapped_column(String(512), nullable=False)
    document_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL"))
    tombstoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))



class NormalizedVersionProvenance(Base):
    """Snapshot accepted provider and observation provenance for a normalized version."""
    __tablename__ = "normalized_version_provenance"
    __table_args__ = (
        UniqueConstraint("document_id", "accepted_record_hash", "normalization_version", name="uq_normalized_version_provenance_identity"),
        Index("ix_normalized_version_provenance_version", "document_version_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    document_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False)
    provider_id: Mapped[str] = mapped_column(String(512), nullable=False)
    provider_version: Mapped[str | None] = mapped_column(String(255))
    accepted_record_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    normalization_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    selection_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_type: Mapped[str | None] = mapped_column(String(64))
    provenance_json: Mapped[dict[str, Any]] = mapped_column("provenance", JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class DocumentChunk(Base):
    """Persist a bounded searchable segment tied to one immutable document version."""
    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("document_version_id", "chunk_index", name="uq_document_chunks_version_index"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    document_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default="{}"
    )


class DocumentCleanupOperation(Base):
    """Retain individual or source-scoped document cleanup stages and detached identities.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """
    __tablename__ = "document_cleanup_operations"
    __table_args__ = (
        CheckConstraint(
            "(membership_revision IS NULL AND configuration_revision IS NULL AND source_generation IS NULL) OR "
            "(membership_revision IS NOT NULL AND configuration_revision IS NOT NULL AND source_generation IS NOT NULL "
            "AND membership_revision > 0 AND configuration_revision > 0 AND source_generation > 0)",
            name="ck_document_cleanup_original_epoch",
        ),
        CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed')", name="ck_document_cleanup_status"),
        CheckConstraint("record_status = 'deleted'", name="ck_document_cleanup_record_status"),
        CheckConstraint("graph_status = 'tombstoned'", name="ck_document_cleanup_graph_status"),
        CheckConstraint("raw_status IN ('queued', 'not_present', 'retained_shared', 'succeeded', 'failed')", name="ck_document_cleanup_raw_status"),
        CheckConstraint("evidence_scope_status IN ('capturing', 'captured', 'unavailable')", name="ck_document_cleanup_evidence_scope_status"),
        CheckConstraint("copied_status IN ('queued', 'running', 'succeeded', 'failed')", name="ck_document_cleanup_copied_status"),
        CheckConstraint("chat_status IN ('queued', 'running', 'succeeded', 'failed')", name="ck_document_cleanup_chat_status"),
        CheckConstraint("copied_cursor IS NULL OR octet_length(copied_cursor::text) <= 4096", name="ck_document_cleanup_copied_cursor_bound"),
        CheckConstraint("memory_status IN ('queued', 'running', 'succeeded', 'failed')", name="ck_document_cleanup_memory_status"),
        CheckConstraint("memory_cursor IS NULL OR octet_length(memory_cursor::text) <= 4096", name="ck_document_cleanup_memory_cursor_bound"),
        CheckConstraint("memory_unresolved_count >= 0", name="ck_document_cleanup_memory_unresolved_nonnegative"),
        CheckConstraint("agent_status IN ('queued', 'running', 'succeeded', 'failed')", name="ck_document_cleanup_agent_status"),
        CheckConstraint("agent_cursor IS NULL OR octet_length(agent_cursor::text) <= 4096", name="ck_document_cleanup_agent_cursor_bound"),
        CheckConstraint("agent_unresolved_count >= 0", name="ck_document_cleanup_agent_unresolved_nonnegative"),
        CheckConstraint("materialization_status IN ('queued', 'running', 'succeeded', 'failed')", name="ck_document_cleanup_materialization_status"),
        CheckConstraint("materialization_cursor IS NULL OR octet_length(materialization_cursor::text) <= 4096", name="ck_document_cleanup_materialization_cursor_bound"),
        CheckConstraint("materialization_unresolved_count >= 0", name="ck_document_cleanup_materialization_unresolved_nonnegative"),
        CheckConstraint("brief_status IN ('queued', 'running', 'succeeded', 'failed')", name="ck_document_cleanup_brief_status"),
        CheckConstraint("brief_cursor IS NULL OR octet_length(brief_cursor::text) <= 4096", name="ck_document_cleanup_brief_cursor_bound"),
        CheckConstraint("brief_unresolved_count >= 0", name="ck_document_cleanup_brief_unresolved_nonnegative"),
        Index("ix_document_cleanup_status_created", "status", "created_at"),
        Index(
            "ix_document_cleanup_copied_stages_reconcile", "id",
            postgresql_where=text(
                "materialization_status IN ('queued', 'running') OR brief_status IN ('queued', 'running')"
            ),
        ),
        Index(
            "ix_document_cleanup_agent_reconcile", "id",
            postgresql_where=text("agent_status IN ('queued', 'running')"),
        ),
        Index("ix_document_cleanup_source_purge_id", "source_purge_operation_id", "id"),
        # Source-wide historical coverage aggregates by exact source, including NULL/older linkage.
        Index("ix_document_cleanup_source_id_id", "source_id", "id"),
        Index(
            "uq_document_cleanup_source_purge_document",
            "source_purge_operation_id", "document_id", unique=True,
            postgresql_where=text("source_purge_operation_id IS NOT NULL"),
        ),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_document_cleanup_operations_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "actor_user_id"], ['workspaces.id', 'workspaces.owner_user_id'], name="fk_w2_document_cleanup_operations_principal", ondelete="RESTRICT"),
        UniqueConstraint("workspace_id", "id", name="uq_w2_document_cleanup_operations_id"),
        Index("ix_w2_document_cleanup_operations_scope", 'workspace_id', 'id'),
        Index("ix_w2_document_cleanup_operations_work", 'workspace_id', 'created_at', 'id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    actor_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # NULL is quarantined legacy authority; new receipts capture the exact admitted epoch.
    membership_revision: Mapped[int | None] = mapped_column(BigInteger)
    configuration_revision: Mapped[int | None] = mapped_column(BigInteger)
    source_generation: Mapped[int | None] = mapped_column(Integer)


    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    # Documents keeps this owner-local linkage; no cross-owner foreign key is introduced.
    source_purge_operation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    # No FK: this receipt must outlive the hard-deleted document rows.
    document_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    raw_uri: Mapped[str | None] = mapped_column(Text)
    record_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="deleted")
    graph_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="tombstoned")
    raw_status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="queued")
    evidence_scope_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="unavailable")
    # This aggregate stays running until every copied-evidence owner stage is integrated.
    copied_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="queued")
    copied_cursor: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    copied_error_code: Mapped[str | None] = mapped_column(String(64))
    # Each copied owner keeps an independent bounded continuation state.
    chat_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="queued")
    chat_error_code: Mapped[str | None] = mapped_column(String(64))
    memory_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="queued")
    memory_error_code: Mapped[str | None] = mapped_column(String(64))
    memory_cursor: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    memory_unresolved_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    memory_cache_pending: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    agent_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="queued")
    agent_error_code: Mapped[str | None] = mapped_column(String(64))
    agent_cursor: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    agent_unresolved_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    agent_waiting_for_lease: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # Notifications/Automations copied-metadata stage; its cursor is private to that stage.
    materialization_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="queued")
    materialization_error_code: Mapped[str | None] = mapped_column(String(64))
    materialization_cursor: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    materialization_unresolved_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Dashboard saved-brief stage; unresolved counts legacy briefs whose prompt lineage is unknowable.
    brief_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="queued")
    brief_error_code: Mapped[str | None] = mapped_column(String(64))
    brief_cursor: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    brief_unresolved_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # DB-recorded earliest version time captured before canonical deletion; NULL = unknown (historical).
    earliest_version_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="queued")
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())



class DocumentCleanupEvidenceReference(Base):
    """Retain immutable version/chunk IDs needed to clean copies after hard deletion.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """

    __tablename__ = "document_cleanup_evidence_references"
    __table_args__ = (
        CheckConstraint(
            "(reference_kind = 'version' AND chunk_id IS NULL) OR "
            "(reference_kind = 'chunk' AND chunk_id IS NOT NULL)",
            name="ck_document_cleanup_evidence_reference_shape",
        ),
        Index("ix_document_cleanup_evidence_operation_id", "operation_id", "id"),
        Index("ix_document_cleanup_evidence_document_version_id", "document_version_id"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_document_cleanup_evidence_references_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "operation_id"], ["document_cleanup_operations.workspace_id", "document_cleanup_operations.id"], name="fk_w2_document_cleanup_evidence_references_operation_id", ondelete="CASCADE"),
        Index("ix_w2_document_cleanup_evidence_references_scope", 'workspace_id', 'id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    operation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    # No FK to document_versions or document_chunks: these identities outlive their rows.
    document_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    chunk_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    reference_kind: Mapped[str] = mapped_column(String(16), nullable=False)

