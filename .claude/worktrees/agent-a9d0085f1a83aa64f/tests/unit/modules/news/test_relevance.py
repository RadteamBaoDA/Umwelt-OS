"""Characterization of ``score_relevance``: pins signal values, availability and completeness."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from core.workspaces.schemas import WorkspaceContext
from modules.news import relevance
from modules.news.relevance import _BoundedProfileList

SCOPE = WorkspaceContext(user_id=1, workspace_id=uuid4(), role="owner", membership_revision=1)
NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


def _story(evidence_count: int = 1, age_hours: float = 36.0) -> SimpleNamespace:
    evidence = [
        SimpleNamespace(
            source_id=uuid4(),
            document_id=uuid4(),
            document_version_id=uuid4(),
            observed_at=NOW - timedelta(hours=age_hours),
        )
    ]
    return SimpleNamespace(
        title="Port strike halts shipping",
        excerpt="Dock workers",
        evidence=evidence,
        evidence_count=evidence_count,
    )


async def _score(
    story: SimpleNamespace,
    topics_: list[SimpleNamespace],
    goals_: list[SimpleNamespace],
    *,
    projection: object = "match",
    refs: list[SimpleNamespace] | None = None,
    membership_error: bool = False,
    topics_complete: bool = True,
) -> object:
    item = story.evidence[0]
    current = None
    if projection == "match":
        current = SimpleNamespace(
            document_version_id=item.document_version_id,
            chunks_truncated=False,
            chunks=[SimpleNamespace(id=uuid4())],
        )
    elif projection == "truncated":
        current = SimpleNamespace(
            document_version_id=item.document_version_id, chunks_truncated=True, chunks=[]
        )
    membership = (
        AsyncMock(side_effect=ValueError("limit"))
        if membership_error
        else AsyncMock(return_value=refs or [])
    )
    cache: dict[object, object] = {}
    with (
        patch(
            "modules.knowledge.documents.public.get_news_document_projection",
            AsyncMock(return_value=current),
        ),
        patch("modules.knowledge.entities.public.list_version_membership_refs", membership),
    ):
        result = await relevance.score_relevance(
            AsyncMock(),
            story,
            scope=SCOPE,
            multi_workspace_enabled=True,
            as_of=NOW,  # type: ignore[arg-type]
            preloaded_topics=_BoundedProfileList(topics_, complete=topics_complete),
            preloaded_goals=_BoundedProfileList(goals_),
            projection_cache=cache,
        )
    assert item.document_id in cache
    return result


def _topic(entity_ids: list[object] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        name="shipping",
        description=None,
        keywords=["port"],
        weight=10.0,
        revision=4,
        entity_ids=entity_ids or [],
    )


@pytest.mark.asyncio
async def test_without_profiles_only_story_signals_are_available() -> None:
    """No topics/goals: topic, entity, goal and project are unavailable; recency/importance/novelty count."""
    result = await _score(_story(), [], [])
    signals = result.signals  # type: ignore[attr-defined]
    assert [name for name, s in signals.items() if s.available] == [
        "recency",
        "importance",
        "novelty",
    ]
    assert signals["recency"].value == pytest.approx(0.5)
    assert signals["importance"].value == pytest.approx(1 / 3)
    assert signals["novelty"].value == 1.0
    assert result.score == round((0.5 + 1 / 3 + 1.0) / 7, 6)  # type: ignore[attr-defined]
    assert result.why_relevant == ["recency", "importance", "novelty"]  # type: ignore[attr-defined]
    assert result.complete is True  # type: ignore[attr-defined]
    assert signals["project"].method == "project_public_owner_unavailable"


@pytest.mark.asyncio
async def test_topic_goal_and_entity_hits_record_revisions_in_order() -> None:
    """Keyword overlap and canonical entity links add topic then goal revisions and evidence IDs."""
    entity = uuid4()
    topic = _topic([entity])
    goal = SimpleNamespace(
        id=uuid4(),
        title="Keep shipping stable",
        description=None,
        desired_outcome=None,
        revision=9,
        entity_ids=[entity],
    )
    result = await _score(_story(), [topic], [goal], refs=[SimpleNamespace(entity_id=entity)])
    signals = result.signals  # type: ignore[attr-defined]
    assert signals["topic"].available and signals["topic"].value > 0
    assert signals["topic"].evidence_ids == [str(topic.id)]
    assert signals["goal"].evidence_ids == [str(goal.id)]
    assert signals["goal"].value == pytest.approx(1 / 3)
    assert signals["entity"].available and signals["entity"].value == 1.0
    assert signals["entity"].evidence_ids == [str(entity)]
    assert result.profile_revisions == [  # type: ignore[attr-defined]
        {"kind": "topic", "id": str(topic.id), "revision": 4},
        {"kind": "goal", "id": str(goal.id), "revision": 9},
    ]


@pytest.mark.asyncio
async def test_membership_overflow_marks_entity_signal_incomplete() -> None:
    """A bounded membership overflow only disables the entity signal and the completeness flag."""
    result = await _score(_story(), [_topic([uuid4()])], [], membership_error=True)
    entity = result.signals["entity"]  # type: ignore[attr-defined]
    assert (entity.available, entity.method, entity.evidence_ids) == (False, "membership_limit", [])
    assert result.complete is False  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_truncated_chunks_and_stale_projection() -> None:
    """Chunk truncation reports ``chunk_limit``; a version mismatch simply contributes no entities."""
    truncated = await _score(_story(), [_topic()], [], projection="truncated")
    assert truncated.signals["entity"].method == "chunk_limit"  # type: ignore[attr-defined]
    missing = await _score(_story(), [_topic()], [], projection=None)
    assert missing.signals["entity"].method == "canonical_entity_overlap"  # type: ignore[attr-defined]
    assert missing.signals["entity"].available is False  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_incomplete_profile_pagination_and_many_evidence_clear_complete() -> None:
    """Exhausted profile pagination or more than 100 evidence rows make the result incomplete."""
    assert (await _score(_story(), [], [], topics_complete=False)).complete is False  # type: ignore[attr-defined]
    assert (await _score(_story(evidence_count=101), [], [])).complete is False  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_unpreloaded_profiles_are_loaded_through_owner_facades() -> None:
    """Without preloaded lists the bounded loaders run with the caller's scope and flag."""
    story = _story()
    loaded_topics, loaded_goals = (
        _BoundedProfileList([], complete=True),
        _BoundedProfileList([], complete=True),
    )
    with (
        patch.object(relevance, "load_active_topics", AsyncMock(return_value=loaded_topics)) as lt,
        patch.object(relevance, "load_active_goals", AsyncMock(return_value=loaded_goals)) as lg,
        patch(
            "modules.knowledge.documents.public.get_news_document_projection",
            AsyncMock(return_value=None),
        ),
    ):
        await relevance.score_relevance(
            AsyncMock(), story, scope=SCOPE, multi_workspace_enabled=True, as_of=NOW
        )  # type: ignore[arg-type]
    assert lt.await_args.kwargs == {"scope": SCOPE, "multi_workspace_enabled": True}
    assert lg.await_args.kwargs == {"scope": SCOPE, "multi_workspace_enabled": True}
