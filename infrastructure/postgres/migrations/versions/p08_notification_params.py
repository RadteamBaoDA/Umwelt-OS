"""Store notification message keys with params instead of display-ready English text."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "p08_notification_params"
down_revision: str | Sequence[str] | None = "p08_daily_brief_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add params JSON and let title become an optional legacy fallback."""
    op.add_column("notifications", sa.Column("params", postgresql.JSONB(), server_default="{}", nullable=False))
    op.alter_column("notifications", "title", existing_type=sa.String(length=300), nullable=True)


def downgrade() -> None:
    """Restore the required title (blank for key-only rows) and drop params."""
    op.execute("UPDATE notifications SET title = kind WHERE title IS NULL")
    op.alter_column("notifications", "title", existing_type=sa.String(length=300), nullable=False)
    op.drop_column("notifications", "params")
