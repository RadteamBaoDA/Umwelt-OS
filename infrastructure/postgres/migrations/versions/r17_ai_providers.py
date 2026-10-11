"""Add per-alias multi-provider storage to AI settings."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "r17_ai_providers"
down_revision: str | Sequence[str] | None = "r16_ai_system_prompts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``providers`` and move the single OmniRoute endpoint/key into provider ``default``.

    Legacy columns stay as a mirror of ``default`` so old readers and consent destination ids keep working.
    """
    op.add_column("ai_settings", sa.Column(
        "providers", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False))
    op.execute("""
        UPDATE ai_settings SET providers = jsonb_build_object('default', jsonb_build_object(
            'kind', 'omniroute', 'label', 'Default', 'base_url', omniroute_base_url,
            'key_ciphertext', omniroute_api_key_ciphertext, 'local', false))
        WHERE omniroute_base_url IS NOT NULL OR omniroute_api_key_ciphertext IS NOT NULL
    """)


def downgrade() -> None:
    """Drop provider storage; the legacy columns still hold the default provider."""
    op.drop_column("ai_settings", "providers")
