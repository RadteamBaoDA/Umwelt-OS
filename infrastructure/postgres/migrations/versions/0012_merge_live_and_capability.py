"""Join the live-map and pending model-capability migration histories."""

from collections.abc import Sequence

revision: str = "0012_merge_live_and_capability"
down_revision: str | Sequence[str] | None = ("0011_merge_live_map", "0011_model_capability_evidence")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the parent branches without applying schema operations."""


def downgrade() -> None:
    """Leave parent schemas intact when reversing the merge revision."""
