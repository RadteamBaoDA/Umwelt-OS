"""Persist individual document evidence identities and Chat cleanup progress."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "p12_chat_evidence_cleanup"
down_revision: str | Sequence[str] | None = "p12_backup_control"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add truthful copied-evidence stages and detached version/chunk identities."""
    op.add_column(
        "document_cleanup_operations",
        sa.Column("copied_status", sa.String(length=16), server_default="queued", nullable=False),
    )
    op.add_column(
        "document_cleanup_operations",
        sa.Column("copied_cursor", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "document_cleanup_operations",
        sa.Column("copied_error_code", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "document_cleanup_operations",
        sa.Column("chat_status", sa.String(length=16), server_default="queued", nullable=False),
    )
    op.add_column(
        "document_cleanup_operations",
        sa.Column("chat_error_code", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "document_cleanup_operations",
        sa.Column("evidence_scope_status", sa.String(length=16), server_default="unavailable", nullable=False),
    )
    op.create_check_constraint(
        "ck_document_cleanup_copied_status",
        "document_cleanup_operations",
        "copied_status IN ('queued', 'running', 'failed')",
    )
    op.create_check_constraint(
        "ck_document_cleanup_chat_status",
        "document_cleanup_operations",
        "chat_status IN ('queued', 'running', 'succeeded', 'failed')",
    )
    op.create_check_constraint(
        "ck_document_cleanup_copied_cursor_bound",
        "document_cleanup_operations",
        "copied_cursor IS NULL OR octet_length(copied_cursor::text) <= 4096",
    )
    op.create_check_constraint(
        "ck_document_cleanup_evidence_scope_status",
        "document_cleanup_operations",
        "evidence_scope_status IN ('capturing', 'captured', 'unavailable')",
    )
    # Old receipts predate immutable-ID capture and cannot prove version-only copies were found.
    op.execute(sa.text(
        "UPDATE document_cleanup_operations "
        "SET chat_status = 'failed', copied_status = 'failed', "
        "copied_error_code = 'evidence_identity_unavailable', status = 'failed', "
        "error_code = 'evidence_identity_unavailable' "
        "WHERE evidence_scope_status = 'unavailable'"
    ))
    op.create_table(
        "document_cleanup_evidence_references",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False,
        ),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reference_kind", sa.String(length=16), nullable=False),
        sa.CheckConstraint(
            "(reference_kind = 'version' AND chunk_id IS NULL) OR "
            "(reference_kind = 'chunk' AND chunk_id IS NOT NULL)",
            name="ck_document_cleanup_evidence_reference_shape",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"], ["document_cleanup_operations.id"],
            name="fk_document_cleanup_evidence_operation", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_document_cleanup_evidence_operation_id",
        "document_cleanup_evidence_references",
        ["operation_id", "id"],
    )


def downgrade() -> None:
    """Remove only cleanup tracking metadata; canonical data is already deleted by the user action."""
    op.drop_index(
        "ix_document_cleanup_evidence_operation_id",
        table_name="document_cleanup_evidence_references",
    )
    op.drop_table("document_cleanup_evidence_references")
    op.drop_constraint(
        "ck_document_cleanup_copied_cursor_bound", "document_cleanup_operations", type_="check",
    )
    op.drop_constraint(
        "ck_document_cleanup_evidence_scope_status", "document_cleanup_operations", type_="check",
    )
    op.drop_constraint("ck_document_cleanup_chat_status", "document_cleanup_operations", type_="check")
    op.drop_constraint("ck_document_cleanup_copied_status", "document_cleanup_operations", type_="check")
    op.drop_column("document_cleanup_operations", "chat_status")
    op.drop_column("document_cleanup_operations", "chat_error_code")
    op.drop_column("document_cleanup_operations", "evidence_scope_status")
    op.drop_column("document_cleanup_operations", "copied_error_code")
    op.drop_column("document_cleanup_operations", "copied_cursor")
    op.drop_column("document_cleanup_operations", "copied_status")
