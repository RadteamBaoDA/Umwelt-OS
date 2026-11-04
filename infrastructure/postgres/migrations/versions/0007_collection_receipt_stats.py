"""Add nullable bounded run telemetry to ingestion collection receipts."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0007_collection_receipt_stats"
down_revision: str | Sequence[str] | None = "0006_web_search_fallbacks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``collection_stats`` (new/backlog/gap/duplicates/failed sections); existing receipts stay NULL."""
    op.add_column("ingestion_collection_receipts", sa.Column("collection_stats", JSONB, nullable=True))


def downgrade() -> None:
    """Drop the telemetry column; nothing else reads it, so older code is unaffected."""
    op.drop_column("ingestion_collection_receipts", "collection_stats")
