"""Persist revisioned daily briefs, the editable brief schedule and owner notifications."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "p08_daily_brief_notifications"
down_revision: str | Sequence[str] | None = "p08_news_stories"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create brief revision, schedule and notification tables with dedupe constraints."""
    op.create_table(
        "daily_briefs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("brief_date", sa.Date(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="current", nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("citations", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("model_alias", sa.String(length=32), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_daily_briefs_revision"),
        sa.CheckConstraint("status IN ('current', 'stale')", name="ck_daily_briefs_status"),
        sa.CheckConstraint("jsonb_typeof(citations) = 'array'", name="ck_daily_briefs_citations_array"),
        sa.ForeignKeyConstraint(["owner_id"], ["owner.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "brief_date", "timezone", "revision", name="uq_daily_briefs_revision"),
    )
    op.create_index("ix_daily_briefs_owner_date", "daily_briefs", ["owner_id", "brief_date", "timezone"])
    op.create_table(
        "brief_schedules",
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("hour", sa.Integer(), server_default="7", nullable=False),
        sa.Column("minute", sa.Integer(), server_default="0", nullable=False),
        sa.Column("timezone", sa.String(length=64), server_default="Asia/Ho_Chi_Minh", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("hour BETWEEN 0 AND 23 AND minute BETWEEN 0 AND 59", name="ck_brief_schedules_time"),
        sa.ForeignKeyConstraint(["owner_id"], ["owner.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("owner_id"),
    )
    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("body", sa.String(length=2000), nullable=True),
        sa.Column("link", sa.String(length=300), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["owner.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "dedupe_key", name="uq_notifications_dedupe"),
    )
    op.create_index("ix_notifications_owner_created", "notifications", ["owner_id", "created_at"])


def downgrade() -> None:
    """Drop notification, schedule and brief tables."""
    op.drop_table("notifications")
    op.drop_table("brief_schedules")
    op.drop_table("daily_briefs")
