"""Persist per-owner completion receipts for the explicit P08 demo seed.

Revision ID: p08_demo_seed_receipts
Revises: p08_topic_interest_profiles
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "p08_demo_seed_receipts"
down_revision: str | Sequence[str] | None = "p08_topic_interest_profiles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the owner/namespace uniqueness fence and completion timestamp table."""
    op.create_table(
        "demo_seed_receipts",
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("namespace", sa.String(length=128), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("length(namespace) BETWEEN 1 AND 128", name="ck_demo_seed_receipts_namespace_length"),
        sa.ForeignKeyConstraint(["owner_id"], ["owner.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("owner_id", "namespace"),
    )


def downgrade() -> None:
    """Drop the P08 seed receipt table and its owner-scoped completion records."""
    op.drop_table("demo_seed_receipts")
