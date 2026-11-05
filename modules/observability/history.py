"""Persisted system health history: a per-minute sampler, bounded retention purge and SQL-bucketed reads.

The data is instance-level operational telemetry (component statuses, latencies, queue depth). It holds
no personal data and is not tied to an account or workspace, so account erasure leaves it untouched
(registered as special instance tables) and retention alone bounds it.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from redis.asyncio import Redis
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from core.errors import ConflictError
from core.system.health import system_health
from core.worker_context import worker_session_factory
from modules.observability.database_stats import read_database_stats
from modules.observability.models import SystemHealthSample, SystemHistorySettings
from modules.observability.operations import queue_summary
from modules.observability.schemas import (
    HistoryBucketRead,
    HistoryComponentRead,
    HistoryDbRead,
    HistoryRange,
    HistoryRead,
    HistorySettingsRead,
)

BUCKET_SECONDS: dict[str, int] = {"1h": 60, "24h": 900, "7d": 7200, "30d": 21600, "90d": 86400}
RANGE_WINDOW: dict[str, timedelta] = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
}
PURGE_BATCH = 5000
_ACTIVE_STAGES = ("pending", "queued", "running", "retrying")


async def read_history_settings(session: AsyncSession) -> HistorySettingsRead:
    """Return the settings singleton, defaulting to 7 days/1 minute/revision 1 if the row is somehow absent."""
    row = await session.scalar(select(SystemHistorySettings).where(SystemHistorySettings.id == 1))
    return HistorySettingsRead(
        retention_days=row.retention_days if row else 7,
        sample_interval_minutes=row.sample_interval_minutes if row else 1,
        revision=row.revision if row else 1,
    )


async def save_history_settings(
    session: AsyncSession,
    *,
    expected_revision: int,
    retention_days: int | None = None,
    sample_interval_minutes: int | None = None,
) -> HistorySettingsRead:
    """Compare-and-set the given fields (None = keep); raises 409 when ``expected_revision`` is stale. Caller commits."""
    changes: dict[str, Any] = {
        k: v
        for k, v in (
            ("retention_days", retention_days),
            ("sample_interval_minutes", sample_interval_minutes),
        )
        if v is not None
    }
    result = await session.execute(
        update(SystemHistorySettings)
        .where(SystemHistorySettings.id == 1, SystemHistorySettings.revision == expected_revision)
        .values(
            **changes, revision=SystemHistorySettings.revision + 1, updated_at=datetime.now(UTC)
        )
        .returning(
            SystemHistorySettings.retention_days,
            SystemHistorySettings.sample_interval_minutes,
            SystemHistorySettings.revision,
        )
    )
    row = result.first()
    if row is None:
        raise ConflictError(detail="History settings changed; reload and retry")
    return HistorySettingsRead(
        retention_days=row.retention_days,
        sample_interval_minutes=row.sample_interval_minutes,
        revision=row.revision,
    )


async def purge_expired_samples(
    session: AsyncSession,
    *,
    now: datetime,
    retention_days: int,
    limit: int = PURGE_BATCH,
) -> int:
    """Delete at most ``limit`` samples older than the retention window; the rest go on later minutes."""
    oldest = (
        select(SystemHealthSample.id)
        .where(SystemHealthSample.sampled_at < now - timedelta(days=retention_days))
        .limit(limit)
    )
    result = await session.execute(
        delete(SystemHealthSample).where(SystemHealthSample.id.in_(oldest))
    )
    return int(cast(Any, result).rowcount)


def compact_db_stats(stats: dict[str, Any]) -> dict[str, Any]:
    """Reduce full database stats to the compact numeric sample stored per minute; absent values are omitted."""
    pg, rd = stats.get("postgres") or {}, stats.get("redis") or {}
    tx = pg.get("transactions") or {}
    compact = {
        "pg_size_bytes": pg.get("database_size_bytes"),
        "pg_connections_total": (pg.get("connections") or {}).get("total"),
        "pg_cache_hit_ratio": pg.get("cache_hit_ratio"),
        "pg_xact_commit": tx.get("commit"),
        "pg_xact_rollback": tx.get("rollback"),
        "redis_used_memory_bytes": rd.get("used_memory_bytes"),
        "redis_ops_per_sec": rd.get("ops_per_sec"),
        "redis_keyspace_hit_ratio": rd.get("keyspace_hit_ratio"),
    }
    return {k: v for k, v in compact.items() if v is not None}


async def sample_system_health(ctx: dict[str, object]) -> int:
    """Worker cron (fires every minute): record one health sample unless the interval says to wait, then purge.

    Interval 0 means collection is off; otherwise a sample is skipped while the newest one is younger than
    ``interval - 30s`` (tolerance for cron jitter so a 5-minute interval yields one sample per 5 minutes).
    Expired samples are purged whenever no sample is taken because collection is off, so retention keeps applying.

    Never raises on probe failure: ``system_health`` already reports failed probes as ``unavailable``.
    The queue backlog (active ingestion stages) is best effort and 0 when unreadable.
    Returns the number of purged rows.
    """
    factory = worker_session_factory(ctx)
    now = datetime.now(UTC)
    async with factory() as session:
        cfg = await read_history_settings(session)
        if cfg.sample_interval_minutes == 0:
            purged = await purge_expired_samples(
                session, now=now, retention_days=cfg.retention_days
            )
            await session.commit()
            return purged
        newest = await session.scalar(select(func.max(SystemHealthSample.sampled_at)))
        if newest is not None and newest > now - timedelta(
            minutes=cfg.sample_interval_minutes
        ) + timedelta(seconds=30):
            return 0
        health = await system_health(
            session, cast(Redis, ctx["redis"]), cast(Settings, ctx["settings"])
        )
        backlog = 0
        try:
            stages = (await queue_summary(session, instance_operator=True)).get(
                "ingestion_stages"
            ) or {}
            backlog = sum(
                int(n)
                for status, n in cast(dict[str, int], stages).items()
                if status in _ACTIVE_STAGES
            )
        except Exception:  # noqa: BLE001 - telemetry must not fail because the queue read did
            await session.rollback()
        db_stats: dict[str, Any] = {}
        try:
            db_stats = compact_db_stats(
                await read_database_stats(session, cast(Redis, ctx["redis"]))
            )
        except Exception:  # noqa: BLE001 - the sample must still be written without db stats
            await session.rollback()
        components = {
            name: {"status": c["status"], "latency_ms": c.get("latency_ms")}
            for name, c in health["components"].items()
        }
        session.add(
            SystemHealthSample(
                sampled_at=now,
                overall=health["overall"],
                components=components,
                queue_backlog=backlog,
                db_stats=db_stats,
            )
        )
        await session.flush()
        purged = await purge_expired_samples(session, now=now, retention_days=cfg.retention_days)
        await session.commit()
        return purged


# Buckets are floor(epoch / size) * size so boundaries are stable across requests; aggregation stays in SQL.
_BUCKETS_SQL = text("""
SELECT to_timestamp(floor(extract(epoch FROM sampled_at) / :b) * :b) AS start,
       count(*) AS samples, count(*) FILTER (WHERE overall = 'healthy') AS overall_healthy,
       max(queue_backlog) AS queue_backlog_max,
       max((db_stats->>'pg_size_bytes')::bigint) AS pg_size_max,
       avg((db_stats->>'pg_connections_total')::numeric) AS pg_conn_avg,
       max((db_stats->>'pg_connections_total')::int) AS pg_conn_max,
       avg((db_stats->>'pg_cache_hit_ratio')::numeric) AS pg_hit_avg,
       (array_agg((db_stats->>'pg_xact_commit')::numeric + (db_stats->>'pg_xact_rollback')::numeric
                  ORDER BY sampled_at) FILTER (WHERE db_stats ? 'pg_xact_commit' AND db_stats ? 'pg_xact_rollback'))[1] AS xact_first,
       (array_agg((db_stats->>'pg_xact_commit')::numeric + (db_stats->>'pg_xact_rollback')::numeric
                  ORDER BY sampled_at DESC) FILTER (WHERE db_stats ? 'pg_xact_commit' AND db_stats ? 'pg_xact_rollback'))[1] AS xact_last,
       min(extract(epoch FROM sampled_at)) FILTER (WHERE db_stats ? 'pg_xact_commit' AND db_stats ? 'pg_xact_rollback') AS xact_t_first,
       max(extract(epoch FROM sampled_at)) FILTER (WHERE db_stats ? 'pg_xact_commit' AND db_stats ? 'pg_xact_rollback') AS xact_t_last,
       max((db_stats->>'redis_used_memory_bytes')::bigint) AS redis_mem_max,
       avg((db_stats->>'redis_ops_per_sec')::numeric) AS redis_ops_avg,
       avg((db_stats->>'redis_keyspace_hit_ratio')::numeric) AS redis_hit_avg
FROM system_health_samples WHERE sampled_at >= :since GROUP BY 1 ORDER BY 1
""")
_COMPONENTS_SQL = text("""
SELECT to_timestamp(floor(extract(epoch FROM s.sampled_at) / :b) * :b) AS start, e.key AS name,
       count(*) FILTER (WHERE e.value->>'status' IN ('healthy', 'configured')) AS up,
       count(*) FILTER (WHERE e.value->>'status' NOT IN ('not_configured', 'unconfigured', 'not_installed')) AS total,
       avg((e.value->>'latency_ms')::numeric) AS lat_avg, max((e.value->>'latency_ms')::numeric) AS lat_max
FROM system_health_samples s, jsonb_each(s.components) e WHERE s.sampled_at >= :since GROUP BY 1, 2
""")


def _round(value: Any, digits: int) -> float | None:
    """Round an optional SQL numeric to a float."""
    return None if value is None else round(float(value), digits)


def _db_bucket(r: Any) -> HistoryDbRead:
    """Map one bucket row to DB aggregates; TPS needs >=2 sampled counters and a non-reset (non-decreasing) delta."""
    tps = None
    if (
        r.xact_first is not None
        and r.xact_last is not None
        and r.xact_t_last > r.xact_t_first
        and r.xact_last >= r.xact_first
    ):
        tps = round(float(r.xact_last - r.xact_first) / float(r.xact_t_last - r.xact_t_first), 2)
    return HistoryDbRead(
        pg_size_bytes_max=None if r.pg_size_max is None else int(r.pg_size_max),
        pg_connections_avg=_round(r.pg_conn_avg, 1),
        pg_connections_max=None if r.pg_conn_max is None else int(r.pg_conn_max),
        pg_cache_hit_ratio_avg=_round(r.pg_hit_avg, 4),
        pg_tps_avg=tps,
        redis_memory_bytes_max=None if r.redis_mem_max is None else int(r.redis_mem_max),
        redis_ops_per_sec_avg=_round(r.redis_ops_avg, 1),
        redis_hit_ratio_avg=_round(r.redis_hit_avg, 4),
    )


async def read_history(session: AsyncSession, range_: HistoryRange) -> HistoryRead:
    """Return the bucketed history for ``range_``; only buckets holding samples are returned, ascending."""
    size = BUCKET_SECONDS[range_]
    now = datetime.now(UTC)
    params = {"b": size, "since": now - RANGE_WINDOW[range_]}
    rows = (await session.execute(_BUCKETS_SQL, params)).all()
    comps: dict[datetime, dict[str, HistoryComponentRead]] = {}
    for r in (await session.execute(_COMPONENTS_SQL, params)).all():
        comps.setdefault(r.start, {})[r.name] = HistoryComponentRead(
            up=r.up,
            total=r.total,
            latency_ms_avg=None if r.lat_avg is None else round(float(r.lat_avg), 1),
            latency_ms_max=None if r.lat_max is None else int(r.lat_max),
        )
    oldest = await session.scalar(
        select(SystemHealthSample.sampled_at).order_by(SystemHealthSample.sampled_at).limit(1)
    )
    return HistoryRead(
        range=range_,
        bucket_seconds=size,
        retention_days=(await read_history_settings(session)).retention_days,
        generated_at=now,
        oldest_sample_at=oldest,
        buckets=[
            HistoryBucketRead(
                start=r.start,
                samples=r.samples,
                overall_healthy=r.overall_healthy,
                queue_backlog_max=r.queue_backlog_max or 0,
                components=comps.get(r.start, {}),
                db=_db_bucket(r),
            )
            for r in rows
        ],
    )
