"""System health history: sampler, bounded purge, settings CAS, range validation and bucket mapping (no DB)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.dialects import postgresql

from modules.observability import history
from modules.observability.schemas import HistoryRange, HistorySettingsUpdate

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_range_validation_and_bucket_sizes() -> None:
    adapter = TypeAdapter(HistoryRange)
    for value, size in {"1h": 60, "24h": 900, "7d": 7200, "30d": 21600, "90d": 86400}.items():
        assert adapter.validate_python(value) == value and history.BUCKET_SECONDS[value] == size
    with pytest.raises(ValidationError):
        adapter.validate_python("2d")


@pytest.mark.parametrize("body", [
    {"expected_revision": 1, "retention_days": 0},
    {"expected_revision": 1, "retention_days": 91},
    {"expected_revision": 0, "retention_days": 7},
    {"expected_revision": 1, "retention_days": 7, "extra": 1},
    {"expected_revision": 1},
    {"expected_revision": 1, "sample_interval_minutes": 2},
    {"expected_revision": 1, "sample_interval_minutes": 61},
])
def test_settings_update_rejects_invalid_bodies(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        HistorySettingsUpdate(**body)


@pytest.mark.asyncio
async def test_save_settings_409_on_stale_revision_and_bumps_on_match() -> None:
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(first=lambda: None))
    with pytest.raises(HTTPException) as err:
        await history.save_history_settings(session, expected_revision=1, retention_days=14)
    assert err.value.status_code == 409
    session.execute = AsyncMock(return_value=MagicMock(first=lambda: SimpleNamespace(retention_days=14, sample_interval_minutes=5, revision=2)))
    saved = await history.save_history_settings(session, expected_revision=1, retention_days=14)
    assert (saved.retention_days, saved.revision) == (14, 2)


@pytest.mark.asyncio
async def test_partial_update_only_sets_given_field() -> None:
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(first=lambda: SimpleNamespace(
        retention_days=7, sample_interval_minutes=15, revision=3)))
    saved = await history.save_history_settings(session, expected_revision=2, sample_interval_minutes=15)
    sql = str(session.execute.await_args.args[0].compile(dialect=postgresql.dialect()))
    assert "sample_interval_minutes=" in sql and "retention_days=" not in sql.split("RETURNING")[0]
    assert (saved.retention_days, saved.sample_interval_minutes) == (7, 15)
    assert HistorySettingsUpdate(expected_revision=1, sample_interval_minutes=0).sample_interval_minutes == 0


@pytest.mark.asyncio
async def test_purge_is_bounded_and_respects_retention() -> None:
    session = MagicMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(rowcount=3))
    assert await history.purge_expired_samples(session, now=NOW, retention_days=7, limit=5000) == 3
    stmt = session.execute.await_args.args[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    sql = str(stmt)
    assert "DELETE FROM system_health_samples" in sql and "LIMIT 5000" in sql
    assert "2025-12-25" in sql  # now - 7 days


@pytest.mark.asyncio
async def test_sampler_records_one_sample_and_survives_queue_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    health = {"overall": "degraded", "components": {
        "postgres": {"status": "healthy", "latency_ms": 2}, "redis": {"status": "unavailable", "latency_ms": None},
        "graph": {"status": "not_installed"}}}
    monkeypatch.setattr(history, "system_health", AsyncMock(return_value=health))
    monkeypatch.setattr(history, "queue_summary", AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(history, "read_database_stats", AsyncMock(return_value={}))
    monkeypatch.setattr(history, "read_history_settings", AsyncMock(return_value=SimpleNamespace(retention_days=7, sample_interval_minutes=1)))
    purge = AsyncMock(return_value=4)
    monkeypatch.setattr(history, "purge_expired_samples", purge)
    session = MagicMock(add=MagicMock(), flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock(), scalar=AsyncMock(return_value=None))
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    ctx = {"session_factory": lambda: session, "redis": object(), "settings": object()}
    assert await history.sample_system_health(ctx) == 4
    sample = session.add.call_args.args[0]
    assert sample.overall == "degraded" and sample.queue_backlog == 0
    assert sample.components["graph"] == {"status": "not_installed", "latency_ms": None}
    assert purge.await_args.kwargs["retention_days"] == 7
    session.commit.assert_awaited()


def _sampler_ctx(monkeypatch: pytest.MonkeyPatch, *, interval: int, newest: datetime | None) -> tuple[dict[str, Any], MagicMock, AsyncMock, AsyncMock]:
    """Wire the sampler to mocks with the given interval and newest-sample time."""
    health = AsyncMock(return_value={"overall": "healthy", "components": {}})
    monkeypatch.setattr(history, "system_health", health)
    monkeypatch.setattr(history, "queue_summary", AsyncMock(return_value={}))
    monkeypatch.setattr(history, "read_database_stats", AsyncMock(return_value={}))
    monkeypatch.setattr(history, "read_history_settings", AsyncMock(
        return_value=SimpleNamespace(retention_days=7, sample_interval_minutes=interval)))
    purge = AsyncMock(return_value=2)
    monkeypatch.setattr(history, "purge_expired_samples", purge)
    session = MagicMock(add=MagicMock(), flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock(), scalar=AsyncMock(return_value=newest))
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    return {"session_factory": lambda: session, "redis": object(), "settings": object()}, session, health, purge


@pytest.mark.asyncio
async def test_sampler_off_skips_probe_but_still_purges(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, session, health, purge = _sampler_ctx(monkeypatch, interval=0, newest=None)
    assert await history.sample_system_health(ctx) == 2
    health.assert_not_awaited()
    session.add.assert_not_called()
    purge.assert_awaited()


@pytest.mark.asyncio
async def test_sampler_skips_within_interval_and_inserts_after(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    ctx, session, health, _ = _sampler_ctx(monkeypatch, interval=5, newest=now - timedelta(minutes=3))
    assert await history.sample_system_health(ctx) == 0
    health.assert_not_awaited()
    session.add.assert_not_called()
    # 4m40s old is inside the 30s jitter tolerance of a 5-minute interval, so it is due.
    ctx, session, health, _ = _sampler_ctx(monkeypatch, interval=5, newest=now - timedelta(minutes=4, seconds=40))
    await history.sample_system_health(ctx)
    session.add.assert_called_once()


@pytest.mark.asyncio
async def test_read_history_maps_rows_with_null_latency_and_skips_empty_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    buckets = [SimpleNamespace(start=start, samples=2, overall_healthy=1, queue_backlog_max=None,
        pg_size_max=None, pg_conn_avg=None, pg_conn_max=None, pg_hit_avg=None, xact_first=None, xact_last=None,
        xact_t_first=None, xact_t_last=None, redis_mem_max=None, redis_ops_avg=None, redis_hit_avg=None)]
    comps = [
        SimpleNamespace(start=start, name="postgres", up=2, total=2, lat_avg=2.14, lat_max=5),
        SimpleNamespace(start=start, name="graph", up=0, total=0, lat_avg=None, lat_max=None),
    ]
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[MagicMock(all=lambda: buckets), MagicMock(all=lambda: comps)])
    session.scalar = AsyncMock(return_value=start)
    monkeypatch.setattr(history, "read_history_settings", AsyncMock(return_value=SimpleNamespace(retention_days=7)))
    out = await history.read_history(session, "24h")
    assert out.bucket_seconds == 900 and out.oldest_sample_at == start and len(out.buckets) == 1
    pg = out.buckets[0].components["postgres"]
    assert (pg.up, pg.total, pg.latency_ms_avg, pg.latency_ms_max) == (2, 2, 2.1, 5)
    graph = out.buckets[0].components["graph"]
    assert graph.total == 0 and graph.latency_ms_avg is None and out.buckets[0].queue_backlog_max == 0


def test_component_sql_encodes_up_and_total_rules() -> None:
    sql = str(history._COMPONENTS_SQL)
    assert "IN ('healthy', 'configured')" in sql
    assert "NOT IN ('not_configured', 'unconfigured', 'not_installed')" in sql
