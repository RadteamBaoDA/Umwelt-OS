"""Add auth_session.last_seen_at for the idle session timeout."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "r18_auth_session_last_seen"
down_revision: str | Sequence[str] | None = "r17_ai_providers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add nullable last_seen_at; NULL rows fall back to created_at when the idle window is checked."""
    op.add_column("auth_session", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Drop last_seen_at."""
    op.drop_column("auth_session", "last_seen_at")
