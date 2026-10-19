"""Durable account-erasure receipts; irreversible removal requires forward recovery."""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "r22_account_deletion"
down_revision: str | Sequence[str] | None = "r21_drop_backup_control"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add bounded recovery indexes and scope/status checks without changing account data."""
    op.create_table(
        "account_deletion_operations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("account_id", sa.Integer()),
        sa.Column("workspace_id", sa.Uuid()),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("phase", sa.String(64), nullable=False),
        sa.Column("cursor", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('queued','running','failed','succeeded')", name="ck_account_deletion_status"),
        sa.CheckConstraint("(status = 'succeeded' AND account_id IS NULL AND workspace_id IS NULL) OR "
                           "(status <> 'succeeded' AND account_id IS NOT NULL AND workspace_id IS NOT NULL)",
                           name="ck_account_deletion_scope"),
    )
    op.create_index("uq_account_deletion_receipt", "account_deletion_operations", ["receipt_hash"], unique=True)
    op.create_index("uq_account_deletion_account", "account_deletion_operations", ["account_id"], unique=True)
    op.create_index("ix_account_deletion_due", "account_deletion_operations", ["status", "next_attempt_at"])


def downgrade() -> None:
    """Refuse rollback: discarded recovery authority cannot restore erased identity/data."""
    raise RuntimeError("Account deletion is forward-only; retain durable recovery receipts")
