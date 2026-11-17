"""Add instance-wide system health samples and their retention settings singleton."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_system_health_history"
down_revision: str | Sequence[str] | None = "0008_owner_display_name_accent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the sample table (indexed by time) and the retention singleton seeded at 7 days / 1-minute collection (0 = collection off)."""
    op.create_table(
        "system_health_samples",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("overall", sa.Text(), nullable=False),
        sa.Column("components", postgresql.JSONB(), nullable=False),
        sa.Column("queue_backlog", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("db_stats", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.CheckConstraint("queue_backlog >= 0", name="ck_system_health_samples_backlog"),
    )
    op.create_index("ix_system_health_samples_sampled_at", "system_health_samples", ["sampled_at"])
    op.create_table(
        "system_history_settings",
        sa.Column("id", sa.SmallInteger(), primary_key=True, server_default="1"),
        sa.Column("retention_days", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("sample_interval_minutes", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_system_history_settings_singleton"),
        sa.CheckConstraint("retention_days BETWEEN 1 AND 90", name="ck_system_history_settings_days"),
        sa.CheckConstraint(
            "sample_interval_minutes IN (0, 1, 5, 15, 30, 60)", name="ck_system_history_settings_interval",
        ),
    )
    op.execute("INSERT INTO system_history_settings (id) VALUES (1)")


def downgrade() -> None:
    """Drop both tables."""
    op.drop_table("system_history_settings")
    op.drop_index("ix_system_health_samples_sampled_at", table_name="system_health_samples")
    op.drop_table("system_health_samples")
