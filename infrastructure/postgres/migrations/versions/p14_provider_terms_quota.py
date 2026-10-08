"""Add per-source provider terms evidence and the durable provider quota ledger.

Additive only: four new tables, no backfill (a source without a terms row is denied for catalog
providers, i.e. deny by default). Quota windows carry no workspace component because free-key and
shared-egress budgets span the deployment.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "p14_provider_terms_quota"
down_revision: str | Sequence[str] | None = "p14_cleanup_authority"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WINDOW_KEY = ("provider_id", "budget_kind", "subject_hash", "policy_key", "window_start")


def upgrade() -> None:
    """Create terms, quota window, send and debit tables."""
    op.create_table(
        "connector_provider_terms",
        sa.Column("source_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column("terms_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("terms_url", sa.String(2048), nullable=False),
        sa.Column("terms_version", sa.String(64), nullable=False),
        sa.Column("checked_on", sa.Date(), nullable=False),
        sa.Column("owner_acknowledged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("owner_actor_user_id", sa.Integer(), nullable=False),
        sa.Column("declared_use", sa.String(16), nullable=False),
        sa.Column("operator_review_state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("reviewer_user_id", sa.Integer()),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("reviewed_allowed_use", sa.String(16)),
        sa.Column("review_evidence_ref", sa.String(512)),
        sa.Column("reviewed_terms_version", sa.String(64)),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"],
                                name="fk_connector_provider_terms_workspace", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workspace_id", "source_id"], ["sources.workspace_id", "sources.id"],
                                name="fk_connector_provider_terms_source", ondelete="CASCADE"),
        sa.CheckConstraint("terms_revision > 0", name="ck_connector_provider_terms_revision"),
        sa.CheckConstraint("declared_use IN ('personal', 'noncommercial', 'commercial', 'unknown')",
                           name="ck_connector_provider_terms_use"),
        sa.CheckConstraint("operator_review_state IN ('pending', 'approved', 'rejected')",
                           name="ck_connector_provider_terms_review_state"),
        sa.CheckConstraint(
            "reviewed_allowed_use IS NULL OR reviewed_allowed_use IN ('personal', 'noncommercial', 'commercial')",
            name="ck_connector_provider_terms_reviewed_use"),
        sa.CheckConstraint(
            "(operator_review_state = 'pending' AND reviewer_user_id IS NULL AND reviewed_at IS NULL "
            "AND reviewed_allowed_use IS NULL AND review_evidence_ref IS NULL AND reviewed_terms_version IS NULL) "
            "OR (operator_review_state = 'rejected' AND reviewer_user_id IS NOT NULL AND reviewed_at IS NOT NULL "
            "AND review_evidence_ref IS NOT NULL) "
            "OR (operator_review_state = 'approved' AND reviewer_user_id IS NOT NULL AND reviewed_at IS NOT NULL "
            "AND reviewed_allowed_use IS NOT NULL AND review_evidence_ref IS NOT NULL "
            "AND reviewed_terms_version IS NOT NULL)",
            name="ck_connector_provider_terms_review_fields"),
    )
    op.create_index("ix_connector_provider_terms_workspace", "connector_provider_terms",
                    ["workspace_id", "source_id"])

    op.create_table(
        "connector_quota_windows",
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column("budget_kind", sa.String(16), nullable=False),
        sa.Column("subject_hash", sa.String(64), nullable=False),
        sa.Column("policy_key", sa.String(64), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("unit", sa.String(24), nullable=False),
        sa.Column("limit_units", sa.BigInteger()),
        sa.Column("used_units", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("blocked_until", sa.DateTime(timezone=True)),
        sa.Column("policy_revision", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint(*_WINDOW_KEY),
        sa.CheckConstraint("budget_kind IN ('provider', 'credential', 'ip')", name="ck_connector_quota_windows_kind"),
        sa.CheckConstraint("used_units >= 0", name="ck_connector_quota_windows_used"),
        sa.CheckConstraint("limit_units IS NULL OR limit_units >= 0", name="ck_connector_quota_windows_limit"),
        sa.CheckConstraint("window_end > window_start", name="ck_connector_quota_windows_span"),
    )
    op.create_index("ix_connector_quota_windows_end", "connector_quota_windows", ["window_end"])

    op.create_table(
        "connector_provider_sends",
        sa.Column("send_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("admission_token", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("send_sequence", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column("request_target_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"],
                                name="fk_connector_provider_sends_workspace", ondelete="CASCADE"),
        sa.UniqueConstraint("request_id", "admission_token", "send_sequence",
                            name="uq_connector_provider_sends_sequence"),
        sa.CheckConstraint("attempt >= 0 AND send_sequence >= 0", name="ck_connector_provider_sends_counters"),
    )
    op.create_index("ix_connector_provider_sends_created", "connector_provider_sends", ["created_at"])

    op.create_table(
        "connector_quota_debits",
        sa.Column("send_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column("budget_kind", sa.String(16), nullable=False),
        sa.Column("subject_hash", sa.String(64), nullable=False),
        sa.Column("policy_key", sa.String(64), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("units", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("send_id", *_WINDOW_KEY),
        sa.ForeignKeyConstraint(["send_id"], ["connector_provider_sends.send_id"],
                                name="fk_connector_quota_debits_send", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            list(_WINDOW_KEY),
            [f"connector_quota_windows.{column}" for column in _WINDOW_KEY],
            name="fk_connector_quota_debits_window", ondelete="RESTRICT"),
        sa.CheckConstraint("units > 0", name="ck_connector_quota_debits_units"),
    )


def downgrade() -> None:
    """Drop the tables in dependency order."""
    op.drop_table("connector_quota_debits")
    op.drop_index("ix_connector_provider_sends_created", table_name="connector_provider_sends")
    op.drop_table("connector_provider_sends")
    op.drop_index("ix_connector_quota_windows_end", table_name="connector_quota_windows")
    op.drop_table("connector_quota_windows")
    op.drop_index("ix_connector_provider_terms_workspace", table_name="connector_provider_terms")
    op.drop_table("connector_provider_terms")
