"""Persist Documents-owned cleanup receipts for individual document deletion."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "p12_document_cleanup"
down_revision: str | Sequence[str] | None = "p12_onboarding_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create durable document-cleanup stage receipts after the current migration head."""
    op.create_table(
        "document_cleanup_operations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("raw_uri", sa.Text(), nullable=True),
        sa.Column("record_status", sa.String(length=16), server_default="deleted", nullable=False),
        sa.Column("graph_status", sa.String(length=16), server_default="tombstoned", nullable=False),
        sa.Column("raw_status", sa.String(length=24), server_default="queued", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed')", name="ck_document_cleanup_status"),
        sa.CheckConstraint("record_status = 'deleted'", name="ck_document_cleanup_record_status"),
        sa.CheckConstraint("graph_status = 'tombstoned'", name="ck_document_cleanup_graph_status"),
        sa.CheckConstraint(
            "raw_status IN ('queued', 'not_present', 'retained_shared', 'succeeded', 'failed')",
            name="ck_document_cleanup_raw_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_document_cleanup_status_created",
        "document_cleanup_operations",
        ["status", "created_at"],
    )


def downgrade() -> None:
    """Remove cleanup receipts without changing retained Documents or raw files."""
    op.drop_index("ix_document_cleanup_status_created", table_name="document_cleanup_operations")
    op.drop_table("document_cleanup_operations")
