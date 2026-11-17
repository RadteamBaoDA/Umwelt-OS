"""Durable singleton summary for bounded retention and temporary-data maintenance."""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Identity, Integer, SmallInteger, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


class MaintenanceSummary(Base):
    """Keep the latest maintenance counts and next eligible run time without growing a poll ledger."""

    __tablename__ = "maintenance_summary"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_maintenance_summary_singleton"),
        CheckConstraint(
            "agent_traces_redacted >= 0 AND temporary_data_deleted >= 0",
            name="ck_maintenance_summary_counts",
        ),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, server_default="1")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    agent_traces_redacted: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    temporary_data_deleted: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_eligible_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SystemHealthSample(Base):
    """One instance-wide health probe result; holds component statuses and latencies only, no personal data."""

    __tablename__ = "system_health_samples"
    __table_args__ = (
        CheckConstraint("queue_backlog >= 0", name="ck_system_health_samples_backlog"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    sampled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    overall: Mapped[str] = mapped_column(Text, nullable=False)
    components: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    queue_backlog: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    db_stats: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")


class SystemHistorySettings(Base):
    """Singleton retention policy for health samples, revision-fenced for compare-and-set updates."""

    __tablename__ = "system_history_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_system_history_settings_singleton"),
        CheckConstraint("retention_days BETWEEN 1 AND 90", name="ck_system_history_settings_days"),
        CheckConstraint(
            "sample_interval_minutes IN (0, 1, 5, 15, 30, 60)",
            name="ck_system_history_settings_interval",
        ),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, server_default="1")
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="7")
    sample_interval_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
