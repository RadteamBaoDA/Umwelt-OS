"""Allow dashboard.changed in the realtime replay log event-type check."""

from collections.abc import Sequence

from alembic import op

revision: str = "p14_realtime_dashboard_event"
down_revision: str | Sequence[str] | None = "p12_evidence_version_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "ck_realtime_replay_event_type"
_OLD = ("source.changed", "ingestion.changed", "knowledge.changed")
_NEW = (*_OLD, "dashboard.changed")


def _recreate(types: tuple[str, ...]) -> None:
    op.drop_constraint(_NAME, "realtime_replay_events", type_="check")
    quoted = ", ".join(f"'{t}'" for t in types)
    op.create_check_constraint(_NAME, "realtime_replay_events", f"event_type IN ({quoted})")


def upgrade() -> None:
    """Recreate the event-type check to include dashboard.changed."""
    _recreate(_NEW)


def downgrade() -> None:
    """Restore the pre-dashboard event-type check."""
    # Retained rows would violate the old check; clients resync through the replay_gap path.
    op.execute("DELETE FROM realtime_replay_events WHERE event_type = 'dashboard.changed'")
    _recreate(_OLD)
