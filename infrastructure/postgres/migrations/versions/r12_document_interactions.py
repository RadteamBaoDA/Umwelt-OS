"""Persist owner read and bookmark state for exact document versions."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "r12_document_interactions"
down_revision: str | Sequence[str] | None = "p11_retention_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create owner/version interaction state with deletion cascades."""
    op.create_table(
        "document_interactions",
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bookmarked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "read_at IS NOT NULL OR bookmarked_at IS NOT NULL",
            name="ck_document_interactions_nonempty",
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["owner.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("owner_id", "document_version_id"),
    )
    op.create_index(
        "ix_document_interactions_owner_read", "document_interactions", ["owner_id", "read_at"],
    )


def downgrade() -> None:
    """Drop the owner/version interaction projection."""
    op.drop_index("ix_document_interactions_owner_read", table_name="document_interactions")
    op.drop_table("document_interactions")
