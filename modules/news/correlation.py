"""Deterministic, evidence-only cross-domain temporal co-occurrence projection."""

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from core.workspaces.schemas import Scope

from modules.connectors import public as connectors
from modules.knowledge.observations import public as observations
from modules.knowledge.observations.schemas import ObservationQuery
from modules.news.schemas import (
    CorrelationBucketRead,
    CorrelationCoverageRead,
    CorrelationQuery,
    CorrelationResult,
)
from modules.sources import public as sources
from modules.timeline import public as timeline
from modules.news.stories import _admit

_DOMAINS = ("military", "economic", "disaster", "escalation")


async def build_correlations(
    session: AsyncSession, query: CorrelationQuery, *, scope: Scope, multi_workspace_enabled: bool,
) -> CorrelationResult:
    """Group a fixed number of live event/measurement evidence signals by region and UTC hour.

    This method counts exact owner-public evidence identities only. It performs no
    title matching, model inference, pairwise scoring, significance calculation,
    causation claim, or prediction. Each domain receives its own finite event cap;
    current Alpha Vantage observations are a separate economic evidence input.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    coverage_counts = {domain: 0 for domain in _DOMAINS}
    coverage_truncated = {domain: False for domain in _DOMAINS}
    coverage_omitted = {domain: 0 for domain in _DOMAINS}
    signals: list[dict[str, Any]] = []

    for domain in _DOMAINS:
        if not query.source_ids:
            # An empty saved source selection means no event scan, never an implicit all-source scope.
            continue
        page = await timeline.list_correlation_signals(
            session, domain=domain, from_at=query.from_at, to_at=query.to_at,
            regions=query.regions, source_ids=query.source_ids, limit=query.limit_per_domain,
            scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        coverage_counts[domain] = len(page.items)
        coverage_truncated[domain] = page.truncated
        for item in page.items:
            signals.append({
                "identity": f"event:{item.signal_id}", "domain": domain,
                "region": item.region, "observed_at": item.observed_at,
                "event_id": item.event_id, "observation_id": None,
                "document_ids": item.document_ids,
                "document_version_ids": item.document_version_ids,
                "event_evidence_ids": item.event_evidence_ids,
                "omitted_event_evidence_ids": item.omitted_event_evidence_ids,
                "omitted_document_ids": item.omitted_document_ids,
                "omitted_document_version_ids": item.omitted_document_version_ids,
            })

    alpha_sources = []
    for source_id in query.source_ids:
        source = await sources.get_connector_source(session, source_id, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled)
        snapshot = await connectors.get_current_provider_scope(
            session, source_id,
            source.generation if source is not None and source.status == "active" else -1,
            scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        if snapshot is not None and snapshot.provider_id == "alpha_vantage":
            alpha_sources.append(source_id)
        else:
            coverage_omitted["economic"] += 1

    remaining_economic = max(0, query.limit_per_domain - coverage_counts["economic"])
    if alpha_sources and remaining_economic:
        observation_query = ObservationQuery(
            source_ids=alpha_sources, regions=query.regions, from_at=query.from_at,
            to_at=query.to_at, limit=remaining_economic,
        )
        points, next_cursor, scan_truncated, _ = await observations.list_observations(
            session, observation_query, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        coverage_truncated["economic"] = coverage_truncated["economic"] or bool(next_cursor) or scan_truncated
        seen_observations: set[str] = set()
        for point in points:
            identity = f"observation:{point.id}"
            if identity in seen_observations or point.region not in query.regions:
                continue
            if coverage_counts["economic"] >= query.limit_per_domain:
                coverage_truncated["economic"] = True
                break
            seen_observations.add(identity)
            coverage_counts["economic"] += 1
            signals.append({
                "identity": identity, "domain": "economic", "region": point.region,
                "observed_at": point.observed_at, "event_id": None,
                "observation_id": point.id, "document_ids": [point.document_id],
                "document_version_ids": [point.document_version_id],
                "event_evidence_ids": [],
                "omitted_event_evidence_ids": 0,
                "omitted_document_ids": 0,
                "omitted_document_version_ids": 0,
                "evidence_identity": [f"{point.document_version_id}:{point.id}"],
            })
    elif alpha_sources:
        # Event evidence filled the shared economic admission budget, so selected observation
        # sources were not scanned; report the scope omission without implying a signal existed.
        coverage_truncated["economic"] = True
        coverage_omitted["economic"] += len(alpha_sources)

    # Stable ID deduplication bounds counts even when the same source row arrives via overlapping scopes.
    by_identity: dict[str, dict[str, Any]] = {}
    for signal_row in signals:
        by_identity.setdefault(str(signal_row["identity"]), signal_row)
    groups: dict[tuple[str, datetime], list[dict[str, Any]]] = defaultdict(list)
    for signal_row in by_identity.values():
        instant = signal_row["observed_at"]
        bucket = instant.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        groups[(str(signal_row["region"]), bucket)].append(signal_row)

    buckets: list[CorrelationBucketRead] = []
    omitted_support = False
    for (region, bucket), rows in sorted(groups.items(), key=lambda pair: (pair[0][1], pair[0][0])):
        counts = {domain: 0 for domain in _DOMAINS}
        for row in rows:
            counts[str(row["domain"])] += 1
        evidence_ids = sorted({value for item in rows for value in item["event_evidence_ids"]}, key=str)
        document_ids = sorted({value for item in rows for value in item["document_ids"]}, key=str)
        version_ids = sorted({value for item in rows for value in item["document_version_ids"]}, key=str)
        omitted_event_evidence_ids = sum(int(item["omitted_event_evidence_ids"]) for item in rows) + max(0, len(evidence_ids) - 500)
        omitted_document_ids = sum(int(item["omitted_document_ids"]) for item in rows) + max(0, len(document_ids) - 500)
        omitted_document_version_ids = sum(int(item["omitted_document_version_ids"]) for item in rows) + max(0, len(version_ids) - 500)
        omitted_support = omitted_support or any((
            omitted_event_evidence_ids, omitted_document_ids, omitted_document_version_ids,
        ))
        buckets.append(CorrelationBucketRead(
            region=region, window_start=bucket, window_end=bucket + timedelta(hours=1),
            signal_count=len(rows), domain_counts={key: value for key, value in counts.items() if value},
            domains_present=[domain for domain in _DOMAINS if counts[domain]],
            signal_ids=sorted(str(item["identity"]) for item in rows),
            event_ids=sorted({item["event_id"] for item in rows if item["event_id"] is not None}, key=str),
            observation_ids=sorted({item["observation_id"] for item in rows if item["observation_id"] is not None}, key=str),
            document_ids=document_ids[:500], document_version_ids=version_ids[:500],
            event_evidence_ids=evidence_ids[:500],
            omitted_document_ids=omitted_document_ids,
            omitted_document_version_ids=omitted_document_version_ids,
            omitted_event_evidence_ids=omitted_event_evidence_ids,
        ))

    coverage = {
        domain: CorrelationCoverageRead(
            signal_count=min(coverage_counts[domain], 100),
            available=coverage_counts[domain] > 0,
            truncated=coverage_truncated[domain],
            omitted_source_count=coverage_omitted[domain],
        ) for domain in _DOMAINS
    }
    included = [domain for domain in _DOMAINS if coverage_counts[domain] > 0]
    missing = [domain for domain in _DOMAINS if coverage_counts[domain] == 0]
    uncertainty = ["co_occurrence_is_not_causation"]
    if missing:
        uncertainty.append("insufficient_domain_evidence")
    if any(item.truncated or item.omitted_source_count for item in coverage.values()):
        uncertainty.append("bounded_source_coverage")
    if omitted_support:
        uncertainty.append("bounded_evidence_support")
    return CorrelationResult(
        from_at=query.from_at, to_at=query.to_at, regions=query.regions,
        groups=buckets[:500], coverage=coverage, included_domains=included,
        missing_domains=missing, uncertainty_reasons=uncertainty,
    )
