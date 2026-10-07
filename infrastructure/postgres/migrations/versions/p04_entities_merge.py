"""Join the preserved entity revision with the integrated replay head."""

from collections.abc import Sequence

revision: str = "p04_entities_merge"
down_revision: str | Sequence[str] | None = ("0007_receipt_normalization", "0007_entities")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the entity branch with receipt normalization and realtime replay history; this merge revision has no schema operations."""


def downgrade() -> None:
    """Reverse the merge marker; this revision has no schema operations."""
