"""Add per-rule last-notified state for highlight delivery (cooldown)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "r15_highlight_rule_delivery"
down_revision: str | Sequence[str] | None = "r15_document_dismissed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add a NOT NULL JSONB column defaulting to {} (metadata-only on PostgreSQL 11+)."""
    op.add_column(
        "gadget_highlight_progress",
        sa.Column("rule_last_notified", postgresql.JSONB(), nullable=False, server_default="{}"),
    )
    op.create_table(
        "gadget_highlight_suppression",
        sa.Column("definition_id", sa.Uuid(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["definition_id"], ["gadget_definitions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("definition_id", "dedupe_key"),
    )


def downgrade() -> None:
    """Drop the column; cooldown state is transient and rebuilds on the next notification."""
    op.drop_table("gadget_highlight_suppression")
    op.drop_column("gadget_highlight_progress", "rule_last_notified")
