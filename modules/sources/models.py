from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from core.database import Base


class Source(Base):
    """Persist a collection identity, lifecycle generation, and sync state.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """
    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'paused', 'archived')", name="ck_sources_status"
        ),
        Index("ix_sources_created_at_id", "created_at", "id"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_sources_workspace", ondelete="RESTRICT"),
        UniqueConstraint("workspace_id", "id", name="uq_w2_sources_id"),
        Index("ix_w2_sources_scope", 'workspace_id', 'id'),
        Index("ix_w2_sources_work", 'workspace_id', 'created_at', 'id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    local_only: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collection_error_code: Mapped[str | None] = mapped_column(String(64))
    processing_error_code: Mapped[str | None] = mapped_column(String(64))
    generation: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    configuration: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )



class SourcePurgeOperation(Base):
    """Track source canonical deletion and truthful owner-stage cleanup progress.

    Workspace and actor identities survive nullable or detached canonical references.
    Captured membership_revision identifies the authorization epoch of the durable job;
    runtime resolvers must revalidate it rather than infer authority from the remaining Source.
    """
    __tablename__ = "source_purge_operations"
    __table_args__ = (
        CheckConstraint("membership_revision > 0", name="ck_w2_source_purge_membership_revision_positive"),
        CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed')", name="ck_source_purge_operations_status"),
        CheckConstraint(
            "documents_status IN ('queued', 'deleted', 'failed', 'unavailable')",
            name="ck_source_purge_operations_documents_status",
        ),
        CheckConstraint(
            "memory_status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_source_purge_operations_memory_status",
        ),
        CheckConstraint(
            "memory_cursor IS NULL OR octet_length(memory_cursor::text) <= 4096",
            name="ck_source_purge_operations_memory_cursor_bound",
        ),
        CheckConstraint("memory_unresolved_count >= 0", name="ck_source_purge_operations_memory_unresolved"),
        Index("ix_source_purge_operations_status_created", "status", "created_at"),
        Index("ix_source_purge_operations_source_id", "source_id", "id"),
        # Coverage reconciliation: unfinished Source-local Memory work, excluding terminal unavailable rows.
        Index(
            "ix_source_purge_operations_memory_reconcile", "id",
            postgresql_where=text(
                "documents_status = 'deleted' AND (memory_status IN ('queued', 'running') "
                "OR memory_cache_pending OR (memory_status = 'failed' AND memory_error_code "
                "NOT IN ('evidence_identity_unavailable', 'legacy_provenance_unresolved')))"
            ),
        ),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_source_purge_operations_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "actor_user_id"], ['workspaces.id', 'workspaces.owner_user_id'], name="fk_w2_source_purge_operations_principal", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "source_id"], ["sources.workspace_id", "sources.id"], name="fk_w2_source_purge_operations_source_id", ondelete="RESTRICT"),
        Index("ix_w2_source_purge_operations_scope", 'workspace_id', 'id'),
        Index("ix_w2_source_purge_operations_work", 'workspace_id', 'created_at', 'id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    actor_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    membership_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)


    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    # The legacy URI JSON remains for compatibility repair; new work never populates it.
    documents_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="queued")
    pending_child_count: Mapped[int | None] = mapped_column(Integer)
    failed_child_count: Mapped[int | None] = mapped_column(Integer)
    pending_owner_codes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    raw_uris: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    # Source-local Memory copied-evidence coverage (independent of per-Document receipts). It starts
    # queued: no historical operation is ever presumed clean. Unavailable is durable ``failed`` with
    # evidence_identity_unavailable / legacy_provenance_unresolved and is never retried automatically.
    memory_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="queued")
    memory_error_code: Mapped[str | None] = mapped_column(String(64))
    memory_cursor: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    memory_unresolved_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    memory_cache_pending: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    coverage_reopened: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="queued")
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


