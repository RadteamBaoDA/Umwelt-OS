"""PostgreSQL persistence for immutable structured observation revisions."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from core.database import Base


class Observation(Base):
    """Persist one immutable measurement revision tied to its source and evidence version."""
    __tablename__ = "observations"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", "revision", name="uq_observations_series_revision"),
        UniqueConstraint("source_id", "external_id", "ingestion_observation_id", name="uq_observations_ingestion_acceptance"),
        Index("ix_observations_owner_series_current", "source_id", "provider", "metric", "symbol", "region", "is_current", "observed_at", "id"),
        Index("ix_observations_document_version", "document_version_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False)
    external_id: Mapped[str] = mapped_column(String(512), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_scope_discriminator: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_version: Mapped[str | None] = mapped_column(String(255))
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    metric: Mapped[str] = mapped_column(String(80), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(40))
    region: Mapped[str | None] = mapped_column(String(80))
    latitude: Mapped[float | None] = mapped_column()
    longitude: Mapped[float | None] = mapped_column()
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    value: Mapped[float | None] = mapped_column(nullable=True)
    unit: Mapped[str] = mapped_column(String(64), nullable=False)
    currency: Mapped[str | None] = mapped_column(String(3))
    timezone: Mapped[str | None] = mapped_column(String(64))
    quality: Mapped[str] = mapped_column(String(32), nullable=False)
    missing_reason: Mapped[str | None] = mapped_column(String(64))
    provider_delay_seconds: Mapped[int | None] = mapped_column(Integer)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    document_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    document_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False)
    ingestion_observation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("source_observations.id", ondelete="CASCADE"), nullable=False)
    source_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
