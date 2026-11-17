"""Add nullable owner display name and accent to owner preferences."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_owner_display_name_accent"
down_revision: str | Sequence[str] | None = "0007_collection_receipt_stats"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``display_name`` and ``accent`` (NULL = unset/ink); existing rows stay valid."""
    op.add_column("owner_preferences", sa.Column("display_name", sa.String(80), nullable=True))
    op.add_column("owner_preferences", sa.Column("accent", sa.String(8), nullable=True))
    op.create_check_constraint(
        "ck_owner_preferences_accent", "owner_preferences",
        "accent IS NULL OR accent IN ('ink', 'moss', 'sea', 'earth', 'plum')",
    )
    # Backfill a name for existing preference rows. Mirrors modules.settings.display_name.derive_display_name:
    # email local part split on . _ - +, each part capitalised, space-joined, cut to 80, else 'Owner'.
    # Owners without a preferences row get no row here (that would flip `persisted`); reads resolve their name.
    op.execute(
        """
        UPDATE owner_preferences p
        SET display_name = COALESCE(NULLIF(left(btrim((
            SELECT string_agg(initcap(part), ' ' ORDER BY ord)
            FROM regexp_split_to_table(split_part(COALESCE(o.email, ''), '@', 1), '[._+-]+')
                 WITH ORDINALITY AS t(part, ord)
            WHERE part <> ''
        )), 80), ''), 'Owner')
        FROM owner o
        WHERE o.id = p.owner_id AND p.display_name IS NULL
        """
    )


def downgrade() -> None:
    """Drop the accent check and both columns."""
    op.drop_constraint("ck_owner_preferences_accent", "owner_preferences", type_="check")
    op.drop_column("owner_preferences", "accent")
    op.drop_column("owner_preferences", "display_name")
