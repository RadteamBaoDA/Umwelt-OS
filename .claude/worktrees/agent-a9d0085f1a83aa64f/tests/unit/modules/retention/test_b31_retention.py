"""SQL-shape checks for the B3.1 bounded purges (no database: statements are compiled and inspected)."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from modules.automations import public as automations
from modules.chat import worker as chat_worker
from modules.connectors import scheduler
from modules.ingestion import public as ingestion

NOW = datetime(2026, 10, 13, tzinfo=UTC)


class _Result:
    rowcount = 2

    def all(self):
        return []


class _Session:
    async def commit(self) -> None:
        return None

    def __init__(self) -> None:
        self.sql: list[str] = []

    async def execute(self, statement):
        self.sql.append(
            str(
                statement.compile(
                    dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
                )
            )
        )
        return _Result()


class _Factory:
    def __init__(self, session: _Session) -> None:
        self.session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_exc):
        return None


@pytest.mark.asyncio
async def test_outbox_purge_covers_every_type_but_only_terminal_rows_and_is_bounded() -> None:
    session = _Session()
    # One statement per terminal status (each matches its partial index); the fake reports 2 rows per statement.
    assert await ingestion.purge_delivered_outbox(session, now=NOW) == 4
    assert len(session.sql) == 2
    assert "status = 'delivered'" in session.sql[0] and "status = 'failed'" in session.sql[1]
    sql = session.sql[0]
    # Terminal rows only: undelivered (pending/queued) work can never be purged.
    assert all("'pending'" not in q and "'queued'" not in q for q in session.sql)
    assert "LIMIT 1000" in sql and "id IN (SELECT" in sql
    # Stage fan-out events: 7 days; every other type (no type filter on that arm): 30 days by default.
    assert "ingestion.stage.requested" in sql and "ingestion.normalize.requested" in sql
    assert "2026-10-06" in sql and "2026-09-13" in sql


@pytest.mark.asyncio
async def test_outbox_purge_retention_days_is_configurable() -> None:
    session = _Session()
    await ingestion.purge_delivered_outbox(session, now=NOW, limit=50, retention_days=90)
    assert "LIMIT 50" in session.sql[0] and "2026-07-15" in session.sql[0]


@pytest.mark.asyncio
async def test_automation_run_purge_keeps_live_review_and_recent_runs() -> None:
    session = _Session()
    await automations.purge_finished_runs(session, now=NOW)
    sql = session.sql[0]
    assert "LIMIT 1000" in sql and "2026-09-13" in sql and "2026-10-12 23:00" in sql
    assert "'succeeded'" in sql and "'requires_review'" not in sql
    assert "'queued'" not in sql and "'running'" not in sql and "'awaiting_approval'" not in sql


@pytest.mark.asyncio
async def test_connector_history_purge_is_bounded_and_spares_live_requests_and_debited_windows() -> (
    None
):
    session = _Session()
    await scheduler.purge_collection_history(session, now=NOW)
    requests, sends, windows = session.sql
    assert "LIMIT 1000" in requests and "'queued'" not in requests and "2026-09-13" in requests
    assert "connector_provider_sends" in sends and "2026-10-06" in sends and "LIMIT 1000" in sends
    assert (
        "NOT (EXISTS" in windows and "connector_quota_debits" in windows and "2026-10-12" in windows
    )


@pytest.mark.asyncio
async def test_delta_purge_targets_only_old_terminal_run_deltas_with_a_limit() -> None:
    session = _Session()
    assert await chat_worker.purge_terminal_message_deltas(_Factory(session), now=NOW) == 2
    sql = session.sql[0]
    assert "message.delta" in sql and "LIMIT 5000" in sql and "id IN (SELECT" in sql
    assert "'completed', 'cancelled', 'failed'" in sql and "2026-10-12" in sql


@pytest.mark.asyncio
async def test_seen_keys_skip_measurement_providers_without_querying() -> None:
    session = SimpleNamespace()  # any query would raise AttributeError
    keys = [("a", "h")]
    kwargs = {"source_id": None, "source_generation": 1, "keys": keys}
    assert await ingestion.seen_record_keys(session, provider="open_meteo", **kwargs) == set()
    assert (
        await ingestion.seen_record_keys(
            session, provider="rss", source_id=None, source_generation=1, keys=[]
        )
        == set()
    )


@pytest.mark.asyncio
async def test_purged_deterministic_cleanup_event_is_republished_not_treated_as_a_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retention may delete a delivered ``document.cleanup.requested`` row; the reopen path must re-publish it."""
    from uuid import uuid4

    from modules.knowledge.documents import worker

    published: list[object] = []
    identity = SimpleNamespace(
        workspace_id=uuid4(),
        actor_user_id=1,
        membership_revision=1,
        configuration_revision=1,
        source_id=uuid4(),
        source_generation=1,
    )

    async def resolve(*_a, **_k):
        return identity

    async def missing_event(*_a, **_k):
        return None  # row was purged

    async def publish(_session, event, **_k):
        published.append(event)

    async def commit(*_a, **_k):
        return None

    class _Sess:
        async def rollback(self) -> None:
            return None

    monkeypatch.setattr(worker.documents, "resolve_document_cleanup_job_identity", resolve)
    monkeypatch.setattr(worker.ingestion, "get_document_cleanup_event", missing_event)
    monkeypatch.setattr(worker.ingestion, "publish_event", publish)
    monkeypatch.setattr(worker, "commit_with_replay", commit)
    operation_id = uuid4()
    assert await worker._reopen_cleanup_event(_Factory(_Sess()), False, operation_id, NOW) is True  # type: ignore[arg-type]
    assert [getattr(e, "type", None) for e in published] == ["document.cleanup.requested"]
