"""Unit tests for legacy brief coverage paging and its Document time bound."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from modules.dashboard import briefs


class _Session:
    def __init__(self, ids) -> None:
        self.ids, self.statement = ids, None

    async def scalars(self, statement):
        self.statement = statement
        return SimpleNamespace(all=lambda: list(self.ids))


def _sql(session) -> str:
    return str(session.statement.compile(dialect=postgresql.dialect()))


async def test_not_before_excludes_briefs_generated_before_the_document() -> None:
    bound = datetime(2026, 1, 1, tzinfo=UTC)
    session = _Session([])
    await briefs.legacy_brief_coverage(session, not_before=bound)
    assert "generated_at >=" in _sql(session)
    assert bound in session.statement.compile().params.values()


async def test_without_not_before_every_legacy_brief_is_a_candidate() -> None:
    session = _Session([])
    await briefs.legacy_brief_coverage(session)
    sql = _sql(session)
    assert "generated_at >=" not in sql
    assert "evidence_capture_version IS NULL" in sql


async def test_pages_with_keyset_cursor_and_rejects_bad_limits() -> None:
    ids = sorted(uuid4() for _ in range(3))
    page = await briefs.legacy_brief_coverage(_Session(ids), limit=2)
    assert page.candidate_ids == ids[:2] and page.next_cursor == ids[1]
    last = await briefs.legacy_brief_coverage(_Session(ids[:2]), limit=2)
    assert last.next_cursor is None
    for limit in (0, 101):
        with pytest.raises(ValueError):
            await briefs.legacy_brief_coverage(_Session([]), limit=limit)
