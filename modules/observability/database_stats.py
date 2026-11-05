"""Read-only, aggregate-only Postgres and Redis metadata for the System status page.

Only counts, sizes, ratios and timestamps leave this module: never query text, usernames, client
addresses, Redis key names or row contents. Every section is independently fault-tolerant (a denied or
timed-out probe yields ``None`` for its fields) and each database gets a ~1.5s total budget.
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

DB_BUDGET_SECONDS = 1.5
_PG_TABLE_LIMIT = 10


def _ratio(hit: Any, miss: Any) -> float | None:
    """Return ``hit / (hit + miss)`` rounded to 4 places, or None when unknown or the denominator is 0."""
    if hit is None or miss is None or (hit + miss) <= 0:
        return None
    return round(float(hit) / float(hit + miss), 4)


def _iso(value: datetime | None) -> str | None:
    """Serialize an optional timestamp as ISO-8601."""
    return None if value is None else value.isoformat()


async def _safe_rollback(session: AsyncSession) -> None:
    """Clear an aborted transaction so later probes can run; a failing rollback is ignored."""
    try:
        await session.rollback()
    except Exception:  # noqa: BLE001, S110  # rollback of an already-broken session is best-effort; S110 pass is intentional
        pass


async def _row(session: AsyncSession, sql: str) -> Any | None:
    """Run one read-only probe; any failure (permission, timeout, closed connection) returns None.

    Rolls back on failure so one denied query cannot leave the transaction aborted for later probes.
    """
    try:
        return (await session.execute(text(sql))).first()
    except Exception:  # noqa: BLE001 - a failed section must degrade to nulls, never a 500
        await _safe_rollback(session)
        return None


async def _pg_tables(session: AsyncSession) -> list[dict[str, Any]] | None:
    """Return the top public tables by total size with row, scan and vacuum aggregates, or None."""
    try:
        rows = (await session.execute(text(
            "SELECT relname, pg_total_relation_size(relid) AS total_bytes, n_live_tup, n_dead_tup, "
            "seq_scan, idx_scan, last_autovacuum, last_autoanalyze FROM pg_stat_user_tables "
            "WHERE schemaname = 'public' ORDER BY 2 DESC LIMIT :n"), {"n": _PG_TABLE_LIMIT})).all()
    except Exception:  # noqa: BLE001  # a failed stats section degrades to None, never a 500
        await _safe_rollback(session)
        return None
    return [{
        "name": r.relname, "total_bytes": r.total_bytes, "live_rows": r.n_live_tup, "dead_rows": r.n_dead_tup,
        "dead_ratio": None if not (r.n_live_tup + r.n_dead_tup) else round(r.n_dead_tup / (r.n_live_tup + r.n_dead_tup), 4),
        "seq_scans": r.seq_scan, "index_scans": r.idx_scan,
        "last_autovacuum": _iso(r.last_autovacuum), "last_autoanalyze": _iso(r.last_autoanalyze),
    } for r in rows]


async def _postgres(session: AsyncSession) -> dict[str, Any]:
    """Collect Postgres aggregates for the current database; each probe fails to nulls on its own."""
    meta = await _row(session, (
        "SELECT current_setting('server_version') AS v, "
        "extract(epoch FROM now() - pg_postmaster_start_time()) AS up, "
        "pg_database_size(current_database()) AS size"))
    conns = await _row(session, (
        "SELECT count(*) FILTER (WHERE state = 'active') AS active, count(*) FILTER (WHERE state = 'idle') AS idle, "
        "count(*) FILTER (WHERE state LIKE 'idle in transaction%') AS iit, "
        "count(*) FILTER (WHERE wait_event_type = 'Lock') AS waiting, count(*) AS total, "
        "max(extract(epoch FROM now() - xact_start)) AS longest, current_setting('max_connections') AS max "
        "FROM pg_stat_activity WHERE datname = current_database()"))
    db = await _row(session, (
        "SELECT xact_commit, xact_rollback, deadlocks, temp_bytes, blks_hit, blks_read "
        "FROM pg_stat_database WHERE datname = current_database()"))
    idx = await _row(session, (
        "SELECT sum(idx_blks_hit)::bigint AS hit, sum(idx_blks_read)::bigint AS read FROM pg_statio_user_tables"))
    locks = await _row(session, "SELECT count(*) AS waiting FROM pg_locks WHERE NOT granted")
    tables = await _pg_tables(session)
    return {
        "status": "healthy" if meta is not None else "unavailable",
        "version": None if meta is None else str(meta.v).split(" ")[0],
        "uptime_seconds": None if meta is None or meta.up is None else int(meta.up),
        "database_size_bytes": None if meta is None else int(meta.size),
        "connections": None if conns is None else {
            "active": conns.active, "idle": conns.idle, "idle_in_transaction": conns.iit,
            "waiting": conns.waiting, "total": conns.total, "max": int(conns.max)},
        "cache_hit_ratio": None if db is None else _ratio(db.blks_hit, db.blks_read),
        "index_hit_ratio": None if idx is None else _ratio(idx.hit, idx.read),
        "transactions": None if db is None else {"commit": db.xact_commit, "rollback": db.xact_rollback},
        "deadlocks": None if db is None else db.deadlocks,
        "temp_bytes": None if db is None else db.temp_bytes,
        "locks_waiting": None if locks is None else locks.waiting,
        "longest_transaction_seconds": (
            None if conns is None or conns.longest is None else round(float(conns.longest), 1)),
        "tables": tables,
    }


async def _redis(redis: Redis) -> dict[str, Any]:
    """Collect Redis aggregates from INFO sections; ACL-denied sections leave their fields None."""
    out: dict[str, Any] = {
        "status": "unavailable", "version": None, "uptime_seconds": None, "used_memory_bytes": None,
        "max_memory_bytes": None, "fragmentation_ratio": None, "connected_clients": None, "ops_per_sec": None,
        "keyspace_hit_ratio": None, "evicted_keys": None, "expired_keys": None, "keys_total": None,
        "last_save_status": None,
    }

    async def section(name: str) -> dict[str, Any] | None:
        """Fetch one INFO section; denied/failed returns None so only that section's fields stay None."""
        try:
            return dict(await redis.info(name))
        except Exception:  # noqa: BLE001 - INFO may be ACL-denied
            return None

    try:
        async with asyncio.timeout(DB_BUDGET_SECONDS):
            await redis.ping()
            out["status"] = "healthy"
            srv = await section("server") or {}
            mem = await section("memory") or {}
            cli = await section("clients") or {}
            st = await section("stats") or {}
            ks = await section("keyspace")
            per = await section("persistence") or {}
    except Exception:  # noqa: BLE001 - ping failure or budget exhausted
        return out
    out["version"] = srv.get("redis_version")
    out["uptime_seconds"] = srv.get("uptime_in_seconds")
    out["used_memory_bytes"] = mem.get("used_memory")
    out["max_memory_bytes"] = mem.get("maxmemory")
    out["fragmentation_ratio"] = mem.get("mem_fragmentation_ratio")
    out["connected_clients"] = cli.get("connected_clients")
    out["ops_per_sec"] = st.get("instantaneous_ops_per_sec")
    out["keyspace_hit_ratio"] = _ratio(st.get("keyspace_hits"), st.get("keyspace_misses"))
    out["evicted_keys"] = st.get("evicted_keys")
    out["expired_keys"] = st.get("expired_keys")
    if ks is not None:  # an empty keyspace section is valid (0 keys); None means INFO was denied
        out["keys_total"] = sum(int(v.get("keys", 0)) for k, v in ks.items() if k.startswith("db") and isinstance(v, dict))
    save = per.get("rdb_last_bgsave_status")
    out["last_save_status"] = save if save in ("ok", "err") else None
    return out


async def read_database_stats(session: AsyncSession, redis: Redis) -> dict[str, Any]:
    """Return ``{generated_at, postgres, redis}`` aggregates; never raises and never exposes names or contents.

    Postgres and Redis are probed concurrently; each has a 1.5s budget, and a blown budget degrades that
    database to ``unavailable`` rather than failing the request.
    """
    async def pg() -> dict[str, Any]:
        """Postgres section under its time budget."""
        try:
            async with asyncio.timeout(DB_BUDGET_SECONDS):
                return await _postgres(session)
        except Exception:  # noqa: BLE001  # a failed postgres section degrades to status=unavailable
            await _safe_rollback(session)
            return {"status": "unavailable"}

    postgres, redis_stats = await asyncio.gather(pg(), _redis(redis))
    return {"generated_at": datetime.now(UTC).isoformat(), "postgres": postgres, "redis": redis_stats}
