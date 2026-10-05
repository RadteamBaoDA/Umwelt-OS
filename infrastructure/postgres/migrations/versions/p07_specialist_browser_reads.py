"""Add bounded browser-read storage: remote-heavy guards, per-source grants, read jobs, page evidence and run budget columns."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "p07_specialist_browser_reads"
down_revision: str | Sequence[str] | None = "p07_specialist_profiles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create run browser-budget columns, the remote-heavy guard, browser grants, read jobs and page evidence.

    A guard blocks heavy admission only until its expires_at, which sits just past the remote job's
    hard runtime bound; an elapsed guard no longer blocks, so a lost job cannot wedge admission.
    """
    op.add_column("agent_runs", sa.Column("browser_jobs", sa.Integer(), server_default="0", nullable=False))
    op.add_column("agent_runs", sa.Column("browser_pages", sa.Integer(), server_default="0", nullable=False))
    op.add_column("agent_runs", sa.Column("browser_bytes", sa.Integer(), server_default="0", nullable=False))
    op.add_column("agent_runs", sa.Column("browser_budget_reservations", postgresql.JSONB(), server_default="{}", nullable=False))
    op.create_check_constraint(
        "ck_agent_runs_browser_budget", "agent_runs",
        "browser_jobs BETWEEN 0 AND 2 AND browser_pages BETWEEN 0 AND 6 AND browser_bytes BETWEEN 0 AND 10485760",
    )
    op.create_table(
        "remote_heavy_guards",
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("service_instance_id", sa.String(length=128), nullable=False),
        sa.Column("remote_job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("nonce_hash", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "state IN ('active', 'uncertain', 'cleared')",
            name="ck_remote_heavy_guards_state",
        ),
    )
    op.create_index(
        "ix_remote_heavy_guards_blocking",
        "remote_heavy_guards",
        ["state", "created_at"],
    )
    op.create_table(
        "agent_browser_grants",
        sa.Column("source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("source_generation", sa.Integer(), nullable=False),
        sa.Column("connector_revision", sa.Integer(), nullable=False),
        sa.Column("grant_revision", sa.Integer(), nullable=False),
        sa.Column("scope_hash", sa.String(length=64), nullable=False),
        sa.Column("origin", sa.String(length=512), nullable=False),
        sa.Column("path_prefix", sa.String(length=2048), nullable=False),
        sa.Column("local_only", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("owner_id = 1", name="ck_agent_browser_grants_single_owner"),
        sa.CheckConstraint("source_generation > 0 AND connector_revision > 0", name="ck_agent_browser_grants_fences"),
        sa.CheckConstraint("grant_revision > 0", name="ck_agent_browser_grants_revision"),
    )
    op.create_index(
        "ix_agent_browser_grants_owner_enabled",
        "agent_browser_grants",
        ["owner_id", "enabled"],
    )
    op.create_table(
        "browser_read_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tool_slot", sa.Integer(), nullable=False),
        sa.Column("auth_session_hash", sa.String(length=64), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("profile_id", sa.String(length=24), nullable=False),
        sa.Column("authorized_source_ids", postgresql.JSONB(), nullable=False),
        sa.Column("profile_revision_hash", sa.String(length=64), nullable=False),
        sa.Column("claim_generation", sa.Integer(), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_generation", sa.Integer(), nullable=False),
        sa.Column("connector_revision", sa.Integer(), nullable=False),
        sa.Column("grant_revision", sa.Integer(), nullable=False),
        sa.Column("scope_hash", sa.String(length=64), nullable=False),
        sa.Column("arguments_hash", sa.String(length=64), nullable=False),
        sa.Column("max_pages", sa.Integer(), nullable=False),
        sa.Column("max_bytes", sa.Integer(), nullable=False, server_default="5242880"),
        sa.Column("max_active_seconds", sa.Integer(), nullable=False, server_default="45"),
        sa.Column("actual_pages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("actual_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("request_ordinal", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("service_instance_id", sa.String(length=128)),
        sa.Column("service_token_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="queued"),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("result_hash", sa.String(length=64)),
        sa.Column("error_code", sa.String(length=32)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("owner_id = 1", name="ck_browser_read_jobs_single_owner"),
        sa.CheckConstraint("tool_slot BETWEEN 1 AND 10", name="ck_browser_read_jobs_slot"),
        sa.CheckConstraint("claim_generation > 0 AND source_generation > 0 AND connector_revision > 0", name="ck_browser_read_jobs_fences"),
        sa.CheckConstraint("grant_revision > 0 AND max_pages BETWEEN 1 AND 3", name="ck_browser_read_jobs_limits"),
        sa.CheckConstraint("status IN ('queued','running','succeeded','cancel_requested','cancelled','failed','uncertain','expired')", name="ck_browser_read_jobs_status"),
        sa.CheckConstraint("actual_pages BETWEEN 0 AND 3 AND actual_bytes BETWEEN 0 AND 5242880", name="ck_browser_read_jobs_usage"),
        sa.UniqueConstraint("run_id", "tool_slot", name="uq_browser_read_jobs_run_slot"),
    )
    op.create_index("ix_browser_read_jobs_owner_expiry", "browser_read_jobs", ["owner_id", "expires_at"])
    op.create_table(
        "browser_page_evidence",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("browser_read_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("requested_url", sa.String(length=2048), nullable=False),
        sa.Column("final_url", sa.String(length=2048), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_digest", sa.String(length=64), nullable=False),
        sa.Column("raw_content", sa.LargeBinary(), nullable=False),
        sa.Column("extracted_text", sa.Text(), nullable=False),
        sa.CheckConstraint("page_number BETWEEN 1 AND 3", name="ck_browser_page_evidence_number"),
        sa.CheckConstraint("octet_length(raw_content) <= 5242880", name="ck_browser_page_evidence_raw_bytes"),
        sa.CheckConstraint("octet_length(extracted_text) <= 20000", name="ck_browser_page_evidence_text_bytes"),
        sa.UniqueConstraint("job_id", "page_number", name="uq_browser_page_evidence_job_page"),
    )


def downgrade() -> None:
    """Drop the browser evidence, jobs, grants, remote-heavy guard table and run budget columns added by upgrade."""
    op.drop_table("browser_page_evidence")
    op.drop_index("ix_browser_read_jobs_owner_expiry", table_name="browser_read_jobs")
    op.drop_table("browser_read_jobs")
    op.drop_index("ix_agent_browser_grants_owner_enabled", table_name="agent_browser_grants")
    op.drop_table("agent_browser_grants")
    op.drop_index("ix_remote_heavy_guards_blocking", table_name="remote_heavy_guards")
    op.drop_table("remote_heavy_guards")
    op.drop_constraint("ck_agent_runs_browser_budget", "agent_runs", type_="check")
    op.drop_column("agent_runs", "browser_budget_reservations")
    op.drop_column("agent_runs", "browser_bytes")
    op.drop_column("agent_runs", "browser_pages")
    op.drop_column("agent_runs", "browser_jobs")
