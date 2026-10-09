"""Add the single global translation admission slot and the purge index."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "p14_translation_runtime"
down_revision: str | Sequence[str] | None = "p14_workspace_shares"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create translation_admission_slots (one seeded row, no FK) and ix_content_translations_resource."""
    op.create_table(
        "translation_admission_slots",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("translation_id", sa.Uuid(), nullable=True),
        sa.Column("fencing_token", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("id = 1", name="ck_translation_admission_slots_singleton"),
        sa.CheckConstraint("fencing_token >= 0", name="ck_translation_admission_slots_token"),
        sa.CheckConstraint(
            "(translation_id IS NULL) = (expires_at IS NULL)", name="ck_translation_admission_slots_holder",
        ),
    )
    op.execute("INSERT INTO translation_admission_slots (id, translation_id, fencing_token, expires_at) VALUES (1, NULL, 0, NULL)")
    op.create_index(
        "ix_content_translations_resource", "content_translations",
        ["workspace_id", "resource_type", "resource_id"],
    )


def downgrade() -> None:
    """Drop the slot table and the purge index."""
    op.drop_index("ix_content_translations_resource", table_name="content_translations")
    op.drop_table("translation_admission_slots")
