"""Add durable collection schedules, requests and global admission slots.

Backfills one schedule per existing provisioned source. Native dispatch never sees n8n rows, so
an n8n schedule is enabled only when its source is already active, desired-enabled and has a
workflow (n8n already owns that trigger): nothing new is activated and managed admission is not
locked out. Existing provisioning rows keep the n8n backend at backend_revision 1.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "p14_collection"
down_revision: str | Sequence[str] | None = "p14_workspace_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create collection tables, extend provisioning, seed slots 1 and 2 and backfill schedules."""
    op.add_column("connector_provisioning", sa.Column(
        "execution_backend", sa.String(16), nullable=False, server_default="n8n"))
    op.add_column("connector_provisioning", sa.Column(
        "backend_revision", sa.Integer(), nullable=False, server_default="1"))
    op.create_check_constraint(
        "ck_connector_provisioning_backend", "connector_provisioning", "execution_backend IN ('native', 'n8n')")
    op.create_check_constraint(
        "ck_connector_provisioning_backend_revision", "connector_provisioning", "backend_revision > 0")

    op.create_table(
        "connector_schedules",
        sa.Column("source_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("interval_minutes", sa.Integer(), nullable=False),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_dispatch_at", sa.DateTime(timezone=True)),
        sa.Column("last_considered_at", sa.DateTime(timezone=True)),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_eligible_at", sa.DateTime(timezone=True)),
        sa.Column("blocked_error_code", sa.String(64)),
        sa.Column("blocked_dimensions", postgresql.ARRAY(sa.String(16))),
        sa.Column("blocked_connector_revision", sa.Integer()),
        sa.Column("blocked_credential_revision", sa.Integer()),
        sa.Column("blocked_terms_revision", sa.Integer()),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"],
                                name="fk_connector_schedules_workspace", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workspace_id", "source_id"], ["sources.workspace_id", "sources.id"],
                                name="fk_connector_schedules_source", ondelete="CASCADE"),
        sa.CheckConstraint("interval_minutes IN (15, 30, 60, 360, 1440)", name="ck_connector_schedules_interval"),
        sa.CheckConstraint("failure_count >= 0", name="ck_connector_schedules_failures"),
    )
    op.create_index("ix_connector_schedules_due", "connector_schedules", ["enabled", "next_due_at"])
    op.create_index("ix_connector_schedules_workspace", "connector_schedules", ["workspace_id", "last_considered_at"])
    op.create_index(
        "ix_connector_schedules_eligible", "connector_schedules", ["enabled", "next_eligible_at", "next_due_at"])

    op.create_table(
        "connector_collection_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=False),
        sa.Column("membership_revision", sa.Integer(), nullable=False),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("source_generation", sa.Integer(), nullable=False),
        sa.Column("connector_revision", sa.Integer(), nullable=False),
        sa.Column("backend_revision", sa.Integer(), nullable=False),
        sa.Column("captured_backend", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active_admission_token", postgresql.UUID(as_uuid=True)),
        sa.Column("access_configuration_revision", sa.Integer()),
        sa.Column("template_revision", sa.Integer()),
        sa.Column("credential_revision", sa.Integer()),
        sa.Column("terms_revision", sa.Integer()),
        sa.Column("source_lease_token", postgresql.UUID(as_uuid=True)),
        sa.Column("attempt_started_at", sa.DateTime(timezone=True)),
        sa.Column("attempt_deadline_at", sa.DateTime(timezone=True)),
        sa.Column("accepted_receipt_id", postgresql.UUID(as_uuid=True)),
        sa.Column("wake_next_at", sa.DateTime(timezone=True)),
        sa.Column("wake_claim_token", postgresql.UUID(as_uuid=True)),
        sa.Column("wake_claim_expires_at", sa.DateTime(timezone=True)),
        sa.Column("wake_attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ingestion_run_id", postgresql.UUID(as_uuid=True)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("provider_deadline", sa.DateTime(timezone=True)),
        sa.Column("enqueue_next_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("enqueue_claim_token", postgresql.UUID(as_uuid=True)),
        sa.Column("enqueue_claim_expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"],
                                name="fk_connector_collection_requests_workspace", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workspace_id", "source_id"], ["sources.workspace_id", "sources.id"],
                                name="fk_connector_collection_requests_source", ondelete="CASCADE"),
        sa.CheckConstraint("trigger IN ('manual', 'scheduled', 'retry')",
                           name="ck_connector_collection_requests_trigger"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'no_changes', 'failed', 'cancelled')",
            name="ck_connector_collection_requests_status"),
        sa.CheckConstraint("captured_backend IN ('native', 'n8n')", name="ck_connector_collection_requests_backend"),
        sa.CheckConstraint("attempt BETWEEN 0 AND 5", name="ck_connector_collection_requests_attempt"),
        sa.CheckConstraint("(status = 'running') = (active_admission_token IS NOT NULL)",
                           name="ck_connector_collection_requests_admission"),
    )
    op.create_index(
        "uq_connector_collection_requests_active", "connector_collection_requests", ["source_id"], unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"))
    op.create_index(
        "ix_connector_collection_requests_enqueue", "connector_collection_requests",
        ["available_at", "enqueue_next_at"], postgresql_where=sa.text("status = 'queued'"))
    op.create_index(
        "ix_connector_collection_requests_workspace", "connector_collection_requests",
        ["workspace_id", "source_id", "created_at"])
    op.create_index(
        "uq_connector_collection_requests_receipt", "connector_collection_requests", ["accepted_receipt_id"],
        unique=True, postgresql_where=sa.text("accepted_receipt_id IS NOT NULL"))

    op.create_table(
        "connector_admission_slots",
        sa.Column("slot_id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("occupied_request_id", postgresql.UUID(as_uuid=True)),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True)),
        sa.Column("admission_token", postgresql.UUID(as_uuid=True)),
        sa.Column("lease_kind", sa.String(16)),
        sa.Column("source_owner_id", postgresql.UUID(as_uuid=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"],
                                name="fk_connector_admission_slots_workspace", ondelete="RESTRICT"),
        sa.CheckConstraint("slot_id IN (1, 2)", name="ck_connector_admission_slots_id"),
        sa.CheckConstraint("lease_kind IN ('collection', 'run')", name="ck_connector_admission_slots_lease_kind"),
        sa.CheckConstraint(
            "(occupied_request_id IS NULL) = (lease_kind IS NULL) "
            "AND (occupied_request_id IS NULL) = (workspace_id IS NULL) "
            "AND (occupied_request_id IS NULL) = (admission_token IS NULL) "
            "AND (occupied_request_id IS NULL) = (expires_at IS NULL)",
            name="ck_connector_admission_slots_occupancy"),
    )
    op.create_index(
        "uq_connector_admission_slots_request", "connector_admission_slots", ["occupied_request_id"], unique=True,
        postgresql_where=sa.text("occupied_request_id IS NOT NULL"))
    op.create_index(
        "uq_connector_admission_slots_workspace", "connector_admission_slots", ["workspace_id"], unique=True,
        postgresql_where=sa.text("workspace_id IS NOT NULL"))
    op.execute("INSERT INTO connector_admission_slots (slot_id) VALUES (1), (2)")

    op.create_table(
        "connector_workspace_dispatch",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("last_considered_at", sa.DateTime(timezone=True)),
        sa.Column("last_dispatched_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"],
                                name="connector_workspace_dispatch_workspace_id_fkey", ondelete="CASCADE"),
    )

    # Cadence comes from stored configuration. Only already-running n8n triggers stay enabled.
    op.execute("""
        INSERT INTO connector_schedules (source_id, workspace_id, enabled, interval_minutes, next_due_at)
        SELECT s.id, s.workspace_id,
               (p.state = 'active' AND p.desired_enabled AND p.workflow_id IS NOT NULL),
               CASE WHEN s.configuration ->> 'schedule_interval_minutes' IN ('15', '30', '60', '360', '1440')
                    THEN (s.configuration ->> 'schedule_interval_minutes')::int
                    WHEN s.type = 'rss' THEN 15 ELSE 30 END,
               now()
        FROM connector_provisioning p JOIN sources s ON s.id = p.source_id
    """)


def downgrade() -> None:
    """Drop collection tables and provisioning backend columns; durable requests are discarded."""
    op.drop_table("connector_workspace_dispatch")
    op.drop_table("connector_admission_slots")
    op.drop_table("connector_collection_requests")
    op.drop_table("connector_schedules")
    op.drop_constraint("ck_connector_provisioning_backend_revision", "connector_provisioning", type_="check")
    op.drop_constraint("ck_connector_provisioning_backend", "connector_provisioning", type_="check")
    op.drop_column("connector_provisioning", "backend_revision")
    op.drop_column("connector_provisioning", "execution_backend")
