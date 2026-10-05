"""Public DTOs for /system/metrics and /system/runs (consumed by the Phase 11 operations screens)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

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
