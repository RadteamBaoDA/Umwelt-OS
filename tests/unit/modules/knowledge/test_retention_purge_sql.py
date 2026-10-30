"""SQL-shape tests for the Observations/Documents retention purge contracts."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import column
from sqlalchemy.dialects import postgresql

from modules.knowledge.documents import public as documents
from modules.knowledge.observations import public as observations

CUTOFFS = [(uuid4(), datetime(2026, 1, 1, tzinfo=UTC))]


def _session(rowcount: int = 3) -> AsyncMock:
    """Fake session whose execute() reports ``rowcount`` deleted rows."""
    session = AsyncMock()
    session.execute.return_value = MagicMock(rowcount=rowcount)
    return session


def _sql(session: AsyncMock) -> str:
    """Compile the single executed statement for Postgres."""
    return str(session.execute.call_args.args[0].compile(dialect=postgresql.dialect()))


@pytest.mark.asyncio
async def test_observation_purge_only_noncurrent_locked_ordered():
    """Deletes non-current rows oldest-first with SKIP LOCKED and the row limit."""
    session = _session()
    assert await observations.purge_expired_noncurrent_observations(session, CUTOFFS, limit=5) == 3
    sql = _sql(session)
    assert "is_current" in sql and "false" in sql.lower()
    assert "SKIP LOCKED" in sql and "ORDER BY" in sql and "LIMIT" in sql


@pytest.mark.asyncio
async def test_purges_noop_without_cutoffs():
    """Empty cutoffs never touch the database."""
    session = _session()
    assert await observations.purge_expired_noncurrent_observations(session, [], limit=5) == 0
    assert await documents.purge_expired_document_versions(session, [], limit=5) == 0
    session.execute.assert_not_called()


def test_current_reference_predicate_requires_current_and_valid_field():
    """Predicate is an EXISTS over current observations; unknown fields are rejected."""
    expr = observations.current_observation_references(column("v"), field="document_version_id")
    sql = str(expr.compile(dialect=postgresql.dialect()))
    assert "EXISTS" in sql and "is_current" in sql
    with pytest.raises(ValueError):
        observations.current_observation_references(column("v"), field="id")


@pytest.mark.asyncio
async def test_document_purge_keeps_current_bookmarked_and_protected():
    """Current version, bookmarks and the injected predicate all exclude versions."""
    session = _session(2)
    seen = []

    def protect(col):
        seen.append(col)
        return observations.current_observation_references(col, field="document_version_id")

    assert await documents.purge_expired_document_versions(
        session, CUTOFFS, limit=7, is_protected=protect
    ) == 2
    sql = _sql(session)
    assert len(seen) == 1
    assert "current_version" in sql
    assert "bookmarked_at IS NOT NULL" in sql
    assert sql.count("NOT (EXISTS") == 2
    assert "SKIP LOCKED" in sql
