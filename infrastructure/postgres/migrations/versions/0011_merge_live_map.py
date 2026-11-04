"""Join the live map and existing application migration branches."""

from collections.abc import Sequence

revision: str = "0011_merge_live_map"
down_revision: str | Sequence[str] | None = ("0008_live_map_config", "0010_observations_vn_stock")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join both histories; each parent already applies its own schema changes."""


def downgrade() -> None:
    """Leave both parent schemas intact when returning to the branch point."""
