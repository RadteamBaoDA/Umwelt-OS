"""Add a bounded Memory copy-cleanup stage to Documents receipts."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "p12_memory_document_cleanup"
down_revision: str | Sequence[str] | None = "p12_source_cleanup_children"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add stage state; captured historical receipts are queued by a bounded worker reconciler."""
    op.add_column(
        "document_cleanup_operations",
        sa.Column("memory_status", sa.String(length=16), server_default="queued", nullable=False),
    )
    op.add_column("document_cleanup_operations", sa.Column("memory_error_code", sa.String(length=64)))
    op.add_column(
        "document_cleanup_operations",
        sa.Column("memory_cursor", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "document_cleanup_operations",
        sa.Column("memory_unresolved_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "document_cleanup_operations",
        sa.Column("memory_cache_pending", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.create_check_constraint(
        "ck_document_cleanup_memory_status", "document_cleanup_operations",
        "memory_status IN ('queued', 'running', 'succeeded', 'failed')",
    )
    op.create_check_constraint(
        "ck_document_cleanup_memory_cursor_bound", "document_cleanup_operations",
        "memory_cursor IS NULL OR octet_length(memory_cursor::text) <= 4096",
    )
    op.create_check_constraint(
        "ck_document_cleanup_memory_unresolved_nonnegative", "document_cleanup_operations",
        "memory_unresolved_count >= 0",
    )
    # Old cascades without immutable evidence identities remain explicitly unresolved.
    op.execute(sa.text(
        "UPDATE document_cleanup_operations SET memory_status = 'failed', "
        "memory_error_code = 'evidence_identity_unavailable' "
        "WHERE evidence_scope_status <> 'captured'"
    ))


def downgrade() -> None:
    """Remove Memory stage metadata without restoring any erased payload."""
    op.drop_constraint(
        "ck_document_cleanup_memory_cursor_bound", "document_cleanup_operations", type_="check",
    )
    op.drop_constraint(
        "ck_document_cleanup_memory_unresolved_nonnegative", "document_cleanup_operations", type_="check",
    )
    op.drop_constraint(
        "ck_document_cleanup_memory_status", "document_cleanup_operations", type_="check",
    )
    op.drop_column("document_cleanup_operations", "memory_cache_pending")
    op.drop_column("document_cleanup_operations", "memory_unresolved_count")
    op.drop_column("document_cleanup_operations", "memory_cursor")
    op.drop_column("document_cleanup_operations", "memory_error_code")
    op.drop_column("document_cleanup_operations", "memory_status")
