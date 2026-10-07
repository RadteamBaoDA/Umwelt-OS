"""Add Agent copied-evidence receipts and the Documents Agent cleanup stage."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "p12_agent_document_cleanup"
down_revision: str | Sequence[str] | None = "p12_memory_document_cleanup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add durable Agent revocation/finalization state and Documents receipt progress."""
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    uuid_type = postgresql.UUID(as_uuid=True)
    op.add_column("agent_runs", sa.Column(
        "evidence_revoked", sa.Boolean(), server_default=sa.text("false"), nullable=False,
    ))
    op.add_column("agent_tool_calls", sa.Column("input_source_fences", jsonb, nullable=True))
    op.add_column("agent_tool_calls", sa.Column("input_provenance_version", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_agent_tool_calls_input_provenance_version", "agent_tool_calls",
        "input_provenance_version IS NULL OR input_provenance_version = 1",
    )
    op.create_check_constraint(
        "ck_agent_tool_calls_input_provenance_shape", "agent_tool_calls",
        "(input_provenance_version IS NULL AND input_source_fences IS NULL) OR "
        "(input_provenance_version = 1 AND input_source_fences IS NOT NULL "
        "AND octet_length(input_source_fences::text) <= 64000)",
    )
    op.create_index(
        "ix_agent_runs_source_fences_gin", "agent_runs", ["source_fences"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_agent_tool_calls_input_fences_gin", "agent_tool_calls", ["input_source_fences"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_agent_approvals_source_fences_gin", "agent_approvals", ["source_fences"],
        postgresql_using="gin",
    )
    op.create_table(
        "agent_evidence_cleanups",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("operation_id", uuid_type, nullable=False),
        sa.Column("run_id", uuid_type, sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_id", uuid_type, nullable=False),
        sa.Column("document_id", uuid_type, nullable=False),
        sa.Column("scope_fingerprint", sa.String(64), nullable=False),
        sa.Column("matched_identity", jsonb, nullable=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finalized_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("operation_id", "run_id", name="uq_agent_evidence_cleanups_operation_run"),
        sa.CheckConstraint("state IN ('pending','finalized','unavailable')", name="ck_agent_evidence_cleanups_state"),
        sa.CheckConstraint(
            "matched_identity IS NULL OR octet_length(matched_identity::text) <= 2048",
            name="ck_agent_evidence_cleanups_identity_bytes",
        ),
    )
    op.create_index(
        "ix_agent_evidence_cleanups_operation_state_run", "agent_evidence_cleanups",
        ["operation_id", "state", "run_id"],
    )

    op.add_column("document_cleanup_operations", sa.Column(
        "agent_status", sa.String(16), server_default="queued", nullable=False,
    ))
    op.add_column("document_cleanup_operations", sa.Column("agent_error_code", sa.String(64)))
    op.add_column("document_cleanup_operations", sa.Column("agent_cursor", jsonb, nullable=True))
    op.add_column("document_cleanup_operations", sa.Column(
        "agent_unresolved_count", sa.Integer(), server_default="0", nullable=False,
    ))
    op.add_column("document_cleanup_operations", sa.Column(
        "agent_waiting_for_lease", sa.Boolean(), server_default=sa.text("false"), nullable=False,
    ))
    op.create_check_constraint(
        "ck_document_cleanup_agent_status", "document_cleanup_operations",
        "agent_status IN ('queued', 'running', 'succeeded', 'failed')",
    )
    op.create_check_constraint(
        "ck_document_cleanup_agent_cursor_bound", "document_cleanup_operations",
        "agent_cursor IS NULL OR octet_length(agent_cursor::text) <= 4096",
    )
    op.create_check_constraint(
        "ck_document_cleanup_agent_unresolved_nonnegative", "document_cleanup_operations",
        "agent_unresolved_count >= 0",
    )
    op.create_index(
        "ix_document_cleanup_agent_reconcile", "document_cleanup_operations",
        ["id"], postgresql_where=sa.text("agent_status IN ('queued', 'running')"),
    )
def downgrade() -> None:
    """Remove only the unshipped Agent cleanup stage metadata."""
    op.drop_index("ix_document_cleanup_agent_reconcile", table_name="document_cleanup_operations")
    op.drop_constraint("ck_document_cleanup_agent_unresolved_nonnegative", "document_cleanup_operations", type_="check")
    op.drop_constraint("ck_document_cleanup_agent_cursor_bound", "document_cleanup_operations", type_="check")
    op.drop_constraint("ck_document_cleanup_agent_status", "document_cleanup_operations", type_="check")
    op.drop_column("document_cleanup_operations", "agent_waiting_for_lease")
    op.drop_column("document_cleanup_operations", "agent_unresolved_count")
    op.drop_column("document_cleanup_operations", "agent_cursor")
    op.drop_column("document_cleanup_operations", "agent_error_code")
    op.drop_column("document_cleanup_operations", "agent_status")
    op.drop_index("ix_agent_evidence_cleanups_operation_state_run", table_name="agent_evidence_cleanups")
    op.drop_table("agent_evidence_cleanups")
    op.drop_index("ix_agent_approvals_source_fences_gin", table_name="agent_approvals")
    op.drop_index("ix_agent_tool_calls_input_fences_gin", table_name="agent_tool_calls")
    op.drop_index("ix_agent_runs_source_fences_gin", table_name="agent_runs")
    op.drop_constraint("ck_agent_tool_calls_input_provenance_shape", "agent_tool_calls", type_="check")
    op.drop_constraint("ck_agent_tool_calls_input_provenance_version", "agent_tool_calls", type_="check")
    op.drop_column("agent_tool_calls", "input_provenance_version")
    op.drop_column("agent_tool_calls", "input_source_fences")
    op.drop_column("agent_runs", "evidence_revoked")
