"""Persist owner MCP connections, immutable discovery descriptors, exact grants, and inbound clients."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "p07_mcp_connections"
# Provisional branch parent per R08 controller ruling; root must reconcile active heads before integration.
down_revision: str | Sequence[str] | None = "p06_selective_memory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create MCP persistence with explicit owner cascades, immutable descriptor references, and bounded status fields."""
    uuid_type = postgresql.UUID(as_uuid=True)
    jsonb_type = postgresql.JSONB(astext_type=sa.Text())
    tz = sa.DateTime(timezone=True)

    op.create_table(
        "mcp_connections",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("owner.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("transport", sa.String(32), nullable=False),
        sa.Column("endpoint", sa.String(2048)),
        sa.Column("deployment_profile_id", sa.String(80)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("auth_method", sa.String(16), nullable=False, server_default="none"),
        sa.Column("encrypted_credential", sa.Text()),
        sa.Column("credential_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("health_code", sa.String(64)),
        sa.Column("health_at", tz),
        sa.Column("created_at", tz, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", tz, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("revision > 0 AND credential_revision > 0", name="ck_mcp_connection_revisions"),
        sa.CheckConstraint("timeout_seconds BETWEEN 1 AND 60", name="ck_mcp_connection_timeout"),
        sa.CheckConstraint("transport IN ('streamable_http', 'stdio')", name="ck_mcp_connection_transport"),
        sa.CheckConstraint("auth_method IN ('none', 'bearer')", name="ck_mcp_connection_auth"),
        sa.CheckConstraint("(endpoint IS NULL) != (deployment_profile_id IS NULL)", name="ck_mcp_connection_target"),
        sa.CheckConstraint("enabled = false OR health_code IS NULL OR health_code <> 'needs_review'", name="ck_mcp_connection_enable_review"),
    )
    op.create_index("ix_mcp_connections_owner_updated", "mcp_connections", ["owner_id", "updated_at"])

    op.create_table(
        "mcp_discoveries",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("connection_id", uuid_type, sa.ForeignKey("mcp_connections.id", ondelete="CASCADE", name="fk_mcp_discovery_connection"), nullable=False),
        sa.Column("connection_revision", sa.Integer(), nullable=False),
        sa.Column("negotiated_protocol", sa.String(40), nullable=False),
        sa.Column("server_info", jsonb_type, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("schema_set_hash", sa.String(64), nullable=False),
        sa.Column("capability_count", sa.Integer(), nullable=False),
        sa.Column("created_at", tz, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("connection_revision > 0 AND capability_count BETWEEN 0 AND 200", name="ck_mcp_discovery_bounds"),
        sa.CheckConstraint("schema_set_hash ~ '^[0-9a-f]{64}$'", name="ck_mcp_discovery_hash"),
    )
    op.create_index("ix_mcp_discoveries_connection_created", "mcp_discoveries", ["connection_id", "created_at"])

    op.create_table(
        "mcp_capabilities",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("discovery_id", uuid_type, sa.ForeignKey("mcp_discoveries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("remote_key", sa.String(2048), nullable=False),
        sa.Column("descriptor", jsonb_type, nullable=False),
        sa.Column("descriptor_hash", sa.String(64), nullable=False),
        sa.CheckConstraint("kind IN ('tool', 'resource', 'resource_template')", name="ck_mcp_capability_kind"),
        sa.CheckConstraint("length(descriptor_hash) = 64", name="ck_mcp_capability_hash_length"),
        sa.CheckConstraint("octet_length(descriptor::text) <= 65536", name="ck_mcp_capability_schema_bytes"),
        sa.UniqueConstraint("discovery_id", "kind", "remote_key", name="uq_mcp_capability_discovery_key"),
    )
    op.create_index("ix_mcp_capabilities_discovery", "mcp_capabilities", ["discovery_id"])

    op.create_table(
        "mcp_capability_grants",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("connection_id", uuid_type, sa.ForeignKey("mcp_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("capability_id", uuid_type, sa.ForeignKey("mcp_capabilities.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("descriptor_hash", sa.String(64), nullable=False),
        sa.Column("reviewed_connection_revision", sa.Integer(), nullable=False),
        sa.Column("grant_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("risk", sa.String(24), nullable=False),
        sa.Column("source_ids", jsonb_type, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("destinations", jsonb_type, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("expires_at", tz),
        sa.Column("revoked_at", tz),
        sa.Column("reviewed_at", tz, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("purpose IN ('chat', 'collection')", name="ck_mcp_grant_purpose"),
        sa.CheckConstraint("risk IN ('READ_ONLY', 'INTERNAL_WRITE', 'EXTERNAL_WRITE', 'DESTRUCTIVE')", name="ck_mcp_grant_risk"),
        sa.CheckConstraint("grant_revision > 0 AND reviewed_connection_revision > 0", name="ck_mcp_grant_revisions"),
    )
    op.create_index("ix_mcp_grants_connection_active", "mcp_capability_grants", ["connection_id", "revoked_at", "expires_at"])

    op.create_table(
        "mcp_inbound_clients",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("owner.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("token_prefix", sa.String(20), nullable=False),
        sa.Column("audience", sa.String(255), nullable=False),
        sa.Column("bindings", jsonb_type, nullable=False),
        sa.Column("source_ids", jsonb_type, nullable=False),
        sa.Column("capabilities", jsonb_type, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("expires_at", tz, nullable=False),
        sa.Column("revoked_at", tz),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", tz, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("length(token_hash) = 64", name="ck_mcp_inbound_token_hash"),
        sa.CheckConstraint("revision > 0", name="ck_mcp_inbound_revision"),
        sa.UniqueConstraint("token_hash", name="uq_mcp_inbound_token_hash"),
    )
    op.create_index("ix_mcp_inbound_clients_active", "mcp_inbound_clients", ["revoked_at", "expires_at"])


def downgrade() -> None:
    """Drop inbound identities, grants, descriptors, discoveries, and connections in FK-safe reverse order."""
    op.drop_table("mcp_inbound_clients")
    op.drop_table("mcp_capability_grants")
    op.drop_table("mcp_capabilities")
    op.drop_table("mcp_discoveries")
    op.drop_table("mcp_connections")
