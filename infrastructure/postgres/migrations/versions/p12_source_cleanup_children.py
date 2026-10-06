"""Capture bounded Documents-owned cleanup children for whole-Source deletion."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "p12_source_cleanup_children"
down_revision: str | Sequence[str] | None = "p12_chat_evidence_cleanup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add owner-local child linkage and classify unverifiable legacy Source receipts."""
    op.add_column(
        "document_cleanup_operations",
        sa.Column("source_purge_operation_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_document_cleanup_source_purge_id",
        "document_cleanup_operations",
        ["source_purge_operation_id", "id"],
    )
    op.create_index(
        "uq_document_cleanup_source_purge_document",
        "document_cleanup_operations",
        ["source_purge_operation_id", "document_id"],
        unique=True,
        postgresql_where=sa.text("source_purge_operation_id IS NOT NULL"),
    )
    op.add_column(
        "source_purge_operations",
        sa.Column("documents_status", sa.String(length=16), server_default="queued", nullable=False),
    )
    op.add_column("source_purge_operations", sa.Column("pending_child_count", sa.Integer(), nullable=True))
    op.add_column("source_purge_operations", sa.Column("failed_child_count", sa.Integer(), nullable=True))
    op.add_column(
        "source_purge_operations",
        sa.Column(
            "pending_owner_codes", postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"), nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_source_purge_operations_documents_status",
        "source_purge_operations",
        "documents_status IN ('queued', 'deleted', 'failed', 'unavailable')",
    )
    # The original worker committed status=running in the same transaction as the cascade.
    # Queued receipts have not entered that transaction and can still be captured safely.
    op.execute(sa.text(
        "UPDATE source_purge_operations SET documents_status = 'unavailable', "
        "status = 'failed', error_code = 'evidence_identity_unavailable', "
        "pending_child_count = NULL, failed_child_count = NULL, "
        "pending_owner_codes = '[\"documents\", \"legacy_raw\", \"chat\", \"memory\", "
        "\"agents\", \"dashboard\", \"notifications\", \"automations\"]'::jsonb "
        "WHERE status IN ('running', 'succeeded') "
        "OR (status = 'failed' AND error_code IS DISTINCT FROM 'source_generation_changed')"
    ))
    op.execute(sa.text(
        "UPDATE source_purge_operations SET documents_status = 'failed', "
        "pending_owner_codes = '[\"documents\"]'::jsonb "
        "WHERE status = 'failed' AND error_code = 'source_generation_changed'"
    ))
    op.execute(sa.text(
        "UPDATE source_purge_operations SET pending_owner_codes = '[\"documents\"]'::jsonb, "
        "pending_child_count = NULL, failed_child_count = NULL WHERE status = 'queued'"
    ))


def downgrade() -> None:
    """Remove the new receipt projection without restoring unverifiable legacy claims."""
    op.drop_constraint(
        "ck_source_purge_operations_documents_status", "source_purge_operations", type_="check",
    )
    op.drop_column("source_purge_operations", "pending_owner_codes")
    op.drop_column("source_purge_operations", "failed_child_count")
    op.drop_column("source_purge_operations", "pending_child_count")
    op.drop_column("source_purge_operations", "documents_status")
    op.drop_index(
        "uq_document_cleanup_source_purge_document", table_name="document_cleanup_operations",
    )
    op.drop_index("ix_document_cleanup_source_purge_id", table_name="document_cleanup_operations")
    op.drop_column("document_cleanup_operations", "source_purge_operation_id")
