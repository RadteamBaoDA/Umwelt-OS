"""Private SQLAlchemy persistence models for task management."""

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from core.database import Base


class Task(Base):
    """Persistence model representing an actionable owner task.

    Tracks execution lifecycle across inbox, todo, in_progress, blocked, done, and cancelled,
    supporting date-only or instant deadlines, optimistic revisions, and a deletion tombstone so
    accepted proposal identities remain replayable after a task leaves normal views.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """

    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('inbox', 'todo', 'in_progress', 'blocked', 'done', 'cancelled')",
            name="ck_tasks_status",
        ),
        CheckConstraint(
            "revision BETWEEN 1 AND 9007199254740991",
            name="ck_tasks_revision",
        ),
        CheckConstraint("due_date IS NULL OR due_at IS NULL", name="ck_tasks_one_due_kind"),
        CheckConstraint("(status = 'done') = (completed_at IS NOT NULL)", name="ck_tasks_completion_timestamp"),
        CheckConstraint(
            "jsonb_typeof(entity_ids) = 'array'",
            name="ck_tasks_entity_ids_array",
        ),
        CheckConstraint("jsonb_array_length(entity_ids) <= 100", name="ck_tasks_entity_ids_bound"),
        Index("ix_tasks_owner_status", "owner_id", "status"),
        Index("ix_tasks_owner_due_date", "owner_id", "due_date"),
        Index("ix_tasks_owner_due_at", "owner_id", "due_at"),
        Index("ix_tasks_goal_id", "goal_id"),
        Index("ix_tasks_created_at", "created_at"),
        Index("ix_tasks_owner_deleted_at", "owner_id", "deleted_at"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_tasks_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "owner_id"], ['workspaces.id', 'workspaces.owner_user_id'], name="fk_w2_tasks_principal", ondelete="RESTRICT"),
        # Scalar SET NULL clears only the parent ID; this deferred FK retains workspace.
        ForeignKeyConstraint(["workspace_id", "goal_id"], ["goals.workspace_id", "goals.id"], name="fk_w2_tasks_goal_id", ondelete="NO ACTION", deferrable=True, initially="DEFERRED"),
        Index("ix_w2_tasks_scope", 'workspace_id', 'id'),
        Index("ix_w2_tasks_work", 'workspace_id', 'created_at', 'id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("owner.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="inbox")
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    goal_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("goals.id", ondelete="SET NULL"), nullable=True
    )
    entity_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

