"""Add multi-provider and n8n storage to AI settings."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "r17_ai_providers"
down_revision: str | Sequence[str] | None = "r16_ai_system_prompts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add per-provider storage (with typed models) and the n8n connection settings to AI settings."""
    op.add_column("ai_settings", sa.Column(
        "providers", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False))
    op.add_column("ai_settings", sa.Column(
        "n8n", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False))


def downgrade() -> None:
    """Drop provider and n8n storage."""
    op.drop_column("ai_settings", "n8n")
    op.drop_column("ai_settings", "providers")
