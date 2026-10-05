"""Detect bounded source-breadth trends from current and retained evidence."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.knowledge.documents import public as documents
from modules.news.models import NewsObservation
from modules.news.schemas import TrendFilter, TrendPage, TrendRead
from modules.news.stories import ALGORITHM_VERSION, _live_story_rows, _resolve_source_scope

MAX_HISTORY_OBSERVATIONS = 5000


async def list_trends(
    session: AsyncSession, owner_id: int, filters: TrendFilter, *, as_of: datetime | None = None,
) -> TrendPage:
    """Compare 24 current hours with a bounded seven-day retained baseline.

    Current counts require each observation to match freshly projected current
    evidence. Baseline versions may be historical, but Documents must confirm
    that their document still exists under the same active source generation and
    provider scope. Counts deduplicate by source/document; rising requires three
    current items, two current sources, and 2x growth when baseline is sufficient.
    """
    now = (as_of or datetime.now(UTC)).astimezone(UTC)
    current_start = now - timedelta(hours=24)
    baseline_start = now - timedelta(days=8)
    source_ids, source_selection_incomplete = await _resolve_source_scope(
        session, tuple(filters.source_ids),
    )
    live = await _live_story_rows(
        session, owner_id, source_ids, now, candidate_limit=50,
    )
    story_ids = [story.id for story, _observations, _evidence in live]
    baseline_by_story: dict[UUID, set[tuple[UUID, UUID]]] = {story_id: set() for story_id in story_ids}
    history_incomplete = False
    if story_ids:
        baseline_rows = list((await session.scalars(
            select(NewsObservation).where(
                NewsObservation.story_id.in_(story_ids),
                NewsObservation.source_id.in_(source_ids),
                NewsObservation.algorithm_version == ALGORITHM_VERSION,
                NewsObservation.created_at <= now,
                NewsObservation.observed_at >= baseline_start,
                NewsObservation.observed_at < current_start,
            ).order_by(
                NewsObservation.observed_at.desc(), NewsObservation.id,
            ).limit(MAX_HISTORY_OBSERVATIONS + 1)
        )).all())
        if len(baseline_rows) > MAX_HISTORY_OBSERVATIONS:
            baseline_rows = baseline_rows[:MAX_HISTORY_OBSERVATIONS]
            history_incomplete = True
        authority_cache: dict[tuple[UUID, UUID, int], bool] = {}
        for observation in baseline_rows:
            key = (observation.document_id, observation.source_id, observation.source_generation)
            allowed = authority_cache.get(key)
            if allowed is None:
                allowed = await documents.news_retained_observation_allowed(
                    session, document_id=observation.document_id,
                    source_id=observation.source_id,
                    expected_source_generation=observation.source_generation,
                )
                authority_cache[key] = allowed
            if allowed:
                baseline_by_story.setdefault(observation.story_id, set()).add(
                    (observation.source_id, observation.document_id),
                )

    # Baseline authorization can await many retained-document checks; reproject
    # current titles and window evidence after that work, immediately before output.
    live = await _live_story_rows(
        session, owner_id, source_ids, now, candidate_limit=50,
    )

    result = []
    for story, observations, evidence in live:
        current_versions = {item.document_id: item.document_version_id for item in evidence}
        current_observations = [
            item for item in observations
            if current_versions.get(item.document_id) == item.document_version_id
            and current_start <= item.observed_at.astimezone(UTC) < now
        ]
        current_ids = {(item.source_id, item.document_id) for item in current_observations}
        current_count = len(current_ids)
        source_count = len({source_id for source_id, _document_id in current_ids})
        baseline_count = len(baseline_by_story.get(story.id, set()))
        baseline_per_day = baseline_count / 7.0
        low_baseline = baseline_per_day < 1.0
        ratio = None if low_baseline else current_count / baseline_per_day
        rising = current_count >= 3 and source_count >= 2 and (
            (low_baseline and current_count > 0) or (ratio is not None and ratio >= 2.0)
        )
        if not rising:
            continue
        current_evidence = [
            item for item in evidence if (item.source_id, item.document_id) in current_ids
        ]
        if not current_evidence:
            continue
        representative = max(
            current_evidence,
            key=lambda item: (item.published_at or item.observed_at, str(item.document_id)),
        )
        incomplete_reasons = {
            reason for item in observations if item.incomplete_reason
            for reason in item.incomplete_reason.split(",")
        }
        incomplete_reasons.update(live.incomplete_reasons)
        if source_selection_incomplete:
            incomplete_reasons.add("source_selection_limit")
        if history_incomplete:
            incomplete_reasons.add("history_scan_limit")
        result.append(TrendRead(
            story_id=story.id, title=representative.title, trend="rising",
            current_count=current_count, baseline_per_day=round(baseline_per_day, 6),
            ratio=round(ratio, 6) if ratio is not None else None, low_baseline=low_baseline,
            source_count=source_count, evidence=current_evidence[:100],
            incomplete_reasons=sorted(incomplete_reasons),
            current_window_start=current_start, current_window_end=now,
            baseline_window_start=baseline_start, baseline_window_end=current_start, as_of=now,
        ))
    result.sort(key=lambda item: (-item.current_count, -item.source_count, str(item.story_id)))
    page_reasons = set(live.incomplete_reasons)
    if source_selection_incomplete:
        page_reasons.add("source_selection_limit")
    if history_incomplete:
        page_reasons.add("history_scan_limit")
    page_reasons.update(reason for item in result for reason in item.incomplete_reasons)
    return TrendPage(
        items=result[:filters.limit], as_of=now,
        truncated=live.truncated or history_incomplete,
        incomplete=bool(page_reasons),
        incomplete_reasons=sorted(page_reasons),
    )
