"""Add a (source_id, observed_at DESC) index serving chat's latest-observations and bounded-window reads.

``ix_observations_owner_series_current`` leads with ``(source_id, provider, metric, ...)``, so a read that
filters by source and orders by ``observed_at`` without naming a series cannot use it for the sort. The new
index is built ``CONCURRENTLY`` (no write-blocking lock) through ``core.migration_support``.
"""

from collections.abc import Sequence

from core.migration_support import create_index_concurrently, drop_index_concurrently

revision: str = "0023_obs_source_observed_idx"
down_revision: str | Sequence[str] | None = "0022_dashboard_panels"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_observations_source_observed_desc"


def upgrade() -> None:
    """Create the source/time index concurrently (idempotent; an invalid leftover is rebuilt)."""
    create_index_concurrently(_INDEX, "observations", "(source_id, observed_at DESC)")


def downgrade() -> None:
    """Drop the source/time index concurrently."""
    drop_index_concurrently(_INDEX)
