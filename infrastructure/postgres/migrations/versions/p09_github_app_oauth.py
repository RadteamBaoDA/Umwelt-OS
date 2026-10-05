"""Add GitHub App browser attempts and source-scoped expiring user grants."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "p09_github_app_oauth"
down_revision: str | Sequence[str] | None = "r13_native_provider_collection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create encrypted GitHub token and single-use PKCE attempt tables."""
    op.create_table(
        "github_oauth_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_hash", sa.String(64), nullable=False),
        sa.Column("state_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("browser_hash", sa.String(64), nullable=False),
        sa.Column("encrypted_verifier", sa.Text(), nullable=False),
        sa.Column("source_generation", sa.Integer(), nullable=False),
        sa.Column("configuration_revision", sa.Integer(), nullable=False),
        sa.Column("expected_token_revision", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("source_generation > 0 AND configuration_revision >= 0", name="ck_github_oauth_attempt_fences"),
    )
    op.create_index("ix_github_oauth_attempt_expiry", "github_oauth_attempts", ["expires_at"])
    op.create_table(
        "github_oauth_grants",
        sa.Column("source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("github_user_id", sa.String(20), nullable=False),
        sa.Column("repository_id", sa.String(20), nullable=False),
        sa.Column("source_generation", sa.Integer(), nullable=False),
        sa.Column("configuration_revision", sa.Integer(), nullable=False),
        sa.Column("token_revision", sa.Integer(), nullable=False),
        sa.Column("encrypted_tokens", sa.Text()),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("validated_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("refresh_operation_id", postgresql.UUID(as_uuid=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("source_generation > 0 AND configuration_revision > 0 AND token_revision > 0", name="ck_github_oauth_grant_fences"),
        sa.CheckConstraint("state IN ('ready', 'refreshing', 'reconciliation_required', 'revoked')", name="ck_github_oauth_grant_state"),
    )
    op.create_index("ix_github_oauth_grant_peer", "github_oauth_grants", ["github_user_id", "state", "source_id"])
    op.create_table(
        "github_oauth_coordinators",
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("owner.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True)),
        sa.Column("state", sa.String(32), server_default="idle", nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("state IN ('idle', 'authorizing', 'refreshing', 'revoking', 'reconciliation_required')", name="ck_github_oauth_coordinator_state"),
    )


def downgrade() -> None:
    """Drop unshipped OAuth attempt and encrypted grant state."""
    op.drop_table("github_oauth_coordinators")
    op.drop_index("ix_github_oauth_grant_peer", table_name="github_oauth_grants")
    op.drop_table("github_oauth_grants")
    op.drop_index("ix_github_oauth_attempt_expiry", table_name="github_oauth_attempts")
    op.drop_table("github_oauth_attempts")
