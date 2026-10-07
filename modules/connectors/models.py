from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
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
    desired_configuration: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    workflow_id: Mapped[str | None] = mapped_column(String(128))
    workflow_name: Mapped[str | None] = mapped_column(String(255))
    desired_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    workflow_operation: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    activation_intent: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
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
    operation_envelope: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    resolved_binding: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
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


class ConnectorNativeCredential(Base):
    """Store native Telegram credentials and retain their verified source identity.

    Connector owners alone decrypt tokens, validate remote identity, fence writes,
    and release the active bot reservation. ``bound_bot_id`` survives revocation
    while ``verified_bot_id`` reserves an active bot; source deletion cascades the
    entire row. Persistence state alone does not certify current authentication.
    """
    __tablename__ = "connector_native_credentials"
    __table_args__ = (
        # Bot identity stays reserved across token rotation and paused or unresolved rows.
        UniqueConstraint("verified_bot_id", name="uq_connector_native_credentials_verified_bot"),
        CheckConstraint(
            "verified_bot_id IS NULL OR (bound_bot_id IS NOT NULL AND verified_bot_id = bound_bot_id)",
            name="ck_connector_native_credentials_verified_identity",
        ),
        CheckConstraint(
            "provider = 'telegram'",
            name="ck_connector_native_credentials_provider",
        ),
        CheckConstraint(
            "source_generation > 0 AND configuration_revision > 0",
            name="ck_connector_native_credentials_fences",
        ),
        CheckConstraint(
            "state IN ('pending', 'ready', 'revoked', 'reconciliation_required')",
            name="ck_connector_native_credentials_state",
        ),
        CheckConstraint(
            "state != 'ready' OR (encrypted_token IS NOT NULL AND token_fingerprint IS NOT NULL "
            "AND bound_bot_id IS NOT NULL AND verified_bot_id IS NOT NULL "
            "AND validated_at IS NOT NULL)",
            name="ck_connector_native_credentials_ready_binding",
        ),
        CheckConstraint(
            "state != 'revoked' OR (encrypted_token IS NULL AND token_fingerprint IS NULL)",
            name="ck_connector_native_credentials_revoked_secret",
        ),
    )

    source_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False, server_default="telegram")
    operation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    configuration_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    encrypted_token: Mapped[str | None] = mapped_column(Text)
    token_fingerprint: Mapped[str | None] = mapped_column(String(64))
    # Historical source binding survives secret removal; active uniqueness remains on verified_bot_id.
    bound_bot_id: Mapped[str | None] = mapped_column(String(20))
    verified_bot_id: Mapped[str | None] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(32), nullable=False, server_default="pending")
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ConnectorWorldCredential(Base):
    """Store an encrypted source-bound API key for an explicitly configured world provider."""
    __tablename__ = "connector_world_credentials"
    __table_args__ = (
        CheckConstraint("provider = 'alpha_vantage'", name="ck_connector_world_credentials_provider"),
        CheckConstraint("source_generation > 0 AND configuration_revision > 0", name="ck_connector_world_credentials_fences"),
    )

    source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    source_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    configuration_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    encrypted_key: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class GithubOAuthAttempt(Base):
    """Persist a single-use browser-bound GitHub PKCE attempt under source fences."""
    __tablename__ = "github_oauth_attempts"
    __table_args__ = (
        Index("ix_github_oauth_attempt_expiry", "expires_at"),
        CheckConstraint("source_generation > 0 AND configuration_revision >= 0", name="ck_github_oauth_attempt_fences"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False)
    session_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    browser_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_verifier: Mapped[str] = mapped_column(Text, nullable=False)
    source_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    configuration_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_token_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GithubOAuthGrant(Base):
    """Store one source binding to encrypted expiring GitHub App user tokens."""
    __tablename__ = "github_oauth_grants"
    __table_args__ = (
        Index("ix_github_oauth_grant_peer", "github_user_id", "state", "source_id"),
        CheckConstraint("source_generation > 0 AND configuration_revision > 0 AND token_revision > 0", name="ck_github_oauth_grant_fences"),
        CheckConstraint("binding_revision > 0", name="ck_github_oauth_grant_binding_revision"),
        CheckConstraint("state IN ('ready', 'refreshing', 'reconciliation_required', 'revoked')", name="ck_github_oauth_grant_state"),
    )

    source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True)
    operation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    github_user_id: Mapped[str] = mapped_column(String(20), nullable=False)
    repository_id: Mapped[str] = mapped_column(String(20), nullable=False)
    installation_id: Mapped[str | None] = mapped_column(String(20))
    app_id: Mapped[str | None] = mapped_column(String(20))
    binding_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    source_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    configuration_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    token_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    encrypted_tokens: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    refresh_operation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class GithubOAuthCoordinator(Base):
    """Serialize source grant binding and provider app/user-wide revocation for the local owner."""
    __tablename__ = "github_oauth_coordinators"
    __table_args__ = (
        CheckConstraint("state IN ('idle', 'authorizing', 'refreshing', 'revoking', 'reconciliation_required')", name="ck_github_oauth_coordinator_state"),
    )

    owner_id: Mapped[int] = mapped_column(Integer, ForeignKey("owner.id", ondelete="CASCADE"), primary_key=True)
    operation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32), nullable=False, server_default="idle")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class GithubSyncReset(Base):
    """Retain an owner-requested polling reset as a durable record of the history gap boundary."""
    __tablename__ = "github_sync_resets"
    __table_args__ = (
        Index("ix_github_sync_reset_source", "source_id", "reset_at"),
        CheckConstraint("source_generation > 0 AND connector_revision > 0", name="ck_github_sync_reset_revisions"),
        CheckConstraint("length(scope_sha256) = 64 AND scope_sha256 !~ '[^0-9a-f]'", name="ck_github_sync_reset_scope_digest"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False)
    source_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    connector_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    scope_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reset_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class GithubWebhookCapacity(Base):
    """Serialize global digest and pending-work slots transferred from outboxes to source hints."""
    __tablename__ = "github_webhook_capacity"
    __table_args__ = (
        CheckConstraint("id = 1 AND digest_count BETWEEN 0 AND 100000 AND pending_count BETWEEN 0 AND 100000", name="ck_github_webhook_capacity_bounds"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    digest_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    pending_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class GithubWebhookDelivery(Base):
    """Retain a replay-safe minimal delivery digest and extracted bounded targets."""
    __tablename__ = "github_webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("receiver_revision", "delivery_id", name="uq_github_webhook_delivery_namespace_id"),
        CheckConstraint("length(raw_sha256) = 64 AND raw_sha256 !~ '[^0-9a-f]'", name="ck_github_webhook_delivery_digest"),
        CheckConstraint("length(delivery_id) BETWEEN 1 AND 128 AND jsonb_array_length(targets) <= 100", name="ck_github_webhook_delivery_bounds"),
        CheckConstraint("disposition IN ('received', 'ignored', 'ping')", name="ck_github_webhook_delivery_disposition"),
        Index("ix_github_webhook_delivery_retention", "detail_expires_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    receiver_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery_id: Mapped[str] = mapped_column(String(128), nullable=False)
    raw_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str | None] = mapped_column(String(64))
    app_id: Mapped[str | None] = mapped_column(String(20))
    installation_id: Mapped[str | None] = mapped_column(String(20))
    repository_id: Mapped[str | None] = mapped_column(String(20))
    targets: Mapped[list[dict[str, str]]] = mapped_column(JSONB, nullable=False, server_default="[]")
    disposition: Mapped[str] = mapped_column(String(16), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    detail_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    details_scrubbed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GithubWebhookOutbox(Base):
    """Track restart-safe binding fanout, page-local admission identity, and packaged wake delivery."""
    __tablename__ = "github_webhook_outbox"
    __table_args__ = (
        ForeignKeyConstraint(["delivery_id"], ["github_webhook_deliveries.id"], ondelete="CASCADE"),
        CheckConstraint("state IN ('pending', 'dispatched', 'needs_attention', 'complete')", name="ck_github_webhook_outbox_state"),
        CheckConstraint("attempts BETWEEN 0 AND 5", name="ck_github_webhook_outbox_attempts"),
        CheckConstraint("fanout_page IS NULL OR CASE WHEN jsonb_typeof(fanout_page) = 'object' AND jsonb_typeof(fanout_page -> 'bindings') = 'array' AND jsonb_typeof(fanout_page -> 'admissions') = 'array' THEN jsonb_array_length(fanout_page -> 'bindings') <= 50 AND jsonb_array_length(fanout_page -> 'admissions') <= 5000 AND octet_length(fanout_page::text) <= 2097152 ELSE false END", name="ck_github_webhook_outbox_fanout_page_bounds"),
        Index("ix_github_webhook_outbox_due", "state", "next_attempt_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    delivery_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, unique=True)
    binding_cursor: Mapped[str | None] = mapped_column(String(512))
    # Nonsecret frozen bindings and source/target admissions survive retries until cursor CAS succeeds.
    fanout_page: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    capacity_reserved: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    state: Mapped[str] = mapped_column(String(24), nullable=False, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class GithubSourceHint(Base):
    """Coalesce provider invalidations per source target while preserving dirty revisions and exact claims."""
    __tablename__ = "github_source_hints"
    __table_args__ = (
        UniqueConstraint("source_id", "resource", "locator_kind", "locator", name="uq_github_source_hint_target"),
        CheckConstraint("dirty_revision > 0 AND attempts BETWEEN 0 AND 5", name="ck_github_source_hint_revision_attempts"),
        CheckConstraint("state IN ('pending', 'dispatched', 'accepted_ingestion', 'completed', 'ignored', 'paused', 'visibility_unverified', 'needs_attention', 'capacity_deferred')", name="ck_github_source_hint_state"),
        Index("ix_github_source_hint_due", "state", "next_attempt_at"),
        Index("ix_github_source_hint_source", "source_id", "state", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False)
    source_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    connector_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    repository_id: Mapped[str] = mapped_column(String(20), nullable=False)
    binding_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    resource: Mapped[str] = mapped_column(String(16), nullable=False)
    locator_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    locator: Mapped[str] = mapped_column(String(256), nullable=False)
    intent: Mapped[str] = mapped_column(String(24), nullable=False)
    dirty_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    claimed_revision: Mapped[int | None] = mapped_column(Integer)
    claim_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_delivery_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    acknowledged_batch_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    state: Mapped[str] = mapped_column(String(32), nullable=False, server_default="pending")
    capacity_reserved: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    reconcile_page: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class GithubOAuthOperation(Base):
    """Retain owner-visible OAuth uncertainty after its source or grant rows are deleted."""
    __tablename__ = "github_oauth_operations"
    __table_args__ = (
        CheckConstraint("operation_kind IN ('authorization', 'refresh', 'revoke')", name="ck_github_oauth_operation_kind"),
        CheckConstraint("state IN ('in_progress', 'reconciliation_required', 'review_required', 'completed', 'acknowledged')", name="ck_github_oauth_operation_state"),
        CheckConstraint("source_generation IS NULL OR source_generation > 0", name="ck_github_oauth_operation_generation"),
        CheckConstraint("configuration_revision IS NULL OR configuration_revision >= 0", name="ck_github_oauth_operation_revision"),
        CheckConstraint("token_revision IS NULL OR token_revision >= 0", name="ck_github_oauth_operation_token_revision"),
        CheckConstraint("jsonb_array_length(peer_inventory) <= 100", name="ck_github_oauth_operation_peer_bound"),
        Index("ix_github_oauth_operation_owner_state", "owner_id", "state", "created_at"),
    )

    # The source and owner IDs intentionally have no cascading foreign keys: this row is the
    # durable uncertainty marker when lifecycle cleanup deletes its source or grant.
    operation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    source_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    source_generation: Mapped[int | None] = mapped_column(Integer)
    configuration_revision: Mapped[int | None] = mapped_column(Integer)
    token_revision: Mapped[int | None] = mapped_column(Integer)
    peer_inventory: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    state: Mapped[str] = mapped_column(String(32), nullable=False, server_default="in_progress")
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
