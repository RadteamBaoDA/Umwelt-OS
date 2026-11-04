"""Transactional outbox table mapping (``event_outbox``), owned by ``core.events``.

The table name, columns, constraints and indexes are unchanged from when the mapping lived in
``modules.ingestion.models`` (no migration); that module re-exports ``EventOutbox`` for compatibility.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from core.database import Base
from core.ids import uuid7


class EventOutbox(Base):
    """Persist domain events until queued delivery succeeds or is terminally failed.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    The owned-default actor and captured membership revision retain delivery authorization
    context; runtime dispatch must revalidate this epoch against current membership.
    """
    __tablename__ = "event_outbox"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'queued', 'delivered', 'failed')", name="ck_event_outbox_status"),
        CheckConstraint("membership_revision > 0", name="ck_w2_event_outbox_membership_revision_positive"),
        Index("ix_event_outbox_dispatch", "status", "dispatched_at"),
        Index("ix_event_outbox_delivered_created", "created_at", postgresql_where=text("status = 'delivered'")),
        Index(
            "ix_event_outbox_dispatch_created", "created_at", "id",
            postgresql_where=text("status IN ('pending', 'queued')"),
        ),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_event_outbox_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "actor_user_id"], ['workspaces.id', 'workspaces.owner_user_id'], name="fk_w2_event_outbox_principal", ondelete="RESTRICT"),
        Index("ix_w2_event_outbox_scope", 'workspace_id', 'id'),
        Index("ix_w2_event_outbox_work", 'workspace_id', 'status', 'next_attempt_at', 'id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    actor_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    membership_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)


    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid7)
    type: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    producer: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
