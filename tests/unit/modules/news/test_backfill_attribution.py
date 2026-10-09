"""Attribution backfill: dry-run default, workspace predicate on every page, idempotent writes."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from modules.news import backfill_attribution as bf

WS = uuid4()


def _record(**fields):
    return {"provider_record": {
        "provider": "bbc_world", "identity": "x", "timestamp_basis": "collection", "coverage": "returned_snapshot",
        "content_truncated": False, "license_label": fields.pop("license_label", None), "source_fields": fields,
    }}


def _session(pages):
    """Session whose successive pending queries return `pages`, then nothing."""
    queue = list(pages)
    session = MagicMock(commit=AsyncMock())
    seen = []

    async def execute(statement):
        seen.append(statement)
        if statement.is_select:
            return MagicMock(all=lambda: queue.pop(0) if queue else [])
        return MagicMock()

    session.execute = execute
    session.seen = seen
    return session


def _sql(statement):
    return str(statement.compile(dialect=postgresql.dialect()))


@pytest.mark.asyncio
async def test_dry_run_is_default_and_writes_nothing():
    a = uuid4()
    session = _session([[(a, _record(publisher="BBC"))]])
    result = await bf.backfill_workspace(session, WS)
    assert result == {"examined": 1, "filled": 1}
    assert all(s.is_select for s in session.seen)
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_writes_guarded_update_with_workspace_and_never_invents():
    a, b = sorted([uuid4(), uuid4()])
    session = _session([[(a, _record(publisher="  BBC  ", license_label="CC")), (b, _record(publisher="   "))]])
    result = await bf.backfill_workspace(session, WS, apply=True)
    assert result == {"examined": 2, "filled": 1}
    updates = [s for s in session.seen if not s.is_select]
    assert len(updates) == 1
    sql = _sql(updates[0])
    assert "workspace_id" in sql and "jsonb_set" in sql and "NOT (news_observations.match_evidence ?" in sql
    assert updates[0].compile(dialect=postgresql.dialect()).params["id_1"] == a
    session.commit.assert_awaited()


@pytest.mark.asyncio
async def test_every_page_keeps_workspace_predicate_and_keyset():
    first = [(uuid4(), _record(publisher="P")) for _ in range(bf.PAGE_SIZE)]
    session = _session([first, []])
    await bf.backfill_workspace(session, WS)
    selects = [_sql(s) for s in session.seen if s.is_select]
    assert len(selects) == 2
    assert all("news_observations.workspace_id" in sql for sql in selects)
    assert "news_observations.id >" not in selects[0] and "news_observations.id >" in selects[1]


@pytest.mark.asyncio
async def test_second_run_is_noop_when_nothing_pending():
    session = _session([])
    assert await bf.backfill_workspace(session, WS, apply=True) == {"examined": 0, "filled": 0}
    assert not [s for s in session.seen if not s.is_select]
    session.commit.assert_not_awaited()
