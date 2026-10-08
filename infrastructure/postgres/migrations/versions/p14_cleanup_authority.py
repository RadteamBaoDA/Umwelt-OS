"""Add original-authority epochs to document cleanup and source purge operations.

Historical rows stay NULL (quarantined legacy authority); no defaults, no backfill.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "p14_cleanup_authority"
down_revision: str | Sequence[str] | None = "p14_collection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DOC = "document_cleanup_operations"
_PURGE = "source_purge_operations"


def upgrade() -> None:
    """Add nullable epoch columns with all-or-none (cleanup) / null-or-positive (purge) checks."""
    op.add_column(_DOC, sa.Column("membership_revision", sa.BigInteger(), nullable=True))
    op.add_column(_DOC, sa.Column("configuration_revision", sa.BigInteger(), nullable=True))
    op.add_column(_DOC, sa.Column("source_generation", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_document_cleanup_original_epoch", _DOC,
        "(membership_revision IS NULL AND configuration_revision IS NULL AND source_generation IS NULL) OR "
        "(membership_revision IS NOT NULL AND configuration_revision IS NOT NULL AND source_generation IS NOT NULL "
        "AND membership_revision > 0 AND configuration_revision > 0 AND source_generation > 0)",
    )
    op.add_column(_PURGE, sa.Column("configuration_revision", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        "ck_source_purge_operations_configuration_revision", _PURGE,
        "configuration_revision IS NULL OR configuration_revision > 0",
    )


def downgrade() -> None:
    """Drop the checks and columns."""
    op.drop_constraint("ck_source_purge_operations_configuration_revision", _PURGE, type_="check")
    op.drop_column(_PURGE, "configuration_revision")
    op.drop_constraint("ck_document_cleanup_original_epoch", _DOC, type_="check")
    for column in ("source_generation", "configuration_revision", "membership_revision"):
        op.drop_column(_DOC, column)
