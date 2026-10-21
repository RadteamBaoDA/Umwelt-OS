"""Durable deletion receipts contain no content and survive identity removal."""
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, CheckConstraint, DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


class AccountDeletionOperation(Base):
    """Bind an opaque receipt to original scope until erasure; retain only status afterward."""

    __tablename__ = "account_deletion_operations"
    __table_args__ = (
        CheckConstraint("status IN ('queued','running','failed','succeeded')", name="ck_account_deletion_status"),
        CheckConstraint("(status = 'succeeded' AND account_id IS NULL AND workspace_id IS NULL) OR "
                        "(status <> 'succeeded' AND account_id IS NOT NULL AND workspace_id IS NOT NULL)",
                        name="ck_account_deletion_scope"),
        Index("uq_account_deletion_receipt", "receipt_hash", unique=True),
        Index("uq_account_deletion_account", "account_id", unique=True),
        Index("ix_account_deletion_due", "status", "next_attempt_at"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    receipt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    account_id: Mapped[int | None] = mapped_column(Integer)
    workspace_id: Mapped[UUID | None] = mapped_column()
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    phase: Mapped[str] = mapped_column(String(64), default="queued", nullable=False)
    cursor: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
