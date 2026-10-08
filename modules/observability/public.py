"""Observability service: merged metrics snapshots and a read-only cross-module run listing."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.telemetry import (
    BUCKETS_MS,
    RunMeta,
    TraceContext,
    Usage,
    flush_metrics,
    read_snapshots,
    usage_from_response,
)
from modules.agents import public as agents
from modules.automations import public as automations
from modules.chat import public as chat
from modules.ingestion import public as ingestion
from modules.observability.models import MaintenanceSummary
from modules.observability.schemas import (
    CounterRead,
    HistogramRead,
    MetricsRead,
    RunKind,
    RunRead,
    RunsRead,
)
from modules.settings.schemas import MaintenanceSummaryRead


def _quantile(buckets: list[int], q: float) -> float | None:
    """Estimate a quantile as the upper bound of the bucket containing it (overflow bucket -> None)."""
    total = sum(buckets)
    if not total:
        return None
    seen = 0
    for index, n in enumerate(buckets):
        seen += n
        if seen >= q * total:
            return float(BUCKETS_MS[index]) if index < len(BUCKETS_MS) else None
    return None


async def read_metrics(redis: Any) -> MetricsRead:
    """Flush this process, then sum every live process snapshot by (name, labels)."""
    await flush_metrics(redis, force=True)
    snapshots = await read_snapshots(redis)
    counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
    hists: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}
    for snap in snapshots:
        for row in snap.get("counters", []):
            key = (row["name"], tuple(sorted(row["labels"].items())))
            counters[key] = counters.get(key, 0) + row["value"]
        for row in snap.get("histograms", []):
            key = (row["name"], tuple(sorted(row["labels"].items())))
            into = hists.setdefault(key, {"count": 0, "sum_ms": 0.0, "buckets": [0] * len(row["buckets"])})
            into["count"] += row["count"]
            into["sum_ms"] += row["sum_ms"]
            into["buckets"] = [a + b for a, b in zip(into["buckets"], row["buckets"], strict=False)]
    return MetricsRead(
        generated_at=datetime.now(UTC),
        processes=sorted(s["process"] for s in snapshots),
        buckets_ms=list(BUCKETS_MS),
        counters=[CounterRead(name=n, labels=dict(lb), value=v) for (n, lb), v in sorted(counters.items())],
        histograms=[HistogramRead(name=n, labels=dict(lb), count=h["count"], sum_ms=round(h["sum_ms"], 3),
                                  p50_ms=_quantile(h["buckets"], 0.5), p95_ms=_quantile(h["buckets"], 0.95),
                                  buckets=h["buckets"]) for (n, lb), h in sorted(hists.items())],
    )


def _to_read(meta: RunMeta) -> RunRead:
    """Map owner metadata to the public DTO; duration only for finished runs, usage null when unknown."""
    duration = (meta.finished_at - meta.created_at).total_seconds() * 1000 if meta.finished_at else None
    usage: Usage | None = None
    if isinstance(meta.token_usage, int):
        # AgentRun stores provider total_tokens; its input/output split is unavailable.
        usage = Usage(model_identity=meta.model_identity)
    elif isinstance(meta.token_usage, dict):
        usage = usage_from_response({"usage": meta.token_usage}, model_identity=meta.model_identity)
    trace = TraceContext(
        ingestion_run_id=meta.id if meta.kind == "ingestion" else None,
        agent_run_id=meta.id if meta.kind == "agent" else meta.origin_run_id)
    return RunRead(kind=meta.kind, id=meta.id, status=meta.status, error_code=meta.error_code,
                   created_at=meta.created_at, updated_at=meta.updated_at, finished_at=meta.finished_at,
                   duration_ms=duration, usage=usage, trace=trace)


async def list_runs(
    session: AsyncSession, *, limit: int = 50, kind: RunKind | None = None, instance_operator: bool,
) -> RunsRead:
    """Return the newest ``limit`` runs across modules via each owner's public ``list_run_meta`` (metadata only).

    ``instance_operator`` must be True only from a real ``require_owner`` route; only Ingestion's
    instance aggregate takes it, other kinds keep their owner contracts and gain no invented scope.
    """
    async def ingestion_runs(s: AsyncSession, n: int) -> list[RunMeta]:
        return await ingestion.list_run_meta(s, n, instance_operator=instance_operator)

    sources = {"ingestion": ingestion_runs, "agent": agents.list_run_meta,
               "automation": automations.list_run_meta, "chat": chat.list_run_meta}
    metas: list[RunMeta] = []
    for name, fetch in sources.items():
        if kind in (None, name):
            metas.extend(await fetch(session, limit))
    items = sorted((_to_read(m) for m in metas), key=lambda i: i.created_at, reverse=True)
    return RunsRead(items=items[:limit], limit=limit)


async def get_run_by_id(
    session: AsyncSession, kind: RunKind, run_id: UUID, *, instance_operator: bool,
) -> RunRead | None:
    """Read one safe owner-provided run projection independently of the recent list window.

    Explicit per-kind dispatch: only Ingestion receives ``instance_operator``.
    """
    if kind == "ingestion":
        meta = await ingestion.get_run_meta_by_id(session, run_id, instance_operator=instance_operator)
    else:
        meta = await {"agent": agents, "automation": automations, "chat": chat}[kind].get_run_meta_by_id(session, run_id)
    return _to_read(meta) if meta is not None else None


async def get_maintenance_summary(session: AsyncSession) -> MaintenanceSummaryRead:
    """Return the latest bounded maintenance result or an empty summary before its first run."""
    row = await session.scalar(select(MaintenanceSummary).where(MaintenanceSummary.id == 1))
    if row is None:
        return MaintenanceSummaryRead()
    return MaintenanceSummaryRead(
        completed_at=row.completed_at,
        agent_traces_redacted=row.agent_traces_redacted,
        temporary_data_deleted=row.temporary_data_deleted,
        next_eligible_at=row.next_eligible_at,
    )
