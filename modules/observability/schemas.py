"""Public DTOs for /system/metrics and /system/runs (consumed by the Phase 11 operations screens)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.telemetry import TraceContext, Usage

RunKind = Literal["ingestion", "agent", "automation", "chat"]


class CounterRead(BaseModel):
    """One merged counter series; labels are bounded (route templates, job/tool names, outcomes)."""

    name: str
    labels: dict[str, str]
    value: float


class HistogramRead(BaseModel):
    """One merged latency histogram with bucket counts aligned to ``MetricsRead.buckets_ms`` plus overflow."""

    name: str
    labels: dict[str, str]
    count: int
    sum_ms: float
    p50_ms: float | None = None
    p95_ms: float | None = None
    buckets: list[int]


class MetricsRead(BaseModel):
    """Merged process-local metrics; absent processes simply contribute nothing (best effort)."""

    generated_at: datetime
    processes: list[str]
    buckets_ms: list[int]
    counters: list[CounterRead]
    histograms: list[HistogramRead]
    note: str = "Process-local, bounded, content-free. Token cost values are estimates only, never billing."


class RunRead(BaseModel):
    """One run of any kind with stable ID, timing, nullable usage and trace correlation IDs."""

    kind: RunKind
    id: str
    status: str
    error_code: str | None = None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None
    duration_ms: float | None = None
    usage: Usage | None = None
    trace: TraceContext = Field(default_factory=TraceContext)


class RunsRead(BaseModel):
    """Newest-first run list, capped by ``limit``."""

    items: list[RunRead]
    limit: int


HistoryRange = Literal["1h", "24h", "7d", "30d", "90d"]


class HistoryComponentRead(BaseModel):
    """Per-component availability counts and latency aggregates inside one history bucket."""

    up: int
    total: int
    latency_ms_avg: float | None = None
    latency_ms_max: int | None = None


class HistoryDbRead(BaseModel):
    """Per-bucket database aggregates derived from compact ``db_stats`` samples; None where unsampled."""

    pg_size_bytes_max: int | None = None
    pg_connections_avg: float | None = None
    pg_connections_max: int | None = None
    pg_cache_hit_ratio_avg: float | None = None
    pg_tps_avg: float | None = None
    redis_memory_bytes_max: int | None = None
    redis_ops_per_sec_avg: float | None = None
    redis_hit_ratio_avg: float | None = None


class HistoryBucketRead(BaseModel):
    """One time bucket that contains at least one health sample."""

    start: datetime
    samples: int
    overall_healthy: int
    queue_backlog_max: int
    components: dict[str, HistoryComponentRead]
    db: HistoryDbRead | None = None


class HistoryRead(BaseModel):
    """Bucketed system health history for the System status page."""

    range: HistoryRange
    bucket_seconds: int
    retention_days: int
    generated_at: datetime
    oldest_sample_at: datetime | None
    buckets: list[HistoryBucketRead]


class HistorySettingsRead(BaseModel):
    """Current history retention policy, automatic collection interval (0 = off) and revision."""

    retention_days: int
    sample_interval_minutes: int = 1
    revision: int


class HistorySettingsUpdate(BaseModel):
    """Revision-fenced settings change; at least one of retention or collection interval is required."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    retention_days: int | None = Field(default=None, ge=1, le=90)
    sample_interval_minutes: Literal[0, 1, 5, 15, 30, 60] | None = None

    @model_validator(mode="after")
    def _require_a_change(self) -> "HistorySettingsUpdate":
        """Reject a body that changes nothing so a revision is never bumped for a no-op."""
        if self.retention_days is None and self.sample_interval_minutes is None:
            raise ValueError("retention_days or sample_interval_minutes is required")
        return self


class DatabaseConnectionsRead(BaseModel):
    """Connection counts for the current database only (no sessions, users or addresses)."""

    active: int | None = None
    idle: int | None = None
    idle_in_transaction: int | None = None
    waiting: int | None = None
    total: int | None = None
    max: int | None = None


class DatabaseTransactionsRead(BaseModel):
    """Cumulative commit/rollback counters since the last stats reset."""

    commit: int | None = None
    rollback: int | None = None


class DatabaseTableRead(BaseModel):
    """Aggregate size, row and maintenance counters for one public application table."""

    name: str
    total_bytes: int | None = None
    live_rows: int | None = None
    dead_rows: int | None = None
    dead_ratio: float | None = None
    seq_scans: int | None = None
    index_scans: int | None = None
    last_autovacuum: str | None = None
    last_autoanalyze: str | None = None


class PostgresStatsRead(BaseModel):
    """Postgres aggregates; every field is None when its probe failed or was denied."""

    status: Literal["healthy", "unavailable"]
    version: str | None = None
    uptime_seconds: int | None = None
    database_size_bytes: int | None = None
    connections: DatabaseConnectionsRead | None = None
    cache_hit_ratio: float | None = None
    index_hit_ratio: float | None = None
    transactions: DatabaseTransactionsRead | None = None
    deadlocks: int | None = None
    temp_bytes: int | None = None
    locks_waiting: int | None = None
    longest_transaction_seconds: float | None = None
    tables: list[DatabaseTableRead] | None = None


class RedisStatsRead(BaseModel):
    """Redis aggregates from INFO; every field is None when INFO was denied."""

    status: Literal["healthy", "unavailable"]
    version: str | None = None
    uptime_seconds: int | None = None
    used_memory_bytes: int | None = None
    max_memory_bytes: int | None = None
    fragmentation_ratio: float | None = None
    connected_clients: int | None = None
    ops_per_sec: int | None = None
    keyspace_hit_ratio: float | None = None
    evicted_keys: int | None = None
    expired_keys: int | None = None
    keys_total: int | None = None
    last_save_status: Literal["ok", "err"] | None = None


class DatabaseStatsRead(BaseModel):
    """Realtime database metadata analysis response."""

    generated_at: str
    postgres: PostgresStatsRead
    redis: RedisStatsRead
