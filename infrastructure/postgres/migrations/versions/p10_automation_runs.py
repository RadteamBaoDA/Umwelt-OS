"""Create automation trigger inbox, schedule slots, runs and per-action ledger."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "p10_automation_runs"
down_revision: str | Sequence[str] | None = "p10_automations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the five dispatch tables; the run identity unique index is the dedupe fence."""
    op.create_table(
        "automation_triggers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("trigger_type", sa.String(length=32), nullable=False),
        sa.Column("event_key", sa.String(length=200), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("origin_automation_id", sa.Uuid(), nullable=True),
        sa.Column("origin_run_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('pending','processed')", name="ck_automation_triggers_status"),
        sa.CheckConstraint("depth BETWEEN 0 AND 50", name="ck_automation_triggers_depth"),
        sa.ForeignKeyConstraint(["owner_id"], ["owner.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "trigger_type", "event_key", name="uq_automation_triggers_event"),
    )
    op.create_index("ix_automation_triggers_pending", "automation_triggers", ["status", "created_at"])
    op.create_table(
        "automation_schedules",
        sa.Column("automation_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("cron", sa.String(length=120), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("next_slot", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_slot", sa.DateTime(timezone=True), nullable=True),
        sa.Column("misfire_policy", sa.String(length=16), nullable=False),
        sa.CheckConstraint("misfire_policy IN ('coalesce')", name="ck_automation_schedules_misfire"),
        sa.ForeignKeyConstraint(["automation_id"], ["automations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("automation_id"),
    )
    op.create_index("ix_automation_schedules_next", "automation_schedules", ["next_slot"])
    op.create_table(
        "automation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("automation_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("trigger_type", sa.String(length=32), nullable=False),
        sa.Column("trigger_key", sa.String(length=240), nullable=False),
        sa.Column("trigger_event_id", sa.String(length=200), nullable=True),
        sa.Column("scheduled_slot", sa.DateTime(timezone=True), nullable=True),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("origin_automation_id", sa.Uuid(), nullable=True),
        sa.Column("origin_run_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("reason", sa.String(length=48), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("dispatch_generation", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued','running','awaiting_approval','succeeded','failed','skipped','dropped','requires_review')",
            name="ck_automation_runs_status"),
        sa.CheckConstraint("depth BETWEEN 1 AND 50", name="ck_automation_runs_depth"),
        sa.ForeignKeyConstraint(["owner_id"], ["owner.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["automation_id"], ["automations.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("automation_id", "revision", "trigger_key", name="uq_automation_runs_identity"),
    )
    op.create_index("ix_automation_runs_dispatch", "automation_runs", ["status", "next_attempt_at"])
    op.create_index("ix_automation_runs_rule", "automation_runs", ["automation_id", "created_at"])
    op.create_table(
        "automation_run_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("action_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=48), nullable=True),
        sa.Column("result_reference", sa.String(length=256), nullable=True),
        sa.Column("approval_hash", sa.String(length=64), nullable=True),
        sa.Column("destination_revision", sa.String(length=64), nullable=True),
        sa.Column("approval_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_session_hash", sa.String(length=64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("ordinal BETWEEN 1 AND 10", name="ck_automation_run_actions_ordinal"),
        sa.CheckConstraint(
            "status IN ('pending','awaiting_approval','approved','in_flight','succeeded','failed',"
            "'denied','skipped','requires_review')", name="ck_automation_run_actions_status"),
        sa.ForeignKeyConstraint(["run_id"], ["automation_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "ordinal", name="uq_automation_run_actions_slot"),
    )
    # Supporting indexes for the producer cursor sweeps (none of these columns is indexed by its owner).
    op.create_index("ix_event_outbox_type_created", "event_outbox", ["type", "created_at", "id"])
    op.create_index("ix_ingestion_runs_updated_id", "ingestion_runs", ["updated_at", "id"])
    op.create_index("ix_timeline_events_updated_id", "timeline_events", ["updated_at", "id"])
    op.create_index("ix_entities_updated_id", "entities", ["updated_at", "id"])
    op.create_index("ix_automation_run_actions_reference", "automation_run_actions", ["result_reference"])
    op.create_table(
        "automation_cursors",
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("name"),
    )
    _brief_schedule_owner()


def _brief_schedule_owner() -> None:
    """Add the daily_brief schedule-ownership record to the P08 schedule row (default: internal cron)."""
    op.add_column("brief_schedules", sa.Column(
        "schedule_owner", sa.String(length=16), server_default="internal_brief", nullable=False))
    op.add_column("brief_schedules", sa.Column("automation_id", sa.Uuid(), nullable=True))
    op.create_check_constraint(
        "ck_brief_schedules_owner", "brief_schedules",
        "schedule_owner IN ('internal_brief','automation') AND "
        "((schedule_owner = 'automation') = (automation_id IS NOT NULL))")


def downgrade() -> None:
    """Drop dispatch tables children first."""
    op.drop_constraint("ck_brief_schedules_owner", "brief_schedules", type_="check")
    op.drop_column("brief_schedules", "automation_id")
    op.drop_column("brief_schedules", "schedule_owner")
    op.drop_index("ix_entities_updated_id", table_name="entities")
    op.drop_index("ix_timeline_events_updated_id", table_name="timeline_events")
    op.drop_index("ix_ingestion_runs_updated_id", table_name="ingestion_runs")
    op.drop_index("ix_event_outbox_type_created", table_name="event_outbox")
    op.drop_table("automation_cursors")
    op.drop_index("ix_automation_run_actions_reference", table_name="automation_run_actions")
    op.drop_table("automation_run_actions")
    op.drop_index("ix_automation_runs_rule", table_name="automation_runs")
    op.drop_index("ix_automation_runs_dispatch", table_name="automation_runs")
    op.drop_table("automation_runs")
    op.drop_index("ix_automation_schedules_next", table_name="automation_schedules")
    op.drop_table("automation_schedules")
    op.drop_index("ix_automation_triggers_pending", table_name="automation_triggers")
    op.drop_table("automation_triggers")
