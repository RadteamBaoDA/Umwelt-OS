"""Private durable records for story groups and source-specific observations."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
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
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from core.database import Base


class NewsStory(Base):
    """Store a deterministic news identity and versioned derivation metadata.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """

    __tablename__ = "news_stories"
    __table_args__ = (
        UniqueConstraint("workspace_id", "identity_key", "algorithm_version", name="uq_news_stories_identity_algorithm"),
        CheckConstraint("identity_kind IN ('url', 'hash')", name="ck_news_stories_identity_kind"),
        CheckConstraint("algorithm_version >= 1", name="ck_news_stories_algorithm_version"),
        Index("ix_news_stories_created", "created_at", "id"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_news_stories_workspace", ondelete="RESTRICT"),
        UniqueConstraint("workspace_id", "id", name="uq_w2_news_stories_id"),
        Index("ix_w2_news_stories_scope", 'workspace_id', 'id'),
        Index("ix_w2_news_stories_work", 'workspace_id', 'created_at', 'id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    identity_key: Mapped[str] = mapped_column(String(512), nullable=False)
    identity_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    algorithm_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())



class NewsStoryIdentity(Base):
    """Map every exact URL/hash identity to its deterministic story group.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """

    __tablename__ = "news_story_identities"
    __table_args__ = (
        UniqueConstraint("workspace_id", "identity_key", "algorithm_version", name="uq_news_story_identities_key_algorithm"),
        CheckConstraint("identity_kind IN ('url', 'hash')", name="ck_news_story_identities_identity_kind"),
        Index("ix_news_story_identities_story", "story_id"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_news_story_identities_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "story_id"], ["news_stories.workspace_id", "news_stories.id"], name="fk_w2_news_story_identities_story_id", ondelete="CASCADE"),
        Index("ix_w2_news_story_identities_scope", 'workspace_id', 'id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    story_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    identity_key: Mapped[str] = mapped_column(String(512), nullable=False)
    identity_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    algorithm_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")



class NewsObservation(Base):
    """Retain one independently sourced item/version supporting a story.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """

    __tablename__ = "news_observations"
    __table_args__ = (
        UniqueConstraint("document_version_id", "source_generation", "algorithm_version", name="uq_news_observations_version_generation_algorithm"),
        Index("ix_news_observations_story_time", "story_id", "observed_at"),
        Index("ix_news_observations_source_time", "source_id", "observed_at"),
        CheckConstraint("source_generation >= 0 AND version_number >= 1", name="ck_news_observations_generation_version"),
        CheckConstraint("match_method IN ('url', 'hash', 'embedding_entity_time')", name="ck_news_observations_match_method"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_news_observations_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "story_id"], ["news_stories.workspace_id", "news_stories.id"], name="fk_w2_news_observations_story_id", ondelete="CASCADE"),
        ForeignKeyConstraint(["workspace_id", "document_id"], ["documents.workspace_id", "documents.id"], name="fk_w2_news_observations_document_id", ondelete="CASCADE"),
        ForeignKeyConstraint(["workspace_id", "source_id"], ["sources.workspace_id", "sources.id"], name="fk_w2_news_observations_source_id", ondelete="CASCADE"),
        Index("ix_w2_news_observations_scope", 'workspace_id', 'id'),
        Index("ix_w2_news_observations_work", 'workspace_id', 'created_at', 'id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    story_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    document_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    document_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False)
    chunk_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("document_chunks.id", ondelete="CASCADE"), nullable=False)
    source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
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
    """Persist source/document keyset progress for finite legacy News catch-up.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """

    __tablename__ = "news_recovery_checkpoints"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_news_recovery_checkpoints_workspace", ondelete="RESTRICT"),
        Index("ix_w2_news_recovery_checkpoints_work", "workspace_id", "updated_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)


    source_cursor: Mapped[str | None] = mapped_column(String(512))
    document_cursor: Mapped[str | None] = mapped_column(String(512))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

