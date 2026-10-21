"""Retain exact n8n service origins before dispatch; legacy origins remain unknown."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "r28_n8n_endpoint_lineage"
down_revision: str | Sequence[str] | None = "r27_account_deletion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add nullable origins without claiming mutable current settings describe historical effects."""
    op.add_column("connector_provisioning", sa.Column("n8n_service_url", sa.String(2048)))
    op.add_column("connector_managed_credentials", sa.Column("n8n_service_url", sa.String(2048)))


def downgrade() -> None:
    """Refuse loss of endpoint identity needed for safe remote cleanup and dispatch."""
    raise RuntimeError("n8n endpoint lineage is forward-only; retain remote cleanup authority")
