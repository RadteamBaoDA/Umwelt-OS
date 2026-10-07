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
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import UserDefinedType, Uuid

from core.database import Base


class Vector(UserDefinedType[Any]):
    """Represent PostgreSQL's pgvector type in SQLAlchemy column metadata."""
    cache_ok = True

    def get_col_spec(self, **kw: object) -> str:
        """Return the PostgreSQL column type name used in generated SQL."""
        return "vector"


class IndexGeneration(Base):
    """Persist one embedding configuration and its index-generation lifecycle."""
    __tablename__ = "search_index_generations"
    __table_args__ = (
        CheckConstraint("status IN ('queued', 'running', 'active', 'failed', 'retired')", name="ck_search_index_generations_status"),
        Index("uq_search_index_generations_active", "status", unique=True, postgresql_where=text("status = 'active'")),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    model_version: Mapped[str | None] = mapped_column(String(200))
    gateway_identity: Mapped[str | None] = mapped_column(String(64))
    response_model_id: Mapped[str | None] = mapped_column(String(200))
    dimensions: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="queued")
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class SearchIndexItem(Base):
    """Track embedding status and vector data for one chunk in a generation."""
    __tablename__ = "search_index_items"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'succeeded', 'failed')", name="ck_search_index_items_status"),
        UniqueConstraint("generation_id", "chunk_id", name="uq_search_index_items_generation_chunk"),
        Index("ix_search_index_items_status", "generation_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    generation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("search_index_generations.id", ondelete="CASCADE"), nullable=False)
    chunk_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("document_chunks.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    error_code: Mapped[str | None] = mapped_column(String(64))
    embedding: Mapped[str | None] = mapped_column(Vector())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
