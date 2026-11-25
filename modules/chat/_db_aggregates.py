"""Grouped observation aggregates for the chat collected-data tool (read-only query builder + runner)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import desc, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from modules.knowledge.observations.public import ObservationReadProjection as Observation

_GROUP_KEYS = ("provider", "metric", "symbol", "region", "unit", "currency")


async def _observation_total_count(session: AsyncSession, obs_filters: list[Any]) -> int:
    """Exact number of observation rows matching ``obs_filters`` (one read-only ``COUNT``).

    Takes the very filter list the grouped query uses (workspace, allowed-source, scope, supersession,
    date window), so it can never count rows the groups could not. It is the sum of every group's count,
    independent of the group ``LIMIT``.
    """
    stmt = select(func.count(Observation.id)).where(*obs_filters)
    return int((await session.execute(stmt)).scalar_one() or 0)


async def _observation_aggregates(
    session: AsyncSession, obs_filters: list[Any], limit: int
) -> list[dict[str, Any]]:
    """Run the grouped observation aggregate query and return JSON-ready aggregate rows.

    Read-only; one SELECT on ``session`` ordered by latest observation, fetching ``limit + 1`` groups (the
    caller trims and flags truncation). Groups are keyed by provider, metric, symbol, region, unit and
    currency. ``null_values`` counts rows whose value is NULL (excluded from avg/min/max/sum). Each row
    carries private ``_document_id``/``_version_id`` of its NEWEST observation, which the caller turns into
    a ``citation`` and removes (see ``_apply_aggregate_citations``).

    The aggregates themselves are unordered (hashable, no per-group arrays); the newest observation is then
    looked up by a ``LATERAL`` probe for each of the at most ``limit + 1`` groups under the same filters. An
    ordered ``array_agg`` here sorted every matching row and spilled to disk (~0.35 s at 350k rows).
    """
    keys = [getattr(Observation, k) for k in _GROUP_KEYS]
    groups = (
        select(
            *keys,
            func.count(Observation.id).label("row_count"),
            (func.count(Observation.id) - func.count(Observation.value)).label("null_count"),
            func.avg(Observation.value).label("avg_value"),
            func.min(Observation.value).label("min_value"),
            func.max(Observation.value).label("max_value"),
            func.sum(Observation.value).label("sum_value"),
            func.min(Observation.observed_at).label("first_observed_at"),
            func.max(Observation.observed_at).label("latest_observed_at"),
        )
        .where(*obs_filters)
        .group_by(*keys)
        .order_by(desc(func.max(Observation.observed_at)))
        .limit(limit + 1)
        .subquery("g")
    )
    newest = (
        select(Observation.document_id, Observation.document_version_id)
        .where(
            *obs_filters,
            *(getattr(Observation, k).is_not_distinct_from(groups.c[k]) for k in _GROUP_KEYS),
            Observation.observed_at == groups.c.latest_observed_at,
        )
        .order_by(Observation.accepted_at.desc())
        .limit(1)
        .lateral("newest")
    )
    agg_rows = (
        await session.execute(
            select(groups, newest.c.document_id, newest.c.document_version_id)
            .select_from(groups.outerjoin(newest, true()))
            .order_by(groups.c.latest_observed_at.desc())
        )
    ).all()
    return [
        {
            "provider": r.provider,
            "metric": r.metric,
            "symbol": r.symbol,
            "region": r.region,
            "unit": r.unit,
            "currency": r.currency,
            "count": int(r.row_count or 0),
            "null_values": int(r.null_count or 0),
            "avg": float(r.avg_value) if r.avg_value is not None else None,
            "min": float(r.min_value) if r.min_value is not None else None,
            "max": float(r.max_value) if r.max_value is not None else None,
            "sum": float(r.sum_value) if r.sum_value is not None else None,
            "first_observed_at": r.first_observed_at.isoformat() if r.first_observed_at else None,
            "latest_observed_at": r.latest_observed_at.isoformat()
            if r.latest_observed_at
            else None,
            "_document_id": r.document_id,
            "_version_id": r.document_version_id,
        }
        for r in agg_rows
    ]
