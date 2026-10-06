"""Persist only the owner's onboarding step and explicit completion timestamp."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "p12_onboarding_state"
down_revision: str | Sequence[str] | None = "r07_chat_mutation_receipts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create a singleton table for non-secret resumable onboarding progress."""
    op.create_table(
        "onboarding_state",
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("configuration_revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("current_step", sa.String(length=32), server_default="ai_privacy", nullable=False),
        sa.Column("data_choice", sa.String(length=24), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("owner_id = 1", name="ck_onboarding_state_single_owner"),
        sa.CheckConstraint("configuration_revision > 0", name="ck_onboarding_state_revision_positive"),
        sa.CheckConstraint(
            "current_step IN ('ai_privacy', 'capability', 'sources', 'sample_or_import', 'indexing', 'complete')",
            name="ck_onboarding_state_step",
        ),
        sa.CheckConstraint(
            "data_choice IS NULL OR data_choice IN ('sample', 'personal_import')",
            name="ck_onboarding_state_data_choice",
        ),
        sa.CheckConstraint(
            "(current_step = 'complete') = (completed_at IS NOT NULL)",
            name="ck_onboarding_state_completion",
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["owner.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("owner_id"),
    )


def downgrade() -> None:
    """Drop the onboarding progress table."""
    op.drop_table("onboarding_state")
