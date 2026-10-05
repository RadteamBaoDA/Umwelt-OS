"""Private SQLAlchemy persistence models for goal tracking and planning."""

from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from core.database import Base


class Goal(Base):
    """Persistence model representing a strategic owner goal.

    Tracks high-level objectives with milestones, related entity references, desired outcomes,
    deadline dates, and milestone-driven or manual progress tracking. Accepted proposal records
    retain the immutable content hash, task/milestone IDs, and first-acceptance revision for replay.
    """

    __tablename__ = "goals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'completed', 'paused', 'cancelled')",
            name="ck_goals_status",
        ),
        CheckConstraint(
            "progress BETWEEN 0.0 AND 100.0",
            name="ck_goals_progress",
        ),
        CheckConstraint(
            "revision BETWEEN 1 AND 9007199254740991",
            name="ck_goals_revision",
        ),
        CheckConstraint(
            "jsonb_typeof(milestones) = 'array'",
            name="ck_goals_milestones_array",
        ),
        CheckConstraint("jsonb_array_length(milestones) <= 100", name="ck_goals_milestones_bound"),
        CheckConstraint("jsonb_typeof(entity_ids) = 'array'", name="ck_goals_entity_ids_array"),
        CheckConstraint("jsonb_array_length(entity_ids) <= 100", name="ck_goals_entity_ids_bound"),
        CheckConstraint(
            "jsonb_typeof(accepted_proposals) = 'array'",
            name="ck_goals_accepted_proposals_array",
        ),
        CheckConstraint("jsonb_array_length(accepted_proposals) <= 1000", name="ck_goals_accepted_proposals_bound"),
        Index("ix_goals_owner_status", "owner_id", "status"),
        Index("ix_goals_deadline", "deadline"),
        Index("ix_goals_created_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("owner.id", ondelete="CASCADE"), nullable=False, default=1
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    desired_outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    deadline: Mapped[date | None] = mapped_column(Date, nullable=True)
    progress: Mapped[float] = mapped_column(Float, nullable=False, server_default="0.0")
    manual_progress: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="active")
    milestones: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    entity_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    accepted_proposals: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
