"""Persist administrator-reviewed MCP stdio profile identities without changing history."""

from alembic import op
import sqlalchemy as sa


revision = "p07_mcp_stdio_identity"
down_revision = "p07_mcp_connections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add nullable identity columns so historical approvals remain unavailable pending review."""
    op.add_column("mcp_connections", sa.Column("deployment_profile_hash", sa.String(length=64), nullable=True))
    op.add_column("mcp_connections", sa.Column("draft_check_profile_hash", sa.String(length=64), nullable=True))
    op.add_column("mcp_discoveries", sa.Column("deployment_profile_hash", sa.String(length=64), nullable=True))
    op.add_column("mcp_capability_grants", sa.Column("reviewed_profile_hash", sa.String(length=64), nullable=True))
    op.create_check_constraint(
        "ck_mcp_connection_profile_hash", "mcp_connections",
        "deployment_profile_hash IS NULL OR deployment_profile_hash ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_mcp_connection_profile_transport", "mcp_connections",
        "deployment_profile_hash IS NULL OR transport = 'stdio'",
    )
    op.create_check_constraint(
        "ck_mcp_connection_check_profile_hash", "mcp_connections",
        "draft_check_profile_hash IS NULL OR draft_check_profile_hash ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_mcp_connection_check_profile_identity", "mcp_connections",
        "draft_check_profile_hash IS NULL OR (transport = 'stdio' AND deployment_profile_hash = draft_check_profile_hash AND health_code IS NOT NULL AND health_code = 'connected')",
    )
    op.create_check_constraint(
        "ck_mcp_discovery_profile_hash", "mcp_discoveries",
        "deployment_profile_hash IS NULL OR deployment_profile_hash ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_mcp_grant_profile_hash", "mcp_capability_grants",
        "reviewed_profile_hash IS NULL OR reviewed_profile_hash ~ '^[0-9a-f]{64}$'",
    )


def downgrade() -> None:
    """Drop only this revision's added checks and nullable columns."""
    op.drop_constraint("ck_mcp_grant_profile_hash", "mcp_capability_grants", type_="check")
    op.drop_constraint("ck_mcp_discovery_profile_hash", "mcp_discoveries", type_="check")
    op.drop_constraint("ck_mcp_connection_check_profile_identity", "mcp_connections", type_="check")
    op.drop_constraint("ck_mcp_connection_check_profile_hash", "mcp_connections", type_="check")
    op.drop_constraint("ck_mcp_connection_profile_transport", "mcp_connections", type_="check")
    op.drop_constraint("ck_mcp_connection_profile_hash", "mcp_connections", type_="check")
    op.drop_column("mcp_capability_grants", "reviewed_profile_hash")
    op.drop_column("mcp_discoveries", "deployment_profile_hash")
    op.drop_column("mcp_connections", "draft_check_profile_hash")
    op.drop_column("mcp_connections", "deployment_profile_hash")
