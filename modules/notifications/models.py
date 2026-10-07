"""Private SQLAlchemy persistence model for owner notifications."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from core.database import Base


class Notification(Base):
    """One recipient/workspace notification, deduplicated by workspace, recipient and key.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("workspace_id", "owner_id", "dedupe_key", name="uq_notifications_dedupe"),
        Index("ix_notifications_owner_created", "owner_id", "created_at"),
        Index("ix_notifications_copied_document", "document_id", "id"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_notifications_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "owner_id"], ['workspace_memberships.workspace_id', 'workspace_memberships.user_id'], name="fk_w2_notifications_principal", ondelete="RESTRICT"),
        Index("ix_w2_notifications_scope", 'workspace_id', 'id'),
        Index("ix_w2_notifications_work", 'workspace_id', 'created_at', 'id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[int] = mapped_column(ForeignKey("owner.id", ondelete="CASCADE"), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    # Legacy display fallback only; new rows carry just ``kind`` + ``params`` and the UI localizes.
    title: Mapped[str | None] = mapped_column(String(300))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    body: Mapped[str | None] = mapped_column(String(2000))
    link: Mapped[str | None] = mapped_column(String(300))
    # Private sidecar: copied dashboard titles must remain addressable after Document cascades.
    document_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    document_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    copied_evidence_revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

