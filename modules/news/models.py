"""Private durable records for story groups and source-specific observations."""

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


class NewsStory(Base):
    """Store a deterministic news identity and versioned derivation metadata."""

    __tablename__ = "news_stories"
    __table_args__ = (
        UniqueConstraint("identity_key", "algorithm_version", name="uq_news_stories_identity_algorithm"),
        CheckConstraint("identity_kind IN ('url', 'hash')", name="ck_news_stories_identity_kind"),
        CheckConstraint("algorithm_version >= 1", name="ck_news_stories_algorithm_version"),
        Index("ix_news_stories_created", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    identity_key: Mapped[str] = mapped_column(String(512), nullable=False)
    identity_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    algorithm_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class NewsStoryIdentity(Base):
    """Map every exact URL/hash identity to its deterministic story group."""

    __tablename__ = "news_story_identities"
    __table_args__ = (
        UniqueConstraint("identity_key", "algorithm_version", name="uq_news_story_identities_key_algorithm"),
        CheckConstraint("identity_kind IN ('url', 'hash')", name="ck_news_story_identities_identity_kind"),
        Index("ix_news_story_identities_story", "story_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    story_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("news_stories.id", ondelete="CASCADE"), nullable=False)
    identity_key: Mapped[str] = mapped_column(String(512), nullable=False)
    identity_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    algorithm_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class NewsObservation(Base):
    """Retain one independently sourced item/version supporting a story."""

    __tablename__ = "news_observations"
    __table_args__ = (
        UniqueConstraint("document_version_id", "source_generation", "algorithm_version", name="uq_news_observations_version_generation_algorithm"),
        Index("ix_news_observations_story_time", "story_id", "observed_at"),
        Index("ix_news_observations_source_time", "source_id", "observed_at"),
        CheckConstraint("source_generation >= 0 AND version_number >= 1", name="ck_news_observations_generation_version"),
        CheckConstraint("match_method IN ('url', 'hash', 'embedding_entity_time')", name="ck_news_observations_match_method"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    story_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("news_stories.id", ondelete="CASCADE"), nullable=False)
    document_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    document_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False)
    chunk_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("document_chunks.id", ondelete="CASCADE"), nullable=False)
    source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False)
    source_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    excerpt: Mapped[str] = mapped_column(String(1000), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(128))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    local_only: Mapped[bool] = mapped_column(nullable=False)
    membership_entity_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    match_method: Mapped[str] = mapped_column(String(32), nullable=False)
    incomplete_reason: Mapped[str | None] = mapped_column(String(64))
    match_evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    recorded_signals: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    algorithm_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class NewsRecoveryCheckpoint(Base):
    """Persist source/document keyset progress for finite legacy News catch-up."""

    __tablename__ = "news_recovery_checkpoints"
    __table_args__ = (CheckConstraint("id = 1", name="ck_news_recovery_checkpoint_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    source_cursor: Mapped[str | None] = mapped_column(String(512))
    document_cursor: Mapped[str | None] = mapped_column(String(512))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
