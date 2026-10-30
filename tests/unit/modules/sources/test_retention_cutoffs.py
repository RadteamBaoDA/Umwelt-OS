"""Unit tests for the Sources retention-cutoff contract."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from modules.sources import public as sources

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _session(rows: list[SimpleNamespace]) -> AsyncMock:
    """Return a fake session whose execute() yields ``rows`` from ``.all()``."""
    result = MagicMock()
    result.all.return_value = rows
    session = AsyncMock()
    session.execute.return_value = result
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cfg", "expected_days"),
    [
        ({"retention_days": 30}, 30),
        ({"retention_days": 1}, 1),
        ({"retention_days": 3650}, 3650),
        ({"retention_days": 0}, 7),
        ({"retention_days": 3651}, 7),
        ({"retention_days": True}, 7),
        ({"retention_days": "30"}, 7),
        ({}, 7),
    ],
)
async def test_retention_days_bounds(monkeypatch, cfg, expected_days):
    """Only real ints in 1..3650 override the provider default."""
    monkeypatch.setattr(
        "modules.connectors.public.default_retention_days", lambda provider: 7
    )
    sid = uuid4()
    session = _session([SimpleNamespace(id=sid, provider="x", configuration=cfg)])
    assert await sources.list_retention_cutoffs(session, now=NOW) == [
        (sid, NOW - timedelta(days=expected_days))
    ]


@pytest.mark.asyncio
async def test_unbounded_default_is_omitted_and_non_dict_config_tolerated(monkeypatch):
    """No window (default None) yields no cutoff; non-dict configuration is ignored."""
    monkeypatch.setattr("modules.connectors.public.default_retention_days", lambda provider: None)
    session = _session([SimpleNamespace(id=uuid4(), provider="x", configuration=None)])
    assert await sources.list_retention_cutoffs(session, now=NOW) == []


@pytest.mark.asyncio
async def test_query_excludes_file_and_manual_sources():
    """The SQL excludes real user-authored SourceTypes (file, manual) via NOT IN."""
    from sqlalchemy.dialects import postgresql

    session = _session([])
    await sources.list_retention_cutoffs(session, now=NOW)
    stmt = session.execute.call_args.args[0]
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "NOT IN ('file', 'manual')" in sql
    assert "'upload'" not in sql
