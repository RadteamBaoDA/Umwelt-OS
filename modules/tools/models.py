"""Durable owner-managed MCP connections, immutable discoveries, grants, and inbound clients."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, LargeBinary, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from core.database import Base


class BrowserReadJob(Base):
    """Own durable browser observation identity, authority fences, budgets, and lifecycle state."""

    __tablename__ = "browser_read_jobs"
    __table_args__ = (
        CheckConstraint("owner_id = 1", name="ck_browser_read_jobs_single_owner"),
        CheckConstraint("tool_slot BETWEEN 1 AND 10", name="ck_browser_read_jobs_slot"),
        CheckConstraint("claim_generation > 0 AND source_generation > 0 AND connector_revision > 0", name="ck_browser_read_jobs_fences"),
        CheckConstraint("grant_revision > 0 AND max_pages BETWEEN 1 AND 3", name="ck_browser_read_jobs_limits"),
        CheckConstraint("status IN ('queued','running','succeeded','cancel_requested','cancelled','failed','uncertain','expired')", name="ck_browser_read_jobs_status"),
        CheckConstraint("actual_pages BETWEEN 0 AND 3 AND actual_bytes BETWEEN 0 AND 5242880", name="ck_browser_read_jobs_usage"),
        UniqueConstraint("run_id", "tool_slot", name="uq_browser_read_jobs_run_slot"),
        Index("ix_browser_read_jobs_owner_expiry", "owner_id", "expires_at"),
        Index("ix_browser_read_jobs_retention", "status", "expires_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    operation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, unique=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    tool_slot: Mapped[int] = mapped_column(Integer, nullable=False)
    auth_session_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    conversation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    profile_id: Mapped[str] = mapped_column(String(24), nullable=False)
    authorized_source_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    profile_revision_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    claim_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    connector_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    grant_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    scope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    max_pages: Mapped[int] = mapped_column(Integer, nullable=False)
    max_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=5 * 1024 * 1024)
    max_active_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=45)
    actual_pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    actual_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    request_ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    service_instance_id: Mapped[str | None] = mapped_column(String(128))
    service_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    cancel_requested: Mapped[bool] = mapped_column(nullable=False, default=False)
    result_hash: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(32))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class BrowserPageEvidence(Base):
    """Retain bounded raw page bytes and extracted text under a private job-owned identity."""

    __tablename__ = "browser_page_evidence"
    __table_args__ = (
        CheckConstraint("page_number BETWEEN 1 AND 3", name="ck_browser_page_evidence_number"),
        CheckConstraint("octet_length(raw_content) <= 5242880", name="ck_browser_page_evidence_raw_bytes"),
        CheckConstraint("octet_length(extracted_text) <= 20000", name="ck_browser_page_evidence_text_bytes"),
        UniqueConstraint("job_id", "page_number", name="uq_browser_page_evidence_job_page"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("browser_read_jobs.id", ondelete="CASCADE"), nullable=False)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    final_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    extracted_text: Mapped[str] = mapped_column(Text, nullable=False)


class McpConnection(Base):
    """Revisioned HTTP endpoint or deployment profile; stdio stores review hashes, never launch inputs."""
    __tablename__ = "mcp_connections"
    __table_args__ = (
        CheckConstraint("revision > 0 AND credential_revision > 0", name="ck_mcp_connection_revisions"),
        CheckConstraint("timeout_seconds BETWEEN 1 AND 60", name="ck_mcp_connection_timeout"),
        CheckConstraint("transport IN ('streamable_http', 'stdio')", name="ck_mcp_connection_transport"),
        CheckConstraint("auth_method IN ('none', 'bearer')", name="ck_mcp_connection_auth"),
        CheckConstraint("(endpoint IS NULL) != (deployment_profile_id IS NULL)", name="ck_mcp_connection_target"),
        CheckConstraint("deployment_profile_hash IS NULL OR deployment_profile_hash ~ '^[0-9a-f]{64}$'", name="ck_mcp_connection_profile_hash"),
        CheckConstraint("deployment_profile_hash IS NULL OR transport = 'stdio'", name="ck_mcp_connection_profile_transport"),
        CheckConstraint("draft_check_profile_hash IS NULL OR draft_check_profile_hash ~ '^[0-9a-f]{64}$'", name="ck_mcp_connection_check_profile_hash"),
        CheckConstraint("draft_check_profile_hash IS NULL OR (transport = 'stdio' AND deployment_profile_hash = draft_check_profile_hash AND health_code IS NOT NULL AND health_code = 'connected')", name="ck_mcp_connection_check_profile_identity"),
        CheckConstraint("enabled = false OR health_code IS NULL OR health_code <> 'needs_review'", name="ck_mcp_connection_enable_review"),
        Index("ix_mcp_connections_owner_updated", "owner_id", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("owner.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    transport: Mapped[str] = mapped_column(String(32), nullable=False)
    endpoint: Mapped[str | None] = mapped_column(String(2048))
    deployment_profile_id: Mapped[str | None] = mapped_column(String(80))
    deployment_profile_hash: Mapped[str | None] = mapped_column(String(64))
    draft_check_profile_hash: Mapped[str | None] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    enabled: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    auth_method: Mapped[str] = mapped_column(String(16), nullable=False, server_default="none")
    encrypted_credential: Mapped[str | None] = mapped_column(Text())
    credential_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="30")
    health_code: Mapped[str | None] = mapped_column(String(64))
    health_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class McpDiscovery(Base):
    """Immutable negotiation descriptors and the exact stdio profile identity used to discover them."""
    __tablename__ = "mcp_discoveries"
    __table_args__ = (
        ForeignKeyConstraint(["connection_id"], ["mcp_connections.id"], ondelete="CASCADE", name="fk_mcp_discovery_connection"),
        CheckConstraint("connection_revision > 0 AND capability_count BETWEEN 0 AND 200", name="ck_mcp_discovery_bounds"),
        CheckConstraint("schema_set_hash ~ '^[0-9a-f]{64}$'", name="ck_mcp_discovery_hash"),
        CheckConstraint("deployment_profile_hash IS NULL OR deployment_profile_hash ~ '^[0-9a-f]{64}$'", name="ck_mcp_discovery_profile_hash"),
        Index("ix_mcp_discoveries_connection_created", "connection_id", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    connection_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    connection_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    negotiated_protocol: Mapped[str] = mapped_column(String(40), nullable=False)
    server_info: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, server_default="{}")
    schema_set_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    deployment_profile_hash: Mapped[str | None] = mapped_column(String(64))
    capability_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class McpCapability(Base):
    """One immutable, untrusted tool/resource/template descriptor in a discovery snapshot."""
    __tablename__ = "mcp_capabilities"
    __table_args__ = (
        CheckConstraint("kind IN ('tool', 'resource', 'resource_template')", name="ck_mcp_capability_kind"),
        CheckConstraint("length(descriptor_hash) = 64", name="ck_mcp_capability_hash_length"),
        CheckConstraint("octet_length(descriptor::text) <= 65536", name="ck_mcp_capability_schema_bytes"),
        UniqueConstraint("discovery_id", "kind", "remote_key", name="uq_mcp_capability_discovery_key"),
        Index("ix_mcp_capabilities_discovery", "discovery_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    discovery_id: Mapped[UUID] = mapped_column(ForeignKey("mcp_discoveries.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    remote_key: Mapped[str] = mapped_column(String(2048), nullable=False)
    descriptor: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    descriptor_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class McpCapabilityGrant(Base):
    """Owner selection bound to descriptor, connection/profile revisions, purpose, risk, scope and expiry."""
    __tablename__ = "mcp_capability_grants"
    __table_args__ = (
        CheckConstraint("purpose IN ('chat', 'collection')", name="ck_mcp_grant_purpose"),
        CheckConstraint("risk IN ('READ_ONLY', 'INTERNAL_WRITE', 'EXTERNAL_WRITE', 'DESTRUCTIVE')", name="ck_mcp_grant_risk"),
        CheckConstraint("grant_revision > 0 AND reviewed_connection_revision > 0", name="ck_mcp_grant_revisions"),
        CheckConstraint("reviewed_profile_hash IS NULL OR reviewed_profile_hash ~ '^[0-9a-f]{64}$'", name="ck_mcp_grant_profile_hash"),
        Index("ix_mcp_grants_connection_active", "connection_id", "revoked_at", "expires_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    connection_id: Mapped[UUID] = mapped_column(ForeignKey("mcp_connections.id", ondelete="CASCADE"), nullable=False)
    capability_id: Mapped[UUID] = mapped_column(ForeignKey("mcp_capabilities.id", ondelete="RESTRICT"), nullable=False)
    descriptor_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewed_connection_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    reviewed_profile_hash: Mapped[str | None] = mapped_column(String(64))
    grant_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    risk: Mapped[str] = mapped_column(String(24), nullable=False)
    source_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    destinations: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class McpInboundClient(Base):
    """Hash-only, audience-bound inbound bearer identity with explicit tool/source scope."""
    __tablename__ = "mcp_inbound_clients"
    __table_args__ = (
        CheckConstraint("length(token_hash) = 64", name="ck_mcp_inbound_token_hash"),
        CheckConstraint("revision > 0", name="ck_mcp_inbound_revision"),
        UniqueConstraint("token_hash", name="uq_mcp_inbound_token_hash"),
        Index("ix_mcp_inbound_clients_active", "revoked_at", "expires_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("owner.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(20), nullable=False)
    audience: Mapped[str] = mapped_column(String(255), nullable=False)
    bindings: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    source_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
