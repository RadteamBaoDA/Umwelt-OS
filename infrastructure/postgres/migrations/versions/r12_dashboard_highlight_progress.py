"""Persist bounded per-definition Dashboard highlight scan progress."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "r12_dashboard_highlight_progress"
down_revision: str | Sequence[str] | None = "r12_document_interactions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create one revision-bound immutable-version cursor per highlight definition."""
    op.create_table(
        "gadget_highlight_progress",
        sa.Column("definition_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("definition_revision", sa.BigInteger(), nullable=False),
        sa.Column("rules_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("cursor_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cursor_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("definition_revision >= 1", name="ck_gadget_highlight_progress_revision"),
        sa.CheckConstraint(
            "(cursor_created_at IS NULL) = (cursor_version_id IS NULL)",
            name="ck_gadget_highlight_progress_cursor_pair",
        ),
        sa.ForeignKeyConstraint(["definition_id"], ["gadget_definitions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("definition_id"),
    )


def downgrade() -> None:
    """Drop durable highlight scan cursors."""
    op.drop_table("gadget_highlight_progress")
