from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from core.database import Base


class Source(Base):
    """Persist a collection identity, lifecycle generation, and sync state."""
    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'paused', 'archived')", name="ck_sources_status"
        ),
        Index("ix_sources_created_at_id", "created_at", "id"),
    )

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
    configuration: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SourcePurgeOperation(Base):
    """Track source canonical deletion and truthful owner-stage cleanup progress."""
    __tablename__ = "source_purge_operations"
    __table_args__ = (
        CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed')", name="ck_source_purge_operations_status"),
        CheckConstraint(
            "documents_status IN ('queued', 'deleted', 'failed', 'unavailable')",
            name="ck_source_purge_operations_documents_status",
        ),
        Index("ix_source_purge_operations_status_created", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    # The legacy URI JSON remains for compatibility repair; new work never populates it.
    documents_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="queued")
    pending_child_count: Mapped[int | None] = mapped_column(Integer)
    failed_child_count: Mapped[int | None] = mapped_column(Integer)
    pending_owner_codes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    raw_uris: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="queued")
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

