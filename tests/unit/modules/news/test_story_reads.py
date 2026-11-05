"""Characterization of the owner story read paths: live rows, list filtering/paging and detail pages."""

import base64
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from core.workspaces.schemas import AccessFence, WorkspaceContext
from modules.news import stories
from modules.news.schemas import StoryFilter

WS = uuid4()
SCOPE = WorkspaceContext(user_id=1, workspace_id=WS, role="owner", membership_revision=2)
FENCE = AccessFence(workspace_id=WS, user_id=1, membership_revision=2, configuration_revision=3)
KW: dict[str, Any] = {"scope": SCOPE, "multi_workspace_enabled": True}
NOW = datetime(2026, 3, 1, 12, tzinfo=UTC)


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)


class _Session:
    """Return scripted ``execute`` results in order and remember every statement."""

    def __init__(self, *results: list[Any]) -> None:
        self.results = list(results)
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Rows:
        self.statements.append(statement)
        return _Rows(self.results.pop(0))


def _projection(
    doc: UUID,
    version: UUID,
    chunk: UUID,
    source: UUID,
    *,
    title: str = "Port reopens",
    observed: datetime = NOW,
    generation: int = 1,
) -> SimpleNamespace:
    return SimpleNamespace(
        document_id=doc,
        document_version_id=version,
        current_source_generation=generation,
        source_id=source,
        source_name="Wire",
        source_type="rss",
        provider="rss",
        canonical_url="https://example.test/a",
        title=title,
        observed_at=observed,
        published_at=None,
        provider_metadata=None,
        chunks_truncated=False,
        chunks=[SimpleNamespace(id=chunk, content="Port reopens after the strike ended.")],
    )


def _observation(
    story_id: UUID, source: UUID, *, observed: datetime = NOW, incomplete: str | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        story_id=story_id,
        document_id=uuid4(),
        document_version_id=uuid4(),
        chunk_id=uuid4(),
        source_id=source,
        source_generation=1,
        observed_at=observed,
        incomplete_reason=incomplete,
    )


def _patch_sources(source: UUID, *, status: str = "active") -> Any:
    return patch.object(
        stories.sources,
        "get_gadget_sources",
        AsyncMock(return_value=[SimpleNamespace(id=source, status=status)]),
    )


@pytest.mark.asyncio
async def test_live_rows_report_scan_limits_and_group_supports() -> None:
    """Candidate and support overflow set continuation keys; stale versions drop with a scope reason."""
    source, story = uuid4(), SimpleNamespace(id=uuid4())
    first, second, stale = (_observation(story.id, source) for _ in range(3))
    proj = _projection(first.document_id, first.document_version_id, first.chunk_id, source)
    stale_proj = _projection(stale.document_id, uuid4(), stale.chunk_id, source)

    async def projection(_s: object, document_id: UUID, **_k: object) -> SimpleNamespace | None:
        return {first.document_id: proj, stale.document_id: stale_proj}.get(document_id)

    candidates = [
        SimpleNamespace(story_id=story.id, latest_observed_at=NOW),
        SimpleNamespace(story_id=uuid4(), latest_observed_at=NOW - timedelta(hours=1)),
    ]
    session = _Session(candidates, [(story, stale, 1), (story, first, 2), (story, second, 3)])
    with (
        _patch_sources(source),
        patch.object(
            stories.documents,
            "news_current_scope_status",
            AsyncMock(return_value=SimpleNamespace(incomplete_reasons={"source_scope"})),
        ),
        patch.object(stories.documents, "get_news_document_projection", projection),
        patch.object(
            stories.documents, "news_projection_scope_unavailable", AsyncMock(return_value=True)
        ),
    ):
        cache: dict[UUID, Any] = {}
        live = await stories._live_story_rows(
            session,
            (source,),
            NOW,
            projection_cache=cache,
            candidate_limit=1,
            support_limit=2,
            **KW,  # type: ignore[arg-type]
        )
    assert live.truncated is True
    assert live.incomplete_reasons == {"source_scope", "candidate_scan_limit", "support_scan_limit"}
    assert live.next_candidate == (NOW, story.id)
    assert list(live.candidate_keys) == [story.id]
    # rank 3 exceeds support_limit=2, so the continuation key is the last in-window support (``first``).
    assert live.next_evidence == (first.source_id, first.document_version_id, first.chunk_id)
    [(grouped_story, observations, evidence)] = live
    assert grouped_story is story and observations == [first] and len(evidence) == 1
    assert evidence[0].title == "Port reopens" and evidence[0].source_name == "Wire"
    assert first.document_id in cache
    assert len(session.statements) == 2


@pytest.mark.asyncio
async def test_live_rows_without_active_sources_or_candidates_issue_no_reads() -> None:
    """Empty or inactive selections return immediately; no candidates skips the support query."""
    session = _Session()
    empty = await stories._live_story_rows(session, (), NOW, **KW)  # type: ignore[arg-type]
    assert list(empty) == [] and session.statements == []
    source = uuid4()
    with _patch_sources(source, status="disabled"):
        inactive = await stories._live_story_rows(session, (source,), NOW, **KW)  # type: ignore[arg-type]
    assert list(inactive) == [] and session.statements == []
    with (
        _patch_sources(source),
        patch.object(
            stories.documents,
            "news_current_scope_status",
            AsyncMock(return_value=SimpleNamespace(incomplete_reasons={"x"})),
        ),
    ):
        none = await stories._live_story_rows(_Session([]), (source,), NOW, **KW)  # type: ignore[arg-type]
    assert list(none) == [] and none.incomplete_reasons == {"x"}


def _live_for_list(
    source: UUID, *, title: str = "Port reopens", more: bool = False
) -> tuple[Any, SimpleNamespace, Any]:
    story = SimpleNamespace(id=uuid4())
    obs = _observation(story.id, source, incomplete="chunk_limit")
    evidence = stories.StoryEvidence(
        document_id=obs.document_id,
        document_version_id=obs.document_version_id,
        chunk_id=obs.chunk_id,
        source_id=source,
        source_name="Wire",
        source_type="rss",
        provider="rss",
        url=None,
        title=title,
        excerpt="Port reopens after the strike",
        observed_at=NOW,
        published_at=None,
    )
    live = stories.LiveStoryRows(
        [(story, [obs], [evidence])],
        more,
        {"scope_unavailable"},
        (NOW, story.id) if more else None,
        {story.id: (NOW, story.id)},
    )
    return live, story, obs


def _relevance(entity_method: str = "canonical_entity_overlap", *, available: bool = True) -> Any:
    signals = {
        name: SimpleNamespace(
            available=available,
            model_dump=lambda n=name: {"value": 0.5, "name": n},
            method="m",
        )
        for name in ("topic", "goal")
    }
    signals["entity"] = SimpleNamespace(
        available=available, model_dump=lambda: {"value": 0.0}, method=entity_method
    )
    return SimpleNamespace(
        score=0.42,
        why_relevant=["topic"],
        signals=signals,
        weights={"topic": 1.0},
        as_of=NOW,
        profile_revisions=[{"kind": "topic", "id": "t", "revision": 1}],
    )


async def _list(
    live: Any,
    source: UUID,
    filters: StoryFilter,
    *,
    current: bool = True,
    relevance: Any = None,
    topic: Any = None,
) -> Any:
    story_obs = live[0][1][0]
    proj = _projection(
        story_obs.document_id, story_obs.document_version_id, story_obs.chunk_id, source
    )
    if not current:
        proj.document_version_id = uuid4()
    with (
        patch.object(stories, "_admit", AsyncMock(return_value=FENCE)),
        patch.object(stories, "_resolve_source_scope", AsyncMock(return_value=((source,), True))),
        patch.object(stories, "_live_story_rows", AsyncMock(return_value=live)),
        patch.object(stories.topics, "get_topic", AsyncMock(return_value=topic)),
        patch.object(
            stories.documents, "get_news_document_projection", AsyncMock(return_value=proj)
        ),
        patch("modules.news.relevance.load_active_topics", AsyncMock(return_value=[])),
        patch("modules.news.relevance.load_active_goals", AsyncMock(return_value=[])),
        patch(
            "modules.news.relevance.score_relevance",
            AsyncMock(return_value=relevance or _relevance()),
        ),
    ):
        return await stories.list_stories(AsyncMock(), filters, **KW)


@pytest.mark.asyncio
async def test_list_stories_enriches_pages_and_aggregates_reasons() -> None:
    """A kept story carries relevance, per-story reasons, a bound continuation cursor and capability."""
    source = uuid4()
    live, story, _obs = _live_for_list(source, more=True)
    page = await _list(live, source, StoryFilter(limit=1), relevance=_relevance("membership_limit"))
    [item] = page.items
    assert item.id == story.id and item.relevance == 0.42 and item.relevance_state == "available"
    assert item.relevance_signals["topic"] == {"value": 0.5, "name": "topic"}
    assert item.incomplete_reasons == ["chunk_limit", "entity_membership_limit"]
    assert page.incomplete_reasons == [
        "chunk_limit",
        "entity_membership_limit",
        "scope_unavailable",
        "source_selection_limit",
    ]
    assert page.capability == "partial" and page.truncated is True
    token = base64.urlsafe_b64decode(page.next_cursor + "=" * (-len(page.next_cursor) % 4))
    cursor = stories.StoryCursor.model_validate_json(token)
    assert cursor.after_story_id == story.id and cursor.resolved_source_ids == [source]
    assert (
        cursor.source_selection_incomplete is True
        and cursor.filter_hash == stories._filter_hash(StoryFilter(limit=1))
    )
    assert cursor.workspace_id == WS and cursor.membership_revision == 2


@pytest.mark.asyncio
async def test_list_stories_drops_changed_evidence_and_filters() -> None:
    """A story whose evidence changed mid-read is dropped with a reason; a text filter excludes it."""
    source = uuid4()
    live, _story, _obs = _live_for_list(source)
    changed = await _list(live, source, StoryFilter(limit=5), current=False)
    assert changed.items == [] and "evidence_changed_during_read" in changed.incomplete_reasons
    live, _story, _obs = _live_for_list(source)
    filtered = await _list(live, source, StoryFilter(limit=5, q="volcano"))
    assert filtered.items == [] and filtered.next_cursor is None
    inactive = SimpleNamespace(is_active=False)
    live, _story, _obs = _live_for_list(source)
    empty = await _list(live, source, StoryFilter(limit=5, topic_id=uuid4()), topic=inactive)
    assert empty.items == [] and empty.incomplete_reasons == []


@pytest.mark.asyncio
async def test_list_stories_topic_profile_requires_lexical_or_entity_hit() -> None:
    """An active topic profile keeps lexically matching stories and drops the rest."""
    source = uuid4()
    entities_patch = patch.object(
        stories, "_current_entity_ids", AsyncMock(return_value=(set(), None))
    )
    hit = SimpleNamespace(is_active=True, name="port", description=None, keywords=[], entity_ids=[])
    miss = SimpleNamespace(
        is_active=True, name="volcano", description=None, keywords=[], entity_ids=[]
    )
    with entities_patch:
        live, _s, _o = _live_for_list(source)
        kept = await _list(live, source, StoryFilter(limit=5, topic_id=uuid4()), topic=hit)
        live, _s, _o = _live_for_list(source)
        dropped = await _list(live, source, StoryFilter(limit=5, topic_id=uuid4()), topic=miss)
    assert len(kept.items) == 1 and dropped.items == []


@pytest.mark.asyncio
async def test_get_story_returns_current_page_or_continuation() -> None:
    """Detail builds a story from fenced supports; a stale fence returns a title-free partial page."""
    source = uuid4()
    live, story, obs = _live_for_list(source)
    live.next_evidence = (uuid4(), uuid4(), uuid4())
    proj = _projection(obs.document_id, obs.document_version_id, obs.chunk_id, source)

    async def run(projection: SimpleNamespace | None) -> Any:
        with (
            patch.object(stories, "_admit", AsyncMock(return_value=FENCE)),
            patch.object(
                stories, "_resolve_source_scope", AsyncMock(return_value=((source,), False))
            ),
            patch.object(stories, "_live_story_rows", AsyncMock(return_value=live)) as rows,
            patch.object(
                stories.documents,
                "get_news_document_projection",
                AsyncMock(return_value=projection),
            ),
        ):
            detail = await stories.get_story(AsyncMock(), story.id, (), evidence_limit=10, **KW)
        assert rows.await_count == 2
        return detail

    current = await run(proj)
    assert current.story.id == story.id and current.story.evidence_count == 1
    assert current.incomplete_reasons == ["chunk_limit", "scope_unavailable"]
    assert current.evidence_cursor is not None
    stale = await run(None)
    assert stale.story is None and stale.evidence_cursor is not None
    assert stale.incomplete_reasons == [
        "chunk_limit",
        "evidence_changed_during_read",
        "scope_unavailable",
        "stale_support_omitted",
    ]
    live.next_evidence = None
    assert await run(None) is None


@pytest.mark.asyncio
async def test_get_story_without_any_match_returns_none_or_scan_continuation() -> None:
    """No base or page match: ``None`` unless a bounded scan continuation remains."""
    empty = stories.LiveStoryRows(
        [], False, {"support_scan_limit"}, None, {}, (uuid4(), uuid4(), uuid4())
    )
    with (
        patch.object(stories, "_admit", AsyncMock(return_value=FENCE)),
        patch.object(stories, "_resolve_source_scope", AsyncMock(return_value=((uuid4(),), False))),
        patch.object(stories, "_live_story_rows", AsyncMock(return_value=empty)),
    ):
        detail = await stories.get_story(AsyncMock(), uuid4(), (), **KW)
        assert detail.story is None and detail.incomplete_reasons == [
            "stale_support_omitted",
            "support_scan_limit",
        ]
        empty.next_evidence = None
        assert await stories.get_story(AsyncMock(), uuid4(), (), **KW) is None
