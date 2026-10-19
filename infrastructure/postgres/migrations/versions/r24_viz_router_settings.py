"""Add the opt-in visualization router settings JSON to AI settings.

Empty means the internal router. Stored shape: {kind, provider_id, model, mode, allow_remote_visualization_routing,
consent_destination, typesafe_key_ciphertext, revision}. Chained after r23_chat_run_awaiting_input.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "r24_viz_router_settings"
down_revision: str | Sequence[str] | None = "r23_chat_run_awaiting_input"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the non-null ``viz_router`` JSONB column defaulting to the internal router (empty object)."""
    op.add_column("ai_settings", sa.Column(
        "viz_router", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False))


def downgrade() -> None:
    """Drop the visualization router settings column."""
    op.drop_column("ai_settings", "viz_router")
