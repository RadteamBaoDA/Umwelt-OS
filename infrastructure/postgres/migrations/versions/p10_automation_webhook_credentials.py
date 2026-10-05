"""Add expiring, revocable hash-only inbound automation credentials."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "p10_automation_webhook_credentials"
down_revision: str | Sequence[str] | None = "p10_automation_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create one credential slot per owner alias; never persist bearer plaintext."""
    op.create_table(
        "automation_webhook_credentials",
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("alias", sa.String(length=40), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("length(token_hash) = 64", name="ck_automation_webhook_credentials_hash"),
        sa.CheckConstraint("revision >= 1", name="ck_automation_webhook_credentials_revision"),
        sa.ForeignKeyConstraint(["owner_id"], ["owner.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("owner_id", "alias"),
        sa.UniqueConstraint("token_hash", name="uq_automation_webhook_credentials_token_hash"),
    )
    op.create_index(
        "ix_automation_webhook_credentials_expiry", "automation_webhook_credentials", ["expires_at"]
    )


def downgrade() -> None:
    """Remove only this additive credential table."""
    op.drop_index("ix_automation_webhook_credentials_expiry", table_name="automation_webhook_credentials")
    op.drop_table("automation_webhook_credentials")
