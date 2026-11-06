"""Database metadata analysis: null-on-denied sections, null ratios, forbidden fields, owner gate, sampler/bucket integration."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from redis.exceptions import ResponseError

from core.auth.dependencies import require_owner
from modules.observability import database_stats, history
from modules.observability.routes import router
from modules.observability.schemas import DatabaseStatsRead

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _result(first: Any = None, rows: Any = None) -> MagicMock:
    return MagicMock(first=lambda: first, all=lambda: rows or [])


def _pg_session(fail: set[int] | None = None) -> MagicMock:
    """Session answering the 5 row probes then the table query; indexes in ``fail`` raise (permission denied)."""
    results = [
        _result(SimpleNamespace(v="16.4 (Debian)", up=100.9, size=2048)),
        _result(
            SimpleNamespace(active=1, idle=2, iit=0, waiting=0, total=3, longest=1.25, max="100")
        ),
        _result(
            SimpleNamespace(
                xact_commit=10, xact_rollback=1, deadlocks=0, temp_bytes=0, blks_hit=0, blks_read=0
            )
        ),
        _result(SimpleNamespace(hit=99, read=1)),
        _result(SimpleNamespace(waiting=0)),
        _result(
            rows=[
                SimpleNamespace(
                    relname="documents",
                    total_bytes=10,
                    n_live_tup=0,
                    n_dead_tup=0,
                    seq_scan=1,
                    idx_scan=2,
                    last_autovacuum=NOW,
                    last_autoanalyze=None,
                )
            ]
        ),
    ]
    calls = iter(range(len(results)))

    async def execute(*_a: Any, **_k: Any) -> MagicMock:
        i = next(calls)
        if i in (fail or set()):
            raise RuntimeError("permission denied")
        return results[i]

    return MagicMock(execute=execute, rollback=AsyncMock())


def _redis(info: dict[str, Any]) -> MagicMock:
    async def get_info(name: str) -> dict[str, Any]:
        if name not in info:
            raise ResponseError("NOPERM")
        return info[name]

    return MagicMock(ping=AsyncMock(), info=get_info)


@pytest.mark.asyncio
async def test_stats_map_and_null_ratio_on_zero_denominator() -> None:
    redis = _redis(
        {
            "server": {"redis_version": "7.2.4", "uptime_in_seconds": 5},
            "memory": {"used_memory": 9, "maxmemory": 0, "mem_fragmentation_ratio": 1.1},
            "clients": {"connected_clients": 4},
            "keyspace": {"db0": {"keys": 40}, "db1": {"keys": 2}},
            "stats": {"keyspace_hits": 0, "keyspace_misses": 0, "instantaneous_ops_per_sec": 12},
            "persistence": {"rdb_last_bgsave_status": "ok"},
        }
    )
    out = await database_stats.read_database_stats(_pg_session(), redis)
    pg, rd = out["postgres"], out["redis"]
    assert (
        pg["version"] == "16.4" and pg["uptime_seconds"] == 100 and pg["connections"]["max"] == 100
    )
    assert pg["cache_hit_ratio"] is None and pg["index_hit_ratio"] == 0.99
    assert pg["tables"][0]["dead_ratio"] is None and pg["tables"][0]["last_autoanalyze"] is None
    assert (
        rd["keys_total"] == 42
        and rd["keyspace_hit_ratio"] is None
        and rd["last_save_status"] == "ok"
    )
    DatabaseStatsRead.model_validate(out)


@pytest.mark.asyncio
async def test_denied_sections_become_null_not_errors() -> None:
    out = await database_stats.read_database_stats(
        _pg_session({1, 3, 5}), _redis({"server": {"redis_version": "7"}})
    )
    pg, rd = out["postgres"], out["redis"]
    assert pg["status"] == "healthy" and pg["connections"] is None and pg["index_hit_ratio"] is None
    assert pg["tables"] is None and pg["transactions"] == {"commit": 10, "rollback": 1}
    assert (
        rd["status"] == "healthy" and rd["used_memory_bytes"] is None and rd["keys_total"] is None
    )
    DatabaseStatsRead.model_validate(out)


@pytest.mark.asyncio
async def test_redis_down_is_unavailable_and_output_has_no_forbidden_fields() -> None:
    redis = MagicMock(ping=AsyncMock(side_effect=ConnectionError("down")))
    out = await database_stats.read_database_stats(_pg_session(), redis)
    assert out["redis"]["status"] == "unavailable"
    blob = json.dumps(out).lower()
    assert not any(word in blob for word in ("query", "usename", "client_addr"))


def test_database_route_requires_owner() -> None:
    route = next(r for r in router.routes if getattr(r, "path", "") == "/api/v1/system/database")
    assert require_owner in [d.call for d in route.dependant.dependencies]  # type: ignore[attr-defined]


def test_compact_db_stats_drops_absent_values() -> None:
    full = {
        "postgres": {
            "database_size_bytes": 5,
            "transactions": {"commit": 3, "rollback": 1},
            "connections": {"total": 4},
            "cache_hit_ratio": None,
        },
        "redis": {"status": "unavailable"},
    }
    assert history.compact_db_stats(full) == {
        "pg_size_bytes": 5,
        "pg_connections_total": 4,
        "pg_xact_commit": 3,
        "pg_xact_rollback": 1,
    }


@pytest.mark.asyncio
async def test_sampler_survives_db_stats_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        history, "system_health", AsyncMock(return_value={"overall": "healthy", "components": {}})
    )
    monkeypatch.setattr(history, "queue_summary", AsyncMock(return_value={}))
    monkeypatch.setattr(history, "read_database_stats", AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(
        history,
        "read_history_settings",
        AsyncMock(return_value=SimpleNamespace(retention_days=7, sample_interval_minutes=1)),
    )
    monkeypatch.setattr(history, "purge_expired_samples", AsyncMock(return_value=0))
    session = MagicMock(
        add=MagicMock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
        scalar=AsyncMock(return_value=None),
    )
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    await history.sample_system_health(
        {"session_factory": lambda: session, "redis": object(), "settings": object()}
    )
    assert session.add.call_args.args[0].db_stats == {}
    session.commit.assert_awaited()


def _bucket(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(
        start=NOW,
        samples=2,
        overall_healthy=2,
        queue_backlog_max=0,
        pg_size_max=7,
        pg_conn_avg=3.0,
        pg_conn_max=5,
        pg_hit_avg=0.99,
        xact_first=100,
        xact_last=220,
        xact_t_first=0,
        xact_t_last=60,
        redis_mem_max=9,
        redis_ops_avg=2.0,
        redis_hit_avg=0.5,
    )
    return SimpleNamespace(**{**base, **over})


@pytest.mark.asyncio
async def test_history_bucket_has_db_fields_and_tps_null_on_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[MagicMock(all=lambda: [_bucket(), _bucket(xact_last=50)]), MagicMock(all=list)]
    )
    session.scalar = AsyncMock(return_value=NOW)
    monkeypatch.setattr(
        history, "read_history_settings", AsyncMock(return_value=SimpleNamespace(retention_days=7))
    )
    out = await history.read_history(session, "1h")
    assert out.buckets[0].db is not None and out.buckets[0].db.pg_tps_avg == 2.0
    assert (
        out.buckets[0].db.pg_connections_max == 5 and out.buckets[0].db.redis_hit_ratio_avg == 0.5
    )
    assert out.buckets[1].db is not None and out.buckets[1].db.pg_tps_avg is None
