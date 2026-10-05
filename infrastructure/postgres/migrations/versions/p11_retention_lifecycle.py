"""Add revisioned retention/module settings, bounded maintenance summary, and agent trace cutoff index."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "p11_retention_lifecycle"
down_revision: str | Sequence[str] | None = "p10_automation_webhook_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Persist owner controls and bounded cleanup evidence without rewriting source/history data."""
    op.add_column("agent_runs", sa.Column("trace_redacted_at", sa.DateTime(timezone=True)))
    op.create_index("ix_agent_runs_trace_retention", "agent_runs", ["trace_redacted_at", "status", "completed_at", "id"])
    op.create_index("ix_agent_approvals_retention", "agent_approvals", ["run_id", "status"])
    op.create_index("ix_agent_effects_retention", "agent_effects", ["run_id", "state"])
    op.create_index("ix_browser_read_jobs_retention", "browser_read_jobs", ["status", "expires_at", "id"])
    op.create_table(
        "retention_settings",
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("owner.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("configuration_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("agent_trace_days", sa.Integer(), nullable=False, server_default="90"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("owner_id = 1", name="ck_retention_settings_single_owner"),
        sa.CheckConstraint("configuration_revision > 0", name="ck_retention_settings_revision"),
        sa.CheckConstraint("agent_trace_days BETWEEN 1 AND 3650", name="ck_retention_settings_trace_days"),
    )
    op.create_table(
        "module_lifecycle_settings",
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("owner.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("configuration_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("disabled_modules", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("owner_id = 1", name="ck_module_lifecycle_single_owner"),
        sa.CheckConstraint("configuration_revision > 0", name="ck_module_lifecycle_revision"),
    )
    op.create_table(
        "maintenance_summary",
        sa.Column("id", sa.SmallInteger(), primary_key=True, server_default="1"),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("agent_traces_redacted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("temporary_data_deleted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_eligible_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("id = 1", name="ck_maintenance_summary_singleton"),
        sa.CheckConstraint("agent_traces_redacted >= 0 AND temporary_data_deleted >= 0", name="ck_maintenance_summary_counts"),
    )


def downgrade() -> None:
    """Remove P11 controls and summary without changing retained owner records."""
    op.drop_table("maintenance_summary")
    op.drop_table("module_lifecycle_settings")
    op.drop_table("retention_settings")
    op.drop_index("ix_browser_read_jobs_retention", table_name="browser_read_jobs")
    op.drop_index("ix_agent_effects_retention", table_name="agent_effects")
    op.drop_index("ix_agent_approvals_retention", table_name="agent_approvals")
    op.drop_index("ix_agent_runs_trace_retention", table_name="agent_runs")
    op.drop_column("agent_runs", "trace_redacted_at")
