"""Add owner "Not relevant" (hidden) state to document interactions."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "r15_document_dismissed"
down_revision: str | Sequence[str] | None = "r15_document_language_backfill"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CK = "ck_document_interactions_nonempty"
# Feed lookups hit the (owner_id, document_version_id) primary key, so no extra index is needed.


def upgrade() -> None:
    """Add nullable dismissed_at (metadata-only) and widen the non-empty check to include it."""
    op.add_column("document_interactions", sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_constraint(_CK, "document_interactions", type_="check")
    op.create_check_constraint(
        _CK, "document_interactions",
        "read_at IS NOT NULL OR bookmarked_at IS NOT NULL OR dismissed_at IS NOT NULL",
    )


def downgrade() -> None:
    """Drop dismissed-only rows (the old check forbids them), then restore the old shape."""
    op.execute("DELETE FROM document_interactions WHERE read_at IS NULL AND bookmarked_at IS NULL")
    op.drop_constraint(_CK, "document_interactions", type_="check")
    op.create_check_constraint(_CK, "document_interactions", "read_at IS NOT NULL OR bookmarked_at IS NOT NULL")
    op.drop_column("document_interactions", "dismissed_at")
