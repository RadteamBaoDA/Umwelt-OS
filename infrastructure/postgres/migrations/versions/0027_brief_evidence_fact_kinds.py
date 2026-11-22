"""Allow the observation-derived fact kinds on ``daily_brief_evidence``.

The scheduled Daily brief prompts ``markets``, ``weather``, ``hazards`` and ``source_health`` facts besides the
legacy ``tasks``, ``goals``, ``stories`` and ``events``. They are independent markers (no document support), so
the ``fact_kind`` CHECK is widened and the document-tuple CHECK also allows a marker row that carries only a
``source_id`` (so every prompted observation source is re-gated at read time, cited or not). Each new CHECK is
added ``NOT VALID`` and validated in a second statement; the table is small and the validation takes only a
``SHARE UPDATE EXCLUSIVE`` lock.

Downgrade deletes the evidence rows of the new kinds (the old CHECKs could not hold them) and marks the owning
briefs ``unavailable`` so they are withheld rather than read with a missing manifest.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0027_brief_evidence_kinds"
down_revision: str | Sequence[str] | None = "0026_chat_sql_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_daily_brief_evidence_fact_kind"
_OLD_KINDS = "'tasks','goals','stories','events'"
_NEW_KINDS = "'markets','weather','hazards','source_health'"
_TUPLE_CONSTRAINT = "ck_daily_brief_evidence_document_tuple"
_TUPLE_ALL_NULL = "document_id IS NULL AND document_version_id IS NULL AND chunk_id IS NULL"
_TUPLE_ALL_SET = (
    "document_id IS NOT NULL AND document_version_id IS NOT NULL AND chunk_id IS NOT NULL"
)
_TUPLE_OLD = (
    f"({_TUPLE_ALL_NULL} AND source_id IS NULL) OR ({_TUPLE_ALL_SET} AND source_id IS NOT NULL)"
)
# Marker rows: no document identity, but the single prompted source id is mandatory.
_TUPLE_NEW = (
    f"{_TUPLE_OLD} OR ({_TUPLE_ALL_NULL} AND source_id IS NOT NULL AND fact_kind IN ({_NEW_KINDS}))"
)


def _replace_check(name: str, expression: str) -> None:
    """Drop CHECK ``name`` and re-add it as ``expression`` (``NOT VALID`` first, then validated)."""
    op.execute(f"ALTER TABLE daily_brief_evidence DROP CONSTRAINT IF EXISTS {name}")
    op.execute(
        f"ALTER TABLE daily_brief_evidence ADD CONSTRAINT {name} CHECK ({expression}) NOT VALID"
    )
    op.execute(f"ALTER TABLE daily_brief_evidence VALIDATE CONSTRAINT {name}")


def upgrade() -> None:
    """Widen the fact-kind CHECK to eight kinds and let marker rows carry a source id."""
    _replace_check(_CONSTRAINT, f"fact_kind IN ({_OLD_KINDS},{_NEW_KINDS})")
    _replace_check(_TUPLE_CONSTRAINT, _TUPLE_NEW)


def downgrade() -> None:
    """Withhold briefs that used the new kinds, delete their evidence rows, restore the old CHECKs."""
    # S608: the interpolated value is a module constant, never user input.
    op.execute(
        "UPDATE daily_briefs SET evidence_capture_status = 'unavailable' WHERE id IN "  # noqa: S608
        f"(SELECT brief_id FROM daily_brief_evidence WHERE fact_kind IN ({_NEW_KINDS}))"
    )
    op.execute(f"DELETE FROM daily_brief_evidence WHERE fact_kind IN ({_NEW_KINDS})")  # noqa: S608
    _replace_check(_CONSTRAINT, f"fact_kind IN ({_OLD_KINDS})")
    _replace_check(_TUPLE_CONSTRAINT, _TUPLE_OLD)
