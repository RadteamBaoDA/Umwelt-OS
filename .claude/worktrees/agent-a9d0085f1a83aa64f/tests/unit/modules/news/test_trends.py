"""Characterization of ``list_trends``: rising rules, baseline authority, and incompleteness flags."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from core.workspaces.schemas import WorkspaceContext
from modules.news import stories, trends
from modules.news.schemas import StoryEvidence, TrendFilter

WS = uuid4()
SCOPE = WorkspaceContext(user_id=1, workspace_id=WS, role="owner", membership_revision=1)
NOW = datetime(2026, 3, 8, 12, tzinfo=UTC)
KW: dict[str, Any] = {"scope": SCOPE, "multi_workspace_enabled": True}


def _live(
    count: int, sources: int, *, incomplete: str | None = None
) -> tuple[Any, SimpleNamespace]:
    story = SimpleNamespace(id=uuid4())
    source_ids = [uuid4() for _ in range(sources)]
    observations, evidence = [], []
    for index in range(count):
        doc, version, source = uuid4(), uuid4(), source_ids[index % sources]
        observed = NOW - timedelta(hours=1 + index)
        observations.append(
            SimpleNamespace(
                story_id=story.id,
                document_id=doc,
                document_version_id=version,
                source_id=source,
                observed_at=observed,
                incomplete_reason=incomplete,
                source_generation=1,
            )
        )
        evidence.append(
            StoryEvidence(
                document_id=doc,
                document_version_id=version,
                chunk_id=uuid4(),
                source_id=source,
                source_name="s",
                source_type="rss",
                provider=None,
                url=None,
                title=f"Title {index}",
                excerpt="x",
                observed_at=observed,
                published_at=None,
            )
        )
    return stories.LiveStoryRows(
        [(story, observations, evidence)], False, {"scope_unavailable"}
    ), story


async def _run(
    live: Any, baseline: list[SimpleNamespace], *, allowed: bool = True, limit: int = 25
) -> Any:
    session = AsyncMock()
    session.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: baseline))
    retained = AsyncMock(return_value=allowed)
    with (
        patch.object(trends, "_admit", AsyncMock()),
        patch.object(trends, "_resolve_source_scope", AsyncMock(return_value=((uuid4(),), True))),
        patch.object(trends, "_live_story_rows", AsyncMock(return_value=live)),
        patch.object(trends.documents, "news_retained_observation_allowed", retained),
    ):
        page = await trends.list_trends(session, TrendFilter(limit=limit), as_of=NOW, **KW)
    return page, retained


def _baseline(story_id: UUID, count: int) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            story_id=story_id, document_id=uuid4(), source_id=uuid4(), source_generation=1
        )
        for _ in range(count)
    ]


@pytest.mark.asyncio
async def test_low_baseline_rising_story_reports_reasons() -> None:
    """Three items across two sources with a thin baseline is rising and carries scan reasons."""
    live, story = _live(3, 2, incomplete="chunk_limit")
    page, retained = await _run(live, _baseline(story.id, 2))
    [item] = page.items
    assert (item.current_count, item.source_count, item.low_baseline, item.ratio) == (
        3,
        2,
        True,
        None,
    )
    assert item.baseline_per_day == round(2 / 7.0, 6)
    assert item.incomplete_reasons == ["chunk_limit", "scope_unavailable", "source_selection_limit"]
    assert item.title == "Title 0" and item.current_window_start == NOW - timedelta(hours=24)
    assert page.incomplete is True and page.truncated is False and page.history_days == 8
    assert (
        retained.await_count == 2
    )  # one authority check per distinct (document, source, generation)


@pytest.mark.asyncio
async def test_sufficient_baseline_requires_double_growth() -> None:
    """With at least one baseline item per day the ratio must reach 2x to count as rising."""
    live, story = _live(3, 2)
    flat, _ = await _run(live, _baseline(story.id, 14))  # 2/day baseline: 3/2 = 1.5x -> not rising
    assert flat.items == []
    live, story = _live(3, 2)
    grown, _ = await _run(live, _baseline(story.id, 7))  # 1/day baseline: 3x
    assert grown.items[0].ratio == 3.0 and grown.items[0].low_baseline is False


@pytest.mark.asyncio
async def test_unauthorized_baseline_and_single_source_do_not_trend() -> None:
    """Unauthorized retained baseline rows are ignored; one source never qualifies."""
    live, story = _live(3, 1)
    page, _ = await _run(live, _baseline(story.id, 14), allowed=False)
    assert page.items == []
    live, story = _live(2, 2)
    page, _ = await _run(live, [])
    assert (
        page.items == []
        and page.incomplete is True
        and page.incomplete_reasons
        == [
            "scope_unavailable",
            "source_selection_limit",
        ]
    )


@pytest.mark.asyncio
async def test_history_scan_limit_marks_page_truncated() -> None:
    """More than the retained-history bound sets ``history_scan_limit`` and truncation."""
    live, story = _live(3, 2)
    rows = _baseline(story.id, trends.MAX_HISTORY_OBSERVATIONS + 1)
    page, retained = await _run(live, rows, allowed=False)
    assert page.truncated is True and "history_scan_limit" in page.incomplete_reasons
    assert "history_scan_limit" in page.items[0].incomplete_reasons
    assert retained.await_count == trends.MAX_HISTORY_OBSERVATIONS
