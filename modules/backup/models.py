"""Durable backup admission, operation, and activity records."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


class BackupControl(Base):
    """Singleton durable admission fence; phase changes never rely on process memory."""

    __tablename__ = "p12_backup_control"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_p12_backup_control_singleton"),
        CheckConstraint("epoch >= 1", name="ck_p12_backup_control_epoch"),
        CheckConstraint(
            "phase IN ('idle', 'draining', 'quiesced', 'snapshotting', 'resuming', 'failed_recovery_required')",
            name="ck_p12_backup_control_phase",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    phase: Mapped[str] = mapped_column(String(40), nullable=False, server_default="idle")
    operation_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    coordinator_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class BackupOperation(Base):
    """Owner-visible operation summary with redacted stage receipts and outcome."""

    __tablename__ = "p12_backup_operations"
    __table_args__ = (
        CheckConstraint("epoch >= 1", name="ck_p12_backup_operations_epoch"),
        CheckConstraint("completeness IN ('complete', 'incomplete')", name="ck_p12_backup_operations_completeness"),
        CheckConstraint(
            "status IN ('pending', 'draining', 'quiesced', 'snapshotting', 'resuming', 'completed', 'incomplete', 'failed', 'failed_recovery_required')",
            name="ck_p12_backup_operations_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, server_default="pending")
    consistency: Mapped[str] = mapped_column(String(32), nullable=False, server_default="quiesced")
    completeness: Mapped[str] = mapped_column(String(32), nullable=False, server_default="incomplete")
    archive_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stage_receipts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BackupActivity(Base):
    """Durable in-flight API or worker identity that the coordinator must drain."""

    __tablename__ = "p12_backup_activity"
    __table_args__ = (
        CheckConstraint("epoch >= 1", name="ck_p12_backup_activity_epoch"),
        CheckConstraint("state IN ('active', 'finished', 'interrupted', 'uncertain')", name="ck_p12_backup_activity_state"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    work_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
