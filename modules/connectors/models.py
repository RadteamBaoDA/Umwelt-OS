from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
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
        CheckConstraint("execution_backend IN ('native', 'n8n')", name="ck_connector_provisioning_backend"),
        CheckConstraint("backend_revision > 0", name="ck_connector_provisioning_backend_revision"),
        CheckConstraint(
            "transition_phase IN ('idle', 'draining', 'deactivating_old', 'activating_new', 'reconciliation_required')",
            name="ck_connector_provisioning_transition_phase",
        ),
        CheckConstraint("target_backend IS NULL OR target_backend IN ('native', 'n8n')", name="ck_connector_provisioning_target_backend"),
        CheckConstraint(
            "applied_backend_revision >= 0 AND template_revision >= 0 AND applied_template_revision >= 0 AND credential_revision > 0",
            name="ck_connector_provisioning_c4_revisions",
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
    # Legacy rows keep n8n; C4 owns transitions and bumps backend_revision to fence old executions.
    execution_backend: Mapped[str] = mapped_column(String(16), nullable=False, server_default="n8n")
    backend_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    # C4: admission needs idle + applied_backend_revision == backend_revision (see backends.backend_admits).
    applied_backend_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    target_backend: Mapped[str | None] = mapped_column(String(16))
    transition_phase: Mapped[str] = mapped_column(String(24), nullable=False, server_default="idle")
    transition_operation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    old_workflow_id: Mapped[str | None] = mapped_column(String(128))
    template_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    applied_template_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Bumped for any credential binding/secret/operation replacement; blocked schedules reopen on it.
    credential_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
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
    """Persist explicit owner opt-in for static reads from one credential-free web source.

    Account or Source lineage replaces the legacy bootstrap-only owner restriction.
    """

    __tablename__ = "agent_browser_grants"
    __table_args__ = (
        CheckConstraint("source_generation > 0 AND connector_revision > 0", name="ck_agent_browser_grants_fences"),
        CheckConstraint("grant_revision > 0", name="ck_agent_browser_grants_revision"),
        Index("ix_agent_browser_grants_owner_enabled", "owner_id", "enabled"),
    )

    source_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
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


class ConnectorRestCredential(Base):
    """Encrypted header secret for a native REST source; written only by owner re-entry, never echoed."""
    __tablename__ = "connector_rest_credentials"
    __table_args__ = (
        CheckConstraint("source_generation > 0 AND configuration_revision > 0", name="ck_connector_rest_credentials_fences"),
        CheckConstraint("state IN ('ready', 'revoked')", name="ck_connector_rest_credentials_state"),
    )

    source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True)
    source_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    configuration_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    header_name: Mapped[str] = mapped_column(String(128), nullable=False)
    encrypted_secret: Mapped[str | None] = mapped_column(Text)
    secret_fingerprint: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="ready")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


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
    """Retain owner-visible OAuth uncertainty after its source or grant rows are deleted.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """
    __tablename__ = "github_oauth_operations"
    __table_args__ = (
        CheckConstraint("operation_kind IN ('authorization', 'refresh', 'revoke')", name="ck_github_oauth_operation_kind"),
        CheckConstraint("state IN ('in_progress', 'reconciliation_required', 'review_required', 'completed', 'acknowledged')", name="ck_github_oauth_operation_state"),
        CheckConstraint("source_generation IS NULL OR source_generation > 0", name="ck_github_oauth_operation_generation"),
        CheckConstraint("configuration_revision IS NULL OR configuration_revision >= 0", name="ck_github_oauth_operation_revision"),
        CheckConstraint("token_revision IS NULL OR token_revision >= 0", name="ck_github_oauth_operation_token_revision"),
        CheckConstraint("jsonb_array_length(peer_inventory) <= 100", name="ck_github_oauth_operation_peer_bound"),
        Index("ix_github_oauth_operation_owner_state", "owner_id", "state", "created_at"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_github_oauth_operations_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "owner_id"], ['workspaces.id', 'workspaces.owner_user_id'], name="fk_w2_github_oauth_operations_principal", ondelete="RESTRICT"),
        Index("ix_w2_github_oauth_operations_scope", 'workspace_id', 'operation_id'),
        Index("ix_w2_github_oauth_operations_work", 'workspace_id', 'created_at', 'operation_id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


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



class ConnectorSchedule(Base):
    """Persist one source's regular collection cadence; PostgreSQL is the only schedule owner."""
    __tablename__ = "connector_schedules"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_connector_schedules_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "source_id"], ["sources.workspace_id", "sources.id"],
            name="fk_connector_schedules_source", ondelete="CASCADE",
        ),
        CheckConstraint("interval_minutes IN (15, 30, 60, 360, 1440)", name="ck_connector_schedules_interval"),
        CheckConstraint("failure_count >= 0", name="ck_connector_schedules_failures"),
        Index("ix_connector_schedules_due", "enabled", "next_due_at"),
        Index("ix_connector_schedules_workspace", "workspace_id", "last_considered_at"),
        Index("ix_connector_schedules_eligible", "enabled", "next_eligible_at", "next_due_at"),
    )

    source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    next_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_dispatch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_considered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_eligible_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when credential/schema/terms failures need owner action; gates manual and scheduled calls.
    blocked_error_code: Mapped[str | None] = mapped_column(String(64))
    # Failed dimensions (config/credential/terms) and the revision each failed at; see clear_collection_block.
    blocked_dimensions: Mapped[list[str] | None] = mapped_column(ARRAY(String(16)))
    blocked_connector_revision: Mapped[int | None] = mapped_column(Integer)
    blocked_credential_revision: Mapped[int | None] = mapped_column(Integer)
    blocked_terms_revision: Mapped[int | None] = mapped_column(Integer)


class ConnectorCollectionRequest(Base):
    """Persist one durable collection request with its captured fences; Redis carries only its id."""
    __tablename__ = "connector_collection_requests"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_connector_collection_requests_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "source_id"], ["sources.workspace_id", "sources.id"],
            name="fk_connector_collection_requests_source", ondelete="CASCADE",
        ),
        CheckConstraint("trigger IN ('manual', 'scheduled', 'retry')", name="ck_connector_collection_requests_trigger"),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'no_changes', 'failed', 'cancelled')",
            name="ck_connector_collection_requests_status",
        ),
        CheckConstraint("captured_backend IN ('native', 'n8n')", name="ck_connector_collection_requests_backend"),
        CheckConstraint("attempt BETWEEN 0 AND 5", name="ck_connector_collection_requests_attempt"),
        CheckConstraint(
            "(status = 'running') = (active_admission_token IS NOT NULL)",
            name="ck_connector_collection_requests_admission",
        ),
        Index(
            "uq_connector_collection_requests_active", "source_id", unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        Index(
            "ix_connector_collection_requests_enqueue", "available_at", "enqueue_next_at",
            postgresql_where=text("status = 'queued'"),
        ),
        Index("ix_connector_collection_requests_workspace", "workspace_id", "source_id", "created_at"),
        Index(
            "uq_connector_collection_requests_receipt", "accepted_receipt_id", unique=True,
            postgresql_where=text("accepted_receipt_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    actor_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    membership_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    source_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    connector_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    backend_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    captured_backend: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="queued")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    active_admission_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    # Reserved by collection-protocol for C3/C4/P1; C2 only stores them.
    access_configuration_revision: Mapped[int | None] = mapped_column(Integer)
    template_revision: Mapped[int | None] = mapped_column(Integer)
    credential_revision: Mapped[int | None] = mapped_column(Integer)
    terms_revision: Mapped[int | None] = mapped_column(Integer)
    source_lease_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    attempt_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_receipt_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    wake_next_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    wake_claim_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    wake_claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    wake_attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    ingestion_run_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    provider_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enqueue_next_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    enqueue_claim_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    enqueue_claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ConnectorAdmissionSlot(Base):
    """Seeded global network-capacity slots 1 and 2; the token fences each admitted attempt.

    Occupancy has no request FK on purpose: an expired row stays occupied until fenced cleanup
    clears it, even if its request was cascaded away with its Source.
    """
    __tablename__ = "connector_admission_slots"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_connector_admission_slots_workspace", ondelete="RESTRICT"),
        CheckConstraint("slot_id IN (1, 2)", name="ck_connector_admission_slots_id"),
        CheckConstraint("lease_kind IN ('collection', 'run')", name="ck_connector_admission_slots_lease_kind"),
        CheckConstraint(
            "(occupied_request_id IS NULL) = (lease_kind IS NULL) "
            "AND (occupied_request_id IS NULL) = (workspace_id IS NULL) "
            "AND (occupied_request_id IS NULL) = (admission_token IS NULL) "
            "AND (occupied_request_id IS NULL) = (expires_at IS NULL)",
            name="ck_connector_admission_slots_occupancy",
        ),
        Index(
            "uq_connector_admission_slots_request", "occupied_request_id", unique=True,
            postgresql_where=text("occupied_request_id IS NOT NULL"),
        ),
        Index(
            "uq_connector_admission_slots_workspace", "workspace_id", unique=True,
            postgresql_where=text("workspace_id IS NOT NULL"),
        ),
    )

    slot_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    occupied_request_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    workspace_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    admission_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    lease_kind: Mapped[str | None] = mapped_column(String(16))
    # Collection lease token or crawl run id per lease_kind; C3 fills it, it never grants ownership.
    source_owner_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConnectorWorkspaceDispatch(Base):
    """Persist fair-turn timestamps per workspace; a scheduling summary, never authorization."""
    __tablename__ = "connector_workspace_dispatch"

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    last_considered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConnectorProviderTerms(Base):
    """Per-source terms acknowledgement (owner) and review evidence (operator only).

    ``terms_revision`` increments on every change; operator fields are never written from an owner DTO.
    """
    __tablename__ = "connector_provider_terms"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_connector_provider_terms_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "source_id"], ["sources.workspace_id", "sources.id"],
            name="fk_connector_provider_terms_source", ondelete="CASCADE",
        ),
        CheckConstraint("terms_revision > 0", name="ck_connector_provider_terms_revision"),
        CheckConstraint(
            "declared_use IN ('personal', 'noncommercial', 'commercial', 'unknown')",
            name="ck_connector_provider_terms_use",
        ),
        CheckConstraint(
            "operator_review_state IN ('pending', 'approved', 'rejected')",
            name="ck_connector_provider_terms_review_state",
        ),
        CheckConstraint(
            "reviewed_allowed_use IS NULL OR reviewed_allowed_use IN ('personal', 'noncommercial', 'commercial')",
            name="ck_connector_provider_terms_reviewed_use",
        ),
        CheckConstraint(
            "(operator_review_state = 'pending' AND reviewer_user_id IS NULL AND reviewed_at IS NULL "
            "AND reviewed_allowed_use IS NULL AND review_evidence_ref IS NULL AND reviewed_terms_version IS NULL) "
            "OR (operator_review_state = 'rejected' AND reviewer_user_id IS NOT NULL AND reviewed_at IS NOT NULL "
            "AND review_evidence_ref IS NOT NULL) "
            "OR (operator_review_state = 'approved' AND reviewer_user_id IS NOT NULL AND reviewed_at IS NOT NULL "
            "AND reviewed_allowed_use IS NOT NULL AND review_evidence_ref IS NOT NULL "
            "AND reviewed_terms_version IS NOT NULL)",
            name="ck_connector_provider_terms_review_fields",
        ),
        Index("ix_connector_provider_terms_workspace", "workspace_id", "source_id"),
    )

    source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
    terms_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    terms_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    terms_version: Mapped[str] = mapped_column(String(64), nullable=False)
    checked_on: Mapped[date] = mapped_column(Date, nullable=False)
    owner_acknowledged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    owner_actor_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    declared_use: Mapped[str] = mapped_column(String(16), nullable=False)
    operator_review_state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    reviewer_user_id: Mapped[int | None] = mapped_column(Integer)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_allowed_use: Mapped[str | None] = mapped_column(String(16))
    review_evidence_ref: Mapped[str | None] = mapped_column(String(512))
    reviewed_terms_version: Mapped[str | None] = mapped_column(String(64))


class ConnectorQuotaWindow(Base):
    """Shared provider/credential/IP budget window; no workspace component so it is deployment-wide."""
    __tablename__ = "connector_quota_windows"
    __table_args__ = (
        CheckConstraint("budget_kind IN ('provider', 'credential', 'ip')", name="ck_connector_quota_windows_kind"),
        CheckConstraint("used_units >= 0", name="ck_connector_quota_windows_used"),
        CheckConstraint("limit_units IS NULL OR limit_units >= 0", name="ck_connector_quota_windows_limit"),
        CheckConstraint("window_end > window_start", name="ck_connector_quota_windows_span"),
        Index("ix_connector_quota_windows_end", "window_end"),
    )

    provider_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    budget_kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    subject_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    policy_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    unit: Mapped[str] = mapped_column(String(24), nullable=False)
    limit_units: Mapped[int | None] = mapped_column(BigInteger)
    used_units: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    policy_revision: Mapped[int] = mapped_column(Integer, nullable=False)


class ConnectorProviderSend(Base):
    """One committed physical provider send; holds no credential, private URL parameter or payload."""
    __tablename__ = "connector_provider_sends"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_connector_provider_sends_workspace", ondelete="CASCADE"),
        UniqueConstraint("request_id", "admission_token", "send_sequence", name="uq_connector_provider_sends_sequence"),
        CheckConstraint("attempt >= 0 AND send_sequence >= 0", name="ck_connector_provider_sends_counters"),
        Index("ix_connector_provider_sends_created", "created_at"),
    )

    send_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    admission_token: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    send_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_target_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ConnectorQuotaDebit(Base):
    """The units one send debited from one window; windows are never deleted while debits reference them."""
    __tablename__ = "connector_quota_debits"
    __table_args__ = (
        ForeignKeyConstraint(
            ["send_id"], ["connector_provider_sends.send_id"],
            name="fk_connector_quota_debits_send", ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["provider_id", "budget_kind", "subject_hash", "policy_key", "window_start"],
            ["connector_quota_windows.provider_id", "connector_quota_windows.budget_kind",
             "connector_quota_windows.subject_hash", "connector_quota_windows.policy_key",
             "connector_quota_windows.window_start"],
            name="fk_connector_quota_debits_window", ondelete="RESTRICT",
        ),
        CheckConstraint("units > 0", name="ck_connector_quota_debits_units"),
    )

    send_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    provider_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    budget_kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    subject_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    policy_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    units: Mapped[int] = mapped_column(Integer, nullable=False)
