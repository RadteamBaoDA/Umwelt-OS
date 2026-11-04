"""Replay CLI: argument rules, owner/failed-only selection, dry-run default and compare-and-set requeue."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from core.events import replay

OWNER = ["--owner-user-id", "7"]


def test_arguments_require_exactly_one_selector_and_type_needs_since():
    """``--event-id`` xor ``--type`` (+ ``--since``); limits and owner id are bounded."""
    event_id = str(uuid4())
    assert replay.parse_args([*OWNER, "--event-id", event_id]).execute is False
    for bad in ([*OWNER], [*OWNER, "--event-id", event_id, "--type", "x", "--since", "2026-01-01T00:00:00Z"],
                [*OWNER, "--type", "x"], [*OWNER, "--event-id", event_id, "--limit", "0"],
                [*OWNER, "--event-id", event_id, "--limit", "1001"], ["--owner-user-id", "0", "--event-id", event_id],
                [*OWNER, "--type", "x", "--since", "2026-01-01T00:00:00"]):
        with pytest.raises(SystemExit):
            replay.parse_args(bad)
    parsed = replay.parse_args([*OWNER, "--type", "x", "--since", "2026-01-01T00:00:00Z", "--execute"])
    assert parsed.since == datetime(2026, 1, 1, tzinfo=UTC) and parsed.execute is True


class FakeSession:
    """Records executed statements and scripted rows for the selection query."""

    def __init__(self, rows):
        self.rows, self.statements, self.commits = rows, [], 0

    async def execute(self, stmt):
        """Return rows for a select, a rowcount for an update."""
        self.statements.append(stmt)
        if stmt.is_select:
            return SimpleNamespace(all=lambda: self.rows)
        return SimpleNamespace(rowcount=len(self.rows))

    async def commit(self):
        """Count commits."""
        self.commits += 1


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def _row():
    return SimpleNamespace(id=uuid4(), type="document.version.ready", created_at=datetime.now(UTC))


@pytest.mark.asyncio
async def test_dry_run_selects_failed_owner_rows_and_writes_nothing(capsys):
    """The default mode only selects: failed status, owner principal and workspace-owner join."""
    session = FakeSession([_row()])
    args = replay.parse_args([*OWNER, "--type", "document.version.ready", "--since", "2026-01-01T00:00:00Z"])
    assert await replay.run(args, session) == 0
    assert len(session.statements) == 1 and session.commits == 0
    sql = _sql(session.statements[0])
    assert "event_outbox.status = 'failed'" in sql and "event_outbox.actor_user_id = 7" in sql
    assert "workspaces.owner_user_id = event_outbox.actor_user_id" in sql
    assert "DRY-RUN" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_execute_requeues_with_a_failed_status_compare_and_set():
    """``--execute`` resets status/attempt/dispatch only for rows still ``failed`` and commits once."""
    session = FakeSession([_row(), _row()])
    args = replay.parse_args([*OWNER, "--event-id", str(uuid4()), "--execute"])
    assert await replay.run(args, session) == 0
    assert session.commits == 1 and len(session.statements) == 2
    update_sql = _sql(session.statements[1])
    assert "SET status='pending'" in update_sql and "dispatched_at=NULL" in update_sql
    assert "event_outbox.status = 'failed'" in update_sql


@pytest.mark.asyncio
async def test_missing_single_event_is_a_nonzero_exit():
    """An unknown/ineligible ``--event-id`` reports nothing to replay and exits 1 without writing."""
    session = FakeSession([])
    args = replay.parse_args([*OWNER, "--event-id", str(uuid4()), "--execute"])
    assert await replay.run(args, session) == 1
    assert session.commits == 0 and len(session.statements) == 1


@pytest.mark.asyncio
async def test_requeue_of_nothing_issues_no_statement():
    """An empty id list short-circuits."""
    session = FakeSession([])
    assert await replay.requeue(session, [], now=datetime.now(UTC)) == 0
    assert session.statements == []
