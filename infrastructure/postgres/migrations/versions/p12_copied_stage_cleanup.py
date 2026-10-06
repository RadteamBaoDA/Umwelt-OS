"""Add copied-evidence owner schema and the Documents materialization/saved-brief cleanup stages.

One serialized descendant of ``p12_agent_document_cleanup``. Historical rows get truthful
defaults: legacy sidecars are NULL (unavailable lineage, never proven clean), revoked flags are
false because nothing was ever revoked, legacy briefs carry no capture manifest, and historical
cleanup receipts start both new stages ``queued`` so owners re-evaluate them honestly.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "p12_copied_stage_cleanup"
down_revision: str | Sequence[str] | None = "p12_agent_document_cleanup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STAGES = ("materialization", "brief")


def upgrade() -> None:
    """Add owner sidecars/manifest tables and receipt stage columns without executing backfills."""
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    uuid_type = postgresql.UUID(as_uuid=True)
    false = sa.text("false")

    # Notifications: private copied-highlight provenance; NULL means legacy/unproven lineage.
    op.add_column("notifications", sa.Column("document_id", uuid_type, nullable=True))
    op.add_column("notifications", sa.Column("document_version_id", uuid_type, nullable=True))
    op.add_column("notifications", sa.Column(
        "copied_evidence_revoked", sa.Boolean(), server_default=false, nullable=False,
    ))
    op.create_index("ix_notifications_copied_document", "notifications", ["document_id", "id"])

    # Automations: private provenance sidecars outside the condition payload whitelist.
    for table in ("automation_triggers", "automation_runs"):
        op.add_column(table, sa.Column("document_id", uuid_type, nullable=True))
        op.add_column(table, sa.Column("document_version_id", uuid_type, nullable=True))
        op.add_column(table, sa.Column(
            "document_evidence_revoked", sa.Boolean(), server_default=false, nullable=False,
        ))
    op.create_index("ix_automation_triggers_document", "automation_triggers", ["document_id", "id"])
    op.create_index("ix_automation_runs_document", "automation_runs", ["document_id", "id"])

    # Dashboard: capture manifest on briefs. All three NULL marks a legacy brief with no known
    # prompt lineage; only the owner's new generation path writes version 1.
    op.add_column("daily_briefs", sa.Column("evidence_capture_version", sa.Integer(), nullable=True))
    op.add_column("daily_briefs", sa.Column("evidence_capture_status", sa.String(16), nullable=True))
    op.add_column("daily_briefs", sa.Column("evidence_fact_count", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_daily_briefs_evidence_capture", "daily_briefs",
        "(evidence_capture_version IS NULL AND evidence_capture_status IS NULL AND evidence_fact_count IS NULL) OR "
        "(evidence_capture_version = 1 AND evidence_capture_status IN ('captured','unavailable') "
        "AND evidence_fact_count BETWEEN 1 AND 40)",
    )
    op.create_table(
        "daily_brief_evidence",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("brief_id", uuid_type, sa.ForeignKey("daily_briefs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fact_ref", sa.Integer(), nullable=False),
        sa.Column("fact_kind", sa.String(16), nullable=False),
        sa.Column("fact_id", sa.String(64), nullable=False),
        sa.Column("fact_hash", sa.String(64), nullable=False),
        sa.Column("support_index", sa.Integer(), nullable=False),
        # No FKs to canonical evidence: these identities must outlive Document cascades.
        sa.Column("document_id", uuid_type, nullable=True),
        sa.Column("document_version_id", uuid_type, nullable=True),
        sa.Column("chunk_id", uuid_type, nullable=True),
        sa.Column("source_id", uuid_type, nullable=True),
        sa.CheckConstraint("fact_ref BETWEEN 1 AND 40", name="ck_daily_brief_evidence_fact_ref"),
        sa.CheckConstraint("support_index BETWEEN 0 AND 99", name="ck_daily_brief_evidence_support_index"),
        sa.CheckConstraint(
            "fact_kind IN ('tasks','goals','stories','events')", name="ck_daily_brief_evidence_fact_kind",
        ),
        sa.CheckConstraint("fact_hash ~ '^[0-9a-f]{64}$'", name="ck_daily_brief_evidence_fact_hash"),
        sa.CheckConstraint(
            "(document_id IS NULL AND document_version_id IS NULL AND chunk_id IS NULL AND source_id IS NULL) OR "
            "(document_id IS NOT NULL AND document_version_id IS NOT NULL AND chunk_id IS NOT NULL AND source_id IS NOT NULL)",
            name="ck_daily_brief_evidence_document_tuple",
        ),
        sa.UniqueConstraint("brief_id", "fact_ref", "support_index", name="uq_daily_brief_evidence_fact_support"),
    )
    op.create_index(
        "ix_daily_brief_evidence_document_brief", "daily_brief_evidence", ["document_id", "brief_id"],
    )

    # Documents receipt: dedicated stages; the aggregate may now finally report succeeded.
    op.drop_constraint("ck_document_cleanup_copied_status", "document_cleanup_operations", type_="check")
    op.create_check_constraint(
        "ck_document_cleanup_copied_status", "document_cleanup_operations",
        "copied_status IN ('queued', 'running', 'succeeded', 'failed')",
    )
    for stage in _STAGES:
        table = "document_cleanup_operations"
        op.add_column(table, sa.Column(f"{stage}_status", sa.String(16), server_default="queued", nullable=False))
        op.add_column(table, sa.Column(f"{stage}_error_code", sa.String(64), nullable=True))
        op.add_column(table, sa.Column(f"{stage}_cursor", jsonb, nullable=True))
        op.add_column(table, sa.Column(
            f"{stage}_unresolved_count", sa.Integer(), server_default="0", nullable=False,
        ))
        op.create_check_constraint(
            f"ck_document_cleanup_{stage}_status", table,
            f"{stage}_status IN ('queued', 'running', 'succeeded', 'failed')",
        )
        op.create_check_constraint(
            f"ck_document_cleanup_{stage}_cursor_bound", table,
            f"{stage}_cursor IS NULL OR octet_length({stage}_cursor::text) <= 4096",
        )
        op.create_check_constraint(
            f"ck_document_cleanup_{stage}_unresolved_nonnegative", table, f"{stage}_unresolved_count >= 0",
        )
    # Earliest version time bounds which legacy (manifest-less) briefs could mention the Document;
    # NULL (historical receipts) keeps the conservative count-all behavior.
    op.add_column("document_cleanup_operations", sa.Column(
        "earliest_version_created_at", sa.DateTime(timezone=True), nullable=True,
    ))
    op.create_index(
        "ix_document_cleanup_copied_stages_reconcile", "document_cleanup_operations", ["id"],
        postgresql_where=sa.text(
            "materialization_status IN ('queued', 'running') OR brief_status IN ('queued', 'running')"
        ),
    )


def downgrade() -> None:
    """Remove the unshipped copied-stage schema; rows already sanitized stay sanitized."""
    table = "document_cleanup_operations"
    op.drop_index("ix_document_cleanup_copied_stages_reconcile", table_name=table)
    op.drop_column(table, "earliest_version_created_at")
    for stage in _STAGES:
        op.drop_constraint(f"ck_document_cleanup_{stage}_unresolved_nonnegative", table, type_="check")
        op.drop_constraint(f"ck_document_cleanup_{stage}_cursor_bound", table, type_="check")
        op.drop_constraint(f"ck_document_cleanup_{stage}_status", table, type_="check")
        op.drop_column(table, f"{stage}_unresolved_count")
        op.drop_column(table, f"{stage}_cursor")
        op.drop_column(table, f"{stage}_error_code")
        op.drop_column(table, f"{stage}_status")
    # Rows aggregated as succeeded cannot satisfy the older constraint; fold them back to running.
    op.execute("UPDATE document_cleanup_operations SET copied_status = 'running' WHERE copied_status = 'succeeded'")
    op.execute("UPDATE document_cleanup_operations SET status = 'running' WHERE status = 'succeeded' AND copied_status = 'running'")
    op.drop_constraint("ck_document_cleanup_copied_status", table, type_="check")
    op.create_check_constraint(
        "ck_document_cleanup_copied_status", table, "copied_status IN ('queued', 'running', 'failed')",
    )
    op.drop_index("ix_daily_brief_evidence_document_brief", table_name="daily_brief_evidence")
    op.drop_table("daily_brief_evidence")
    op.drop_constraint("ck_daily_briefs_evidence_capture", "daily_briefs", type_="check")
    op.drop_column("daily_briefs", "evidence_fact_count")
    op.drop_column("daily_briefs", "evidence_capture_status")
    op.drop_column("daily_briefs", "evidence_capture_version")
    op.drop_index("ix_automation_runs_document", table_name="automation_runs")
    op.drop_index("ix_automation_triggers_document", table_name="automation_triggers")
    for name in ("automation_runs", "automation_triggers"):
        op.drop_column(name, "document_evidence_revoked")
        op.drop_column(name, "document_version_id")
        op.drop_column(name, "document_id")
    op.drop_index("ix_notifications_copied_document", table_name="notifications")
    op.drop_column("notifications", "copied_evidence_revoked")
    op.drop_column("notifications", "document_version_id")
    op.drop_column("notifications", "document_id")
