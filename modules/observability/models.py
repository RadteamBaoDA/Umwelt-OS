"""Durable singleton summary for bounded retention and temporary-data maintenance."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, SmallInteger
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


class MaintenanceSummary(Base):
    """Keep the latest maintenance counts and next eligible run time without growing a poll ledger."""

    __tablename__ = "maintenance_summary"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_maintenance_summary_singleton"),
        CheckConstraint("agent_traces_redacted >= 0 AND temporary_data_deleted >= 0", name="ck_maintenance_summary_counts"),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, server_default="1")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    agent_traces_redacted: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    temporary_data_deleted: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_eligible_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
