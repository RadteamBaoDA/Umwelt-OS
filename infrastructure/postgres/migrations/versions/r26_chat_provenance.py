"""Add nullable model-run provenance to chat todos and chat templates.

Chained after r25_chat_templates. ``source_response_id`` records the run whose model tool call wrote the row so
document-evidence cleanup can delete model-derived copies. It has no foreign key on purpose: run retention must not
silently null the provenance, and cleanup deletes by the id directly.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "r26_chat_provenance"
down_revision: str | Sequence[str] | None = "r25_chat_templates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable provenance column to both tables (existing rows stay NULL = owner-authored)."""
    for table in ("chat_todos", "chat_templates"):
        op.add_column(table, sa.Column("source_response_id", postgresql.UUID(as_uuid=True), nullable=True))


def downgrade() -> None:
    """Drop the provenance columns."""
    for table in ("chat_templates", "chat_todos"):
        op.drop_column(table, "source_response_id")
