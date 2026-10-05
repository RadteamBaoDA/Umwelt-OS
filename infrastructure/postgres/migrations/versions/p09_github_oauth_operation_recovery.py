"""Retain GitHub OAuth recovery identity independently of source lifecycle rows."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "p09_github_oauth_operation_recovery"
down_revision: str | Sequence[str] | None = "p09_github_app_oauth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create non-cascading recovery tombstones for owner-wide GitHub operations."""
    op.create_table(
        "github_oauth_operations",
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("operation_kind", sa.String(24), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True)),
        sa.Column("source_generation", sa.Integer()),
        sa.Column("configuration_revision", sa.Integer()),
        sa.Column("token_revision", sa.Integer()),
        sa.Column("peer_inventory", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("state", sa.String(32), nullable=False, server_default="in_progress"),
        sa.Column("error_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("operation_kind IN ('authorization', 'refresh', 'revoke')", name="ck_github_oauth_operation_kind"),
        sa.CheckConstraint("state IN ('in_progress', 'reconciliation_required', 'review_required', 'completed', 'acknowledged')", name="ck_github_oauth_operation_state"),
        sa.CheckConstraint("source_generation IS NULL OR source_generation > 0", name="ck_github_oauth_operation_generation"),
        sa.CheckConstraint("configuration_revision IS NULL OR configuration_revision >= 0", name="ck_github_oauth_operation_revision"),
        sa.CheckConstraint("token_revision IS NULL OR token_revision >= 0", name="ck_github_oauth_operation_token_revision"),
        sa.CheckConstraint("jsonb_array_length(peer_inventory) <= 100", name="ck_github_oauth_operation_peer_bound"),
    )
    op.create_index(
        "ix_github_oauth_operation_owner_state", "github_oauth_operations",
        ["owner_id", "state", "created_at"],
    )


def downgrade() -> None:
    """Drop the unshipped GitHub OAuth recovery tombstones."""
    op.drop_index("ix_github_oauth_operation_owner_state", table_name="github_oauth_operations")
    op.drop_table("github_oauth_operations")
