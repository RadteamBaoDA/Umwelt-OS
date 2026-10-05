from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from core.database import Base


class ConnectorProvisioning(Base):
    """Persist desired and applied connector configuration and workflow operation state."""
    __tablename__ = "connector_provisioning"
    __table_args__ = (
        Index("ix_connector_provisioning_reconcile", "state", "updated_at"),
        Index("ix_connector_provisioning_desired_enabled", "desired_enabled", "updated_at"),
        CheckConstraint(
            "state IN ('queued', 'provisioning', 'active', 'saved_not_active', 'reconciliation_required', 'disabled')",
            name="ck_connector_provisioning_state",
        ),
        CheckConstraint(
            "desired_revision > 0 AND applied_revision >= 0",
            name="ck_connector_provisioning_revisions",
        ),
    )

    source_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    source_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    desired_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    applied_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    desired_configuration: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    workflow_id: Mapped[str | None] = mapped_column(String(128))
    workflow_name: Mapped[str | None] = mapped_column(String(255))
    desired_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    workflow_operation: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    activation_intent: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(32), nullable=False, server_default="queued")
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ConnectorManagedCredential(Base):
    """Track one source credential slot through create, recovery, and deletion."""
    __tablename__ = "connector_managed_credentials"
    __table_args__ = (
        Index("ix_connector_managed_credentials_state", "state"),
        CheckConstraint(
            "state IN ('queued', 'dispatching', 'ready', 'reconciliation_required', 'delete_pending')",
            name="ck_connector_managed_credential_state",
        ),
    )

    source_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    slot: Mapped[str] = mapped_column(String(64), primary_key=True)
    credential_id: Mapped[str | None] = mapped_column(String(128))
    operation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    operation_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    source_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    credential_type: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, server_default="queued")
    error_code: Mapped[str | None] = mapped_column(String(64))
    operation_envelope: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    resolved_binding: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AgentBrowserGrant(Base):
    """Persist explicit owner opt-in for static reads from one credential-free web source."""

    __tablename__ = "agent_browser_grants"
    __table_args__ = (
        CheckConstraint("owner_id = 1", name="ck_agent_browser_grants_single_owner"),
        CheckConstraint("source_generation > 0 AND connector_revision > 0", name="ck_agent_browser_grants_fences"),
        CheckConstraint("grant_revision > 0", name="ck_agent_browser_grants_revision"),
        Index("ix_agent_browser_grants_owner_enabled", "owner_id", "enabled"),
    )

    source_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    connector_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    grant_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    scope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    origin: Mapped[str] = mapped_column(String(512), nullable=False)
    path_prefix: Mapped[str] = mapped_column(String(2048), nullable=False)
    local_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
