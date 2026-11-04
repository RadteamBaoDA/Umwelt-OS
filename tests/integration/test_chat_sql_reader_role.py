"""Integration proof for the chat SQL tool's least-privilege execution on a real PostgreSQL server.

Unit tests only record statement order with a mock; these tests run the actual TEMP-view, GRANT,
``SET LOCAL transaction_read_only``, ``SET LOCAL ROLE`` and ``ROLLBACK TO SAVEPOINT`` sequence
against the migrated disposable ``bbd_test`` database. The validator is deliberately bypassed
(``_run_sql_as_reader`` is called directly) to prove the database layer holds on its own.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from modules.chat.db_query import _run_sql_as_reader

pytestmark = pytest.mark.integration


async def _identity(session: AsyncSession) -> tuple[str, str]:
    """Return ``(current_user, transaction_read_only)`` for the session's current transaction."""
    row = (await session.execute(text("SELECT current_user, current_setting('transaction_read_only')"))).one()
    return str(row[0]), str(row[1])


async def test_scoped_query_runs_as_reader_and_restores_session(db_session: AsyncSession) -> None:
    """A legitimate scoped query returns rows twice (plan cache) and leaves role/read-only untouched."""
    before = await _identity(db_session)
    for _ in range(2):
        rows = await _run_sql_as_reader(
            db_session,
            "SELECT count(*) AS n FROM scoped_observations",
            workspace_id=uuid4(),
            allowed_source_ids=[uuid4()],
            max_rows=5,
        )
        assert [dict(row) for row in rows] == [{"n": 0}]
    assert await _identity(db_session) == before
    assert before[1] == "off"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) AS n FROM chat_messages",
        "SELECT count(*) AS n FROM observations",
        "SELECT lo_from_bytea(0, 'x'::bytea) AS oid",
    ],
)
async def test_validator_bypass_is_denied_by_the_database(db_session: AsyncSession, sql: str) -> None:
    """Reads of base tables and large-object writes fail under the reader role even without layer 1."""
    before = await _identity(db_session)
    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            await _run_sql_as_reader(
                db_session, sql, workspace_id=uuid4(), allowed_source_ids=[uuid4()], max_rows=5
            )
    assert await _identity(db_session) == before
