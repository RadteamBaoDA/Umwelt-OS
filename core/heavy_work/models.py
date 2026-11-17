from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from core.database import Base


class RemoteHeavyGuard(Base):
    """Persist uncertainty while a bounded remote heavy operation may still run."""

    __tablename__ = "remote_heavy_guards"
    __table_args__ = (
        CheckConstraint(
            "state IN ('active', 'uncertain', 'cleared')",
            name="ck_remote_heavy_guards_state",
        ),
        Index("ix_remote_heavy_guards_blocking", "state", "created_at"),
    )

    operation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    service_instance_id: Mapped[str] = mapped_column(String(128), nullable=False)
    remote_job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    nonce_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
