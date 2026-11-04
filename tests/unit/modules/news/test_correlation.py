"""Characterization of ``build_correlations``: pins coverage, bucketing and uncertainty output."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from core.workspaces.schemas import WorkspaceContext
from modules.news import correlation
from modules.news.schemas import CorrelationQuery
from modules.timeline.schemas import CorrelationSignalPage, CorrelationSignalRead

WS = uuid4()
SCOPE = WorkspaceContext(user_id=1, workspace_id=WS, role="owner", membership_revision=1)
T0 = datetime(2026, 1, 1, 10, 15, tzinfo=UTC)


def _signal(domain: str, region: str, at: datetime) -> CorrelationSignalRead:
    return CorrelationSignalRead(
        signal_id=uuid4(), event_id=uuid4(), event_type=domain, region=region, observed_at=at, source_id=None,  # type: ignore[arg-type]
        event_evidence_ids=[uuid4()], chunk_ids=[], document_ids=[uuid4()], document_version_ids=[uuid4()],
        omitted_event_evidence_ids=1,
    )


def _query(source_ids: list[UUID], limit: int = 100) -> CorrelationQuery:
    return CorrelationQuery(
        source_ids=source_ids, regions=["VN"], from_at=T0.replace(hour=0), to_at=T0.replace(day=3),
        limit_per_domain=limit,
    )


async def _run(query: CorrelationQuery, pages: dict[str, CorrelationSignalPage], alpha: dict[UUID, bool],
               points: list[SimpleNamespace], *, next_cursor: str | None = None) -> tuple[object, AsyncMock]:
    async def list_signals(_s: object, *, domain: str, **_k: object) -> CorrelationSignalPage:
        return pages.get(domain, CorrelationSignalPage(items=[], truncated=False))

    async def get_source(_s: object, source_id: UUID, **_k: object) -> SimpleNamespace:
        return SimpleNamespace(generation=3, status="active")

    async def get_scope(_s: object, source_id: UUID, _gen: int, **_k: object) -> SimpleNamespace | None:
        return SimpleNamespace(provider_id="alpha_vantage") if alpha[source_id] else None

    observations = AsyncMock(return_value=(points, next_cursor, False, None))
    with patch.object(correlation, "_admit", AsyncMock()), \
         patch.object(correlation.timeline, "list_correlation_signals", list_signals), \
         patch.object(correlation.sources, "get_connector_source", get_source), \
         patch.object(correlation.connectors, "get_current_provider_scope", get_scope), \
         patch.object(correlation.observations, "list_observations", observations):
        result = await correlation.build_correlations(
            AsyncMock(), query, scope=SCOPE, multi_workspace_enabled=True,
        )
    return result, observations


@pytest.mark.asyncio
async def test_empty_source_selection_scans_nothing() -> None:
    """No saved sources means no event scan and an all-missing coverage report."""
    result, observations = await _run(_query([]), {}, {}, [])
    assert result.groups == []  # type: ignore[attr-defined]
    assert result.included_domains == []  # type: ignore[attr-defined]
    assert result.missing_domains == ["military", "economic", "disaster", "escalation"]  # type: ignore[attr-defined]
    assert result.uncertainty_reasons == ["co_occurrence_is_not_causation", "insufficient_domain_evidence"]  # type: ignore[attr-defined]
    observations.assert_not_awaited()


@pytest.mark.asyncio
async def test_events_and_observations_share_hourly_buckets() -> None:
    """Events and Alpha Vantage points land in one region/hour bucket; non-Alpha sources are omitted."""
    alpha_id, other_id = uuid4(), uuid4()
    pages = {
        "military": CorrelationSignalPage(items=[_signal("military", "VN", T0)], truncated=True),
        "economic": CorrelationSignalPage(items=[_signal("economic", "VN", T0.replace(hour=11))], truncated=False),
    }
    doc, ver, oid = uuid4(), uuid4(), uuid4()
    points = [
        SimpleNamespace(id=oid, region="VN", observed_at=T0, document_id=doc, document_version_id=ver),
        SimpleNamespace(id=oid, region="VN", observed_at=T0, document_id=doc, document_version_id=ver),
        SimpleNamespace(id=uuid4(), region="XX", observed_at=T0, document_id=doc, document_version_id=ver),
    ]
    result, observations = await _run(
        _query([alpha_id, other_id], limit=2), pages, {alpha_id: True, other_id: False}, points,
    )
    assert observations.await_args.args[1].limit == 1  # type: ignore[union-attr]
    first, second = result.groups  # type: ignore[attr-defined]
    assert first.window_start == T0.replace(minute=0, second=0, microsecond=0)
    assert first.domain_counts == {"military": 1, "economic": 1}
    assert first.observation_ids == [oid]
    assert second.domain_counts == {"economic": 1}
    assert second.window_start == T0.replace(hour=11, minute=0)
    coverage = result.coverage  # type: ignore[attr-defined]
    assert coverage["military"].truncated is True
    assert coverage["economic"].signal_count == 2 and coverage["economic"].omitted_source_count == 1
    assert result.included_domains == ["military", "economic"]  # type: ignore[attr-defined]
    assert result.uncertainty_reasons == [  # type: ignore[attr-defined]
        "co_occurrence_is_not_causation", "insufficient_domain_evidence",
        "bounded_source_coverage", "bounded_evidence_support",
    ]


@pytest.mark.asyncio
async def test_full_economic_budget_skips_observation_scan() -> None:
    """Event evidence filling the shared economic budget reports scope omission without scanning."""
    alpha_id = uuid4()
    pages = {"economic": CorrelationSignalPage(items=[_signal("economic", "VN", T0)], truncated=False)}
    result, observations = await _run(_query([alpha_id], limit=1), pages, {alpha_id: True}, [])
    observations.assert_not_awaited()
    assert result.coverage["economic"].truncated is True  # type: ignore[attr-defined]
    assert result.coverage["economic"].omitted_source_count == 1  # type: ignore[attr-defined]
