"""Persist tasks, goals, plan materialization, and news topics.

Revision ID: p08_tasks_goals
Revises: r10_dashboard_configuration
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "p08_tasks_goals"
down_revision: str | Sequence[str] | None = "r10_dashboard_configuration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create goals, tasks, and news_topics tables with relational constraints and indexes."""
    # 1. Goals table
    op.create_table(
        "goals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("desired_outcome", sa.Text(), nullable=True),
        sa.Column("deadline", sa.Date(), nullable=True),
        sa.Column("progress", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("manual_progress", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        sa.Column("milestones", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("entity_ids", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("accepted_proposals", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("revision", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'completed', 'paused', 'cancelled')",
            name="ck_goals_status",
        ),
        sa.CheckConstraint("progress BETWEEN 0.0 AND 100.0", name="ck_goals_progress"),
        sa.CheckConstraint("revision BETWEEN 1 AND 9007199254740991", name="ck_goals_revision"),
        sa.CheckConstraint("jsonb_typeof(milestones) = 'array'", name="ck_goals_milestones_array"),
        sa.CheckConstraint("jsonb_array_length(milestones) <= 100", name="ck_goals_milestones_bound"),
        sa.CheckConstraint("jsonb_typeof(entity_ids) = 'array'", name="ck_goals_entity_ids_array"),
        sa.CheckConstraint("jsonb_array_length(entity_ids) <= 100", name="ck_goals_entity_ids_bound"),
        sa.CheckConstraint("jsonb_typeof(accepted_proposals) = 'array'", name="ck_goals_accepted_proposals_array"),
        sa.CheckConstraint("jsonb_array_length(accepted_proposals) <= 1000", name="ck_goals_accepted_proposals_bound"),
        sa.ForeignKeyConstraint(["owner_id"], ["owner.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_goals_owner_status", "goals", ["owner_id", "status"])
    op.create_index("ix_goals_deadline", "goals", ["deadline"])
    op.create_index("ix_goals_created_at", "goals", ["created_at"])

    # 2. Tasks table
    op.create_table(
        "tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="inbox", nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("goal_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("entity_ids", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("revision", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('inbox', 'todo', 'in_progress', 'blocked', 'done', 'cancelled')",
            name="ck_tasks_status",
        ),
        sa.CheckConstraint("revision BETWEEN 1 AND 9007199254740991", name="ck_tasks_revision"),
        sa.CheckConstraint("due_date IS NULL OR due_at IS NULL", name="ck_tasks_one_due_kind"),
        sa.CheckConstraint("(status = 'done') = (completed_at IS NOT NULL)", name="ck_tasks_completion_timestamp"),
        sa.CheckConstraint("jsonb_typeof(entity_ids) = 'array'", name="ck_tasks_entity_ids_array"),
        sa.CheckConstraint("jsonb_array_length(entity_ids) <= 100", name="ck_tasks_entity_ids_bound"),
        sa.ForeignKeyConstraint(["owner_id"], ["owner.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["goal_id"], ["goals.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tasks_owner_status", "tasks", ["owner_id", "status"])
    op.create_index("ix_tasks_owner_due_date", "tasks", ["owner_id", "due_date"])
    op.create_index("ix_tasks_owner_due_at", "tasks", ["owner_id", "due_at"])
    op.create_index("ix_tasks_goal_id", "tasks", ["goal_id"])
    op.create_index("ix_tasks_created_at", "tasks", ["created_at"])
    op.create_index("ix_tasks_owner_deleted_at", "tasks", ["owner_id", "deleted_at"])

    # 3. News Topics table
    op.create_table(
        "news_topics",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("keywords", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("weight", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("jsonb_typeof(keywords) = 'array'", name="ck_news_topics_keywords_array"),
        sa.ForeignKeyConstraint(["owner_id"], ["owner.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_news_topics_owner_id", "news_topics", ["owner_id"])


def downgrade() -> None:
    """Drop news_topics, tasks, and goals tables in reverse dependency order."""
    op.drop_index("ix_news_topics_owner_id", table_name="news_topics")
    op.drop_table("news_topics")

    op.drop_index("ix_tasks_created_at", table_name="tasks")
    op.drop_index("ix_tasks_owner_deleted_at", table_name="tasks")
    op.drop_index("ix_tasks_goal_id", table_name="tasks")
    op.drop_index("ix_tasks_owner_due_at", table_name="tasks")
    op.drop_index("ix_tasks_owner_due_date", table_name="tasks")
    op.drop_index("ix_tasks_owner_status", table_name="tasks")
    op.drop_table("tasks")

    op.drop_index("ix_goals_created_at", table_name="goals")
    op.drop_index("ix_goals_deadline", table_name="goals")
    op.drop_index("ix_goals_owner_status", table_name="goals")
    op.drop_table("goals")
