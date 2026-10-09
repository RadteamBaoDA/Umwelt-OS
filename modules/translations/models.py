"""Derived translation cache, batches and batch items; never source-of-truth content.

Rows are actor- and workspace-bound. Reserved T3 columns (lease, slot, attempts, retention) are
written only by the T3 worker; T1 creates rows ``pending`` and reads them.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from core.database import Base

RETENTION_DAYS = 30
STATUSES = ("pending", "ready", "unchanged", "blocked", "failed")
RESOURCE_TYPES = ("news_story", "daily_brief")
_STATUS_SQL = ", ".join(f"'{value}'" for value in STATUSES)
_RESOURCE_SQL = ", ".join(f"'{value}'" for value in RESOURCE_TYPES)


def _expiry() -> datetime:
    """Default retention horizon for derived rows."""
    return datetime.now(UTC) + timedelta(days=RETENTION_DAYS)


class ContentTranslation(Base):
    """Cached translation of one resource revision for one actor under one full fingerprint."""

    __tablename__ = "content_translations"
    __table_args__ = (
        CheckConstraint(f"status IN ({_STATUS_SQL})", name="ck_content_translations_status"),
        CheckConstraint(f"resource_type IN ({_RESOURCE_SQL})", name="ck_content_translations_resource_type"),
        CheckConstraint("target_language IN ('vi', 'en')", name="ck_content_translations_target"),
        CheckConstraint("attempt_count >= 0", name="ck_content_translations_attempts"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_content_translations_workspace", ondelete="CASCADE"),
        UniqueConstraint(
            "workspace_id", "actor_user_id", "resource_type", "resource_id", "resource_revision",
            "content_hash", "visibility_hash", "target_language", "config_hash", "prompt_version",
            name="uq_content_translations_fingerprint",
        ),
        Index("ix_content_translations_scope", "workspace_id", "status", "created_at"),
        Index("ix_content_translations_expiry", "expires_at"),
        Index("ix_content_translations_lease", "status", "next_attempt_at", "lease_expires_at"),
        Index("ix_content_translations_resource", "workspace_id", "resource_type", "resource_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    actor_user_id: Mapped[int] = mapped_column(ForeignKey("owner.id", ondelete="CASCADE"), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(16), nullable=False)
    resource_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    resource_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    visibility_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    target_language: Mapped[str] = mapped_column(String(2), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(64))
    # T3-reserved: durable work lease, global translation slot, attempt budget, next retry.
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    lease_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    slot_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    slot_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_expiry)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class TranslationBatch(Base):
    """One actor's request; binds target language and the settings revision it was admitted under."""

    __tablename__ = "translation_batches"
    __table_args__ = (
        CheckConstraint("target_language IN ('vi', 'en')", name="ck_translation_batches_target"),
        CheckConstraint("settings_revision > 0", name="ck_translation_batches_revision"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_translation_batches_workspace", ondelete="CASCADE"),
        Index("ix_translation_batches_actor", "workspace_id", "actor_user_id", "created_at"),
        Index("ix_translation_batches_expiry", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    actor_user_id: Mapped[int] = mapped_column(ForeignKey("owner.id", ondelete="CASCADE"), nullable=False)
    target_language: Mapped[str] = mapped_column(String(2), nullable=False)
    settings_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_expiry)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class TranslationBatchItem(Base):
    """Reference (never client text) from a batch to the resource revision and its cache row."""

    __tablename__ = "translation_batch_items"
    __table_args__ = (
        CheckConstraint(f"resource_type IN ({_RESOURCE_SQL})", name="ck_translation_batch_items_resource_type"),
        ForeignKeyConstraint(["batch_id"], ["translation_batches.id"], name="fk_translation_batch_items_batch", ondelete="CASCADE"),
        ForeignKeyConstraint(["translation_id"], ["content_translations.id"], name="fk_translation_batch_items_translation", ondelete="SET NULL"),
        UniqueConstraint("batch_id", "resource_type", "resource_id", name="uq_translation_batch_items_ref"),
        Index("ix_translation_batch_items_translation", "translation_id"),
    )

    batch_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(16), nullable=False)
    resource_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    resource_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    translation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class TranslationAdmissionSlot(Base):
    """The one global translation slot (id = 1); a fencing token makes a stale holder's renewal fail."""

    __tablename__ = "translation_admission_slots"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_translation_admission_slots_singleton"),
        CheckConstraint("fencing_token >= 0", name="ck_translation_admission_slots_token"),
        CheckConstraint("(translation_id IS NULL) = (expires_at IS NULL)", name="ck_translation_admission_slots_holder"),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    translation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
