"""Persist verified GitHub App installation identity for bounded polling."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "p09_github_sync_state"
down_revision: str | Sequence[str] | None = "p09_github_oauth_operation_recovery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add verified polling identity, binding revision, and durable owner-reset gap records."""
    op.add_column("github_oauth_grants", sa.Column("installation_id", sa.String(20), nullable=True))
    op.add_column("github_oauth_grants", sa.Column("app_id", sa.String(20), nullable=True))
    op.add_column("github_oauth_grants", sa.Column("binding_revision", sa.Integer(), server_default="1", nullable=False))
    op.create_check_constraint("ck_github_oauth_grant_binding_revision", "github_oauth_grants", "binding_revision > 0")
    op.create_table(
        "github_sync_resets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_generation", sa.Integer(), nullable=False),
        sa.Column("connector_revision", sa.Integer(), nullable=False),
        sa.Column("scope_sha256", sa.String(64), nullable=False),
        sa.Column("reset_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("source_generation > 0 AND connector_revision > 0", name="ck_github_sync_reset_revisions"),
        sa.CheckConstraint("length(scope_sha256) = 64 AND scope_sha256 !~ '[^0-9a-f]'", name="ck_github_sync_reset_scope_digest"),
    )
    op.create_index("ix_github_sync_reset_source", "github_sync_resets", ["source_id", "reset_at"])


def downgrade() -> None:
    """Remove GitHub polling identity added by this unshipped migration."""
    op.drop_index("ix_github_sync_reset_source", table_name="github_sync_resets")
    op.drop_table("github_sync_resets")
    op.drop_constraint("ck_github_oauth_grant_binding_revision", "github_oauth_grants", type_="check")
    op.drop_column("github_oauth_grants", "binding_revision")
    op.drop_column("github_oauth_grants", "app_id")
    op.drop_column("github_oauth_grants", "installation_id")
