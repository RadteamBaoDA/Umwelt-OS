"""Retention cron drains backlogs in passes; terminal run/batch purge keeps batches that still hold observations."""

from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from modules.ingestion import worker as ingestion_worker
from modules.ingestion._purge import _purge_terminal_runs
from modules.ingestion._worker import upload


class Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None

    async def commit(self):
        return None


@pytest.mark.asyncio
async def test_purge_repeats_full_passes_until_a_short_one(monkeypatch):
    passes = [1000, 1000, 17]
    calls = []

    async def outbox(_session, *, limit, **_k):
        calls.append(limit)
        return passes.pop(0)

    async def none(_session, *, limit, **_k):
        return 0

    monkeypatch.setattr(upload.ingestion_api, "purge_expired_collection_receipts", none)
    monkeypatch.setattr(upload.connectors, "purge_collection_history", none)
    monkeypatch.setattr(upload.ingestion_api, "purge_delivered_outbox", outbox)
    monkeypatch.setattr(upload.ingestion_api, "purge_expired_datasource_data", none)
    await ingestion_worker.purge_collection_receipts({"session_factory": lambda: Session()})
    assert calls == [1000, 1000, 1000] and not passes


@pytest.mark.asyncio
async def test_purge_stops_when_time_budget_is_spent(monkeypatch):
    calls = []

    async def endless(_session, *, limit, **_k):
        calls.append(1)
        return limit

    async def none(_session, *, limit, **_k):
        return 0

    monkeypatch.setattr(upload, "PURGE_TIME_BUDGET_SECONDS", -1.0)
    monkeypatch.setattr(upload.ingestion_api, "purge_expired_collection_receipts", none)
    monkeypatch.setattr(upload.connectors, "purge_collection_history", endless)
    monkeypatch.setattr(upload.ingestion_api, "purge_delivered_outbox", none)
    monkeypatch.setattr(upload.ingestion_api, "purge_expired_datasource_data", none)
    await ingestion_worker.purge_collection_receipts({"session_factory": lambda: Session()})
    assert calls == [1]


@pytest.mark.asyncio
async def test_terminal_run_purge_only_targets_unreferenced_terminal_batches():
    from datetime import UTC, datetime
    from uuid import uuid4

    seen = []

    class Db:
        async def execute(self, statement):
            seen.append(str(statement.compile(dialect=postgresql.dialect())))
            return SimpleNamespace(rowcount=4)

    deleted = await _purge_terminal_runs(
        Db(), [(uuid4(), datetime(2026, 1, 1, tzinfo=UTC))], now=datetime.now(UTC), limit=10
    )
    sql = seen[0]
    assert deleted == 4 and "DELETE FROM ingestion_batches" in sql
    assert (
        "ingestion_runs.status IN" in sql and "NOT (EXISTS" in sql and "source_observations" in sql
    )
    assert "SKIP LOCKED" in sql
    assert (
        "ingestion_batches.batch_key LIKE" in sql
    )  # connector-collection batches only, never file:/crawl:


@pytest.mark.asyncio
async def test_terminal_run_purge_failure_is_isolated_in_a_savepoint():
    from datetime import UTC, datetime

    from modules.ingestion._purge import _purge_terminal_runs_isolated

    events = []

    class Nested:
        async def __aenter__(self):
            events.append("savepoint")

        async def __aexit__(self, *exc):
            events.append("rollback" if exc[0] else "release")
            return False

    class Db:
        def begin_nested(self):
            return Nested()

        async def execute(self, statement):
            raise RuntimeError("boom")

    assert await _purge_terminal_runs_isolated(Db(), [], now=datetime.now(UTC), limit=5) == 0
    assert events == ["savepoint", "rollback"]
