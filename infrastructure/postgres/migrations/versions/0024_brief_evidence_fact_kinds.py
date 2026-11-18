"""Allow the observation-derived fact kinds on ``daily_brief_evidence``.

The scheduled Daily brief prompts ``markets``, ``weather``, ``hazards`` and ``source_health`` facts besides the
legacy ``tasks``, ``goals``, ``stories`` and ``events``. They are independent markers (no document support), so
only the ``fact_kind`` CHECK needs widening. The new CHECK is added ``NOT VALID`` and validated in a second
statement; the table is small and the validation takes only a ``SHARE UPDATE EXCLUSIVE`` lock.

Downgrade deletes the evidence rows of the new kinds (the old CHECK could not hold them) and marks the owning
briefs ``unavailable`` so they are withheld rather than read with a missing manifest.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0024_brief_evidence_kinds"
down_revision: str | Sequence[str] | None = "0023_obs_source_observed_idx"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_daily_brief_evidence_fact_kind"
_OLD_KINDS = "'tasks','goals','stories','events'"
_NEW_KINDS = "'markets','weather','hazards','source_health'"


def _replace_check(kinds: str) -> None:
    """Drop the fact-kind CHECK and re-add it for ``kinds`` (``NOT VALID`` first, then validated)."""
    op.execute(f"ALTER TABLE daily_brief_evidence DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE daily_brief_evidence ADD CONSTRAINT {_CONSTRAINT} "
        f"CHECK (fact_kind IN ({kinds})) NOT VALID"
    )
    op.execute(f"ALTER TABLE daily_brief_evidence VALIDATE CONSTRAINT {_CONSTRAINT}")


def upgrade() -> None:
    """Widen the fact-kind CHECK to the eight kinds the brief can prompt."""
    _replace_check(f"{_OLD_KINDS},{_NEW_KINDS}")


def downgrade() -> None:
    """Withhold briefs that used the new kinds, delete their evidence rows, restore the old CHECK."""
    # S608: the interpolated value is a module constant, never user input.
    op.execute(
        "UPDATE daily_briefs SET evidence_capture_status = 'unavailable' WHERE id IN "  # noqa: S608
        f"(SELECT brief_id FROM daily_brief_evidence WHERE fact_kind IN ({_NEW_KINDS}))"
    )
    op.execute(f"DELETE FROM daily_brief_evidence WHERE fact_kind IN ({_NEW_KINDS})")  # noqa: S608
    _replace_check(_OLD_KINDS)
