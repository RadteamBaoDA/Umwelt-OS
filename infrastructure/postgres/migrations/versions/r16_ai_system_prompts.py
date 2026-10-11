"""Add owner-editable system prompt overrides to AI settings."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "r16_ai_system_prompts"
down_revision: str | Sequence[str] | None = "p14_translation_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Store only customized prompts; missing keys fall back to code defaults."""
    op.add_column("ai_settings", sa.Column(
        "system_prompts", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False))


def downgrade() -> None:
    """Drop prompt overrides."""
    op.drop_column("ai_settings", "system_prompts")
