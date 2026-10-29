"""Add ordered encrypted fallback web-search destinations."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0006_web_search_fallbacks"
down_revision: str | Sequence[str] | None = "0005_reddit_oauth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Keep existing primary configuration and initialize fallback lists empty."""
    op.add_column("ai_settings", sa.Column("web_search_fallbacks", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")))


def downgrade() -> None:
    """Remove fallback configuration; existing primary settings are retained."""
    op.drop_column("ai_settings", "web_search_fallbacks")
