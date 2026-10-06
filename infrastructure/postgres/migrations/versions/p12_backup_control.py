"""Persist backup epochs, operation receipts, and drain accounting."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "p12_backup_control"
down_revision: str | Sequence[str] | None = "p12_onboarding_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the maintenance barrier tables and their durable initial idle row."""
    op.create_table(
        "p12_backup_operations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("epoch", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=40), server_default="pending", nullable=False),
        sa.Column("consistency", sa.String(length=32), server_default="quiesced", nullable=False),
        sa.Column("completeness", sa.String(length=32), server_default="incomplete", nullable=False),
        sa.Column("archive_name", sa.String(length=255), nullable=True),
        sa.Column("stage_receipts", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("epoch >= 1", name="ck_p12_backup_operations_epoch"),
        sa.CheckConstraint("completeness IN ('complete', 'incomplete')", name="ck_p12_backup_operations_completeness"),
        sa.CheckConstraint(
            "status IN ('pending', 'draining', 'quiesced', 'snapshotting', 'resuming', 'completed', 'incomplete', 'failed', 'failed_recovery_required')",
            name="ck_p12_backup_operations_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "p12_backup_control",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("epoch", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("phase", sa.String(length=40), server_default="idle", nullable=False),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("coordinator_id", sa.String(length=128), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_p12_backup_control_singleton"),
        sa.CheckConstraint("epoch >= 1", name="ck_p12_backup_control_epoch"),
        sa.CheckConstraint(
            "phase IN ('idle', 'draining', 'quiesced', 'snapshotting', 'resuming', 'failed_recovery_required')",
            name="ck_p12_backup_control_phase",
        ),
        sa.ForeignKeyConstraint(["operation_id"], ["p12_backup_operations.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "p12_backup_activity",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("epoch", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=80), nullable=False),
        sa.Column("work_id", sa.String(length=255), nullable=True),
        sa.Column("state", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("epoch >= 1", name="ck_p12_backup_activity_epoch"),
        sa.CheckConstraint("state IN ('active', 'finished', 'interrupted', 'uncertain')", name="ck_p12_backup_activity_state"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_p12_backup_activity_active_epoch", "p12_backup_activity", ["state", "epoch"],
    )
    op.execute("INSERT INTO p12_backup_control (id, epoch, phase) VALUES (1, 1, 'idle')")


def downgrade() -> None:
    """Drop only the P12 backup barrier and its operation history."""
    op.drop_index("ix_p12_backup_activity_active_epoch", table_name="p12_backup_activity")
    op.drop_table("p12_backup_activity")
    op.drop_table("p12_backup_control")
    op.drop_table("p12_backup_operations")
