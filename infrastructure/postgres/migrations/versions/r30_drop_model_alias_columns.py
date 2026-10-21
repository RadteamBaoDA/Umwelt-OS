"""Drop the retired model-alias columns after r29 moved the data to ``ai_settings.defaults``.

``chat_messages.model_alias`` / ``daily_briefs.model_alias`` stay as historical provenance (they now store the
role name going forward).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "r30_drop_model_alias_columns"
down_revision: str | Sequence[str] | None = "r29_model_roles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop ``ai_settings.aliases/chat_alias/brief_alias`` and ``agent_profiles.model_alias`` (agents always use chat)."""
    op.drop_column("ai_settings", "aliases")
    op.drop_column("ai_settings", "chat_alias")
    op.drop_column("ai_settings", "brief_alias")
    op.drop_column("agent_profiles", "model_alias")


def downgrade() -> None:
    """Restore the columns with their original defaults; r29's downgrade then rebuilds alias data from ``defaults``."""
    op.add_column("ai_settings", sa.Column(
        "aliases", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False))
    op.add_column("ai_settings", sa.Column(
        "chat_alias", sa.String(length=32), server_default="reasoning-large", nullable=False))
    op.add_column("ai_settings", sa.Column(
        "brief_alias", sa.String(length=32), server_default="reasoning-small", nullable=False))
    op.add_column("agent_profiles", sa.Column(
        "model_alias", sa.String(length=64), server_default="reasoning-large", nullable=False))
