"""Create owner-scoped encrypted configuration for transient live map providers."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_live_map_config"
down_revision: str | Sequence[str] | None = "0007_collection_receipt_stats"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create only credential settings; provider events and imagery are not persisted."""
    op.create_table(
        "dashboard_live_credentials",
        sa.Column("workspace_id", sa.Uuid(), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("owner.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("encrypted_payload", sa.Text(), nullable=True),
        sa.CheckConstraint("revision > 0", name="ck_live_map_credentials_revision"),
        sa.ForeignKeyConstraint(["workspace_id", "owner_id"], ["workspaces.id", "workspaces.owner_user_id"],
                                name="fk_live_map_credentials_owner", ondelete="CASCADE"),
    )
    op.create_table(
        "dashboard_live_terms",
        sa.Column("workspace_id", sa.Uuid(), primary_key=True),
        sa.Column("provider_id", sa.String(64), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("owner.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("declared_use", sa.String(32), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("review_state", sa.String(16), nullable=False),
        sa.Column("reviewer_user_id", sa.Integer(), sa.ForeignKey("owner.id", ondelete="SET NULL")),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("evidence_ref", sa.String(512)),
        sa.ForeignKeyConstraint(["workspace_id", "owner_id"], ["workspaces.id", "workspaces.owner_user_id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    """Remove only live-map settings when reverting this additive migration."""
    op.drop_table("dashboard_live_terms")
    op.drop_table("dashboard_live_credentials")
