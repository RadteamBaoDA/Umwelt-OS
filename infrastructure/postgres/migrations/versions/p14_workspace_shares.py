"""Add owner-granted per-member document/brief shares."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "p14_workspace_shares"
down_revision: str | Sequence[str] | None = "r15_highlight_rule_delivery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace_shares",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("resource_type", sa.String(length=16), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("member_user_id", sa.Integer(), nullable=False),
        sa.Column("granted_by_user_id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("resource_revision", sa.BigInteger(), nullable=False),
        sa.Column("membership_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "resource_type", "resource_id", "member_user_id"),
        sa.CheckConstraint("resource_type IN ('document', 'brief')", name="ck_workspace_shares_resource_type"),
        sa.CheckConstraint("revision > 0", name="ck_workspace_shares_positive_revision"),
        sa.CheckConstraint("resource_revision > 0", name="ck_workspace_shares_positive_resource_revision"),
        sa.CheckConstraint("membership_revision > 0", name="ck_workspace_shares_positive_membership_revision"),
        sa.CheckConstraint("member_user_id <> granted_by_user_id", name="ck_workspace_shares_not_self"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "member_user_id"], ["workspace_memberships.workspace_id", "workspace_memberships.user_id"],
            name="fk_workspace_shares_member", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "granted_by_user_id"], ["workspaces.id", "workspaces.owner_user_id"],
            name="fk_workspace_shares_matching_owner", ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_workspace_shares_member_active", "workspace_shares",
        ["workspace_id", "member_user_id", "resource_type", "resource_id"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index("ix_workspace_shares_resource", "workspace_shares", ["workspace_id", "resource_type", "resource_id"])


def downgrade() -> None:
    if not context.is_offline_mode():
        active = op.get_bind().execute(sa.text("SELECT 1 FROM workspace_shares WHERE revoked_at IS NULL LIMIT 1")).first()
        if active is not None:
            raise RuntimeError("Refusing to drop workspace_shares while active shares exist")
    op.drop_index("ix_workspace_shares_resource", table_name="workspace_shares")
    op.drop_index("ix_workspace_shares_member_active", table_name="workspace_shares")
    op.drop_table("workspace_shares")
