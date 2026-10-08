"""Compute a transparent seven-signal story score from owner-visible evidence."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.schemas import Scope
from modules.goals import public as goals
from modules.goals.schemas import GoalFilter
from modules.news import topics
from modules.news.schemas import RecordedSignal, RelevanceRead, StoryRead
from modules.news.stories import _tokens
from modules.news.topics import TopicFilter

SIGNAL_NAMES = ("topic", "entity", "goal", "project", "recency", "importance", "novelty")


def _empty(method: str = "missing_owner_contract") -> RecordedSignal:
    """Represent an unavailable signal without conflating absence with zero evidence."""
    return RecordedSignal(value=0.0, available=False, method=method)


async def score_relevance(
    session: AsyncSession, story: StoryRead, *, scope: Scope, multi_workspace_enabled: bool,
    as_of: datetime | None = None,
) -> RelevanceRead:
    """Score a live story with equal bounded weights and explicit provenance for seven signals.

    Topic and goal inputs are read through their owner facades, entity overlap
    uses canonical IDs retained on current observations, Projects remain
    unavailable because this checkout has no Project public owner, and the
    cross-source importance signal is an explicitly named evidence proxy.
    No model confidence or generated score enters this calculation.
    """
    now = (as_of or datetime.now(UTC)).astimezone(UTC)
    text_value = " ".join(f"{story.title} {story.excerpt}".split())
    story_tokens = _tokens(text_value)
    sources = {str(item.source_id) for item in story.evidence}
    entity_ids: set[str] = set()
    membership_incomplete: str | None = None
    # Story evidence records exact versions; memberships are resolved from each version below.
    from modules.knowledge.documents import public as documents
    from modules.knowledge.entities import public as entities

    for item in story.evidence[:100]:
        current = await documents.get_news_document_projection(
            session, item.document_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        if current is None or current.document_version_id != item.document_version_id:
            continue
        if current.chunks_truncated:
            entity_ids.clear()
            membership_incomplete = "chunk_limit"
            break
        try:
            refs = await entities.list_version_membership_refs(
                session, item.document_version_id, [chunk.id for chunk in current.chunks],
                scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
        except ValueError:
            # A bounded membership overflow makes only the optional entity signal
            # unavailable; permission, deletion and database failures still surface.
            entity_ids.clear()
            membership_incomplete = "membership_limit"
            break
        entity_ids.update(str(ref.entity_id) for ref in refs)
    topic_records = []
    cursor = None
    topic_complete = True
    for _ in range(10):
        page = await topics.list_topics(session, TopicFilter(is_active=True, limit=100, cursor=cursor),
            scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        topic_records.extend(page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    else:
        topic_complete = False
    topic_hits: list[tuple[float, str, set[str]]] = []
    entity_hits: set[str] = set()
    topic_evidence_ids: list[str] = []
    profile_tokens: set[str] = set()
    profile_revisions: list[dict[str, str | int]] = []
    for profile in topic_records:
        terms = _tokens(" ".join([profile.name, profile.description or "", *profile.keywords]))
        profile_tokens.update(terms)
        overlap = story_tokens.intersection(terms)
        linked = entity_ids.intersection(str(value) for value in profile.entity_ids)
        if linked:
            entity_hits.update(linked)
        if linked or overlap:
            profile_revisions.append({"kind": "topic", "id": str(profile.id), "revision": profile.revision})
        if overlap:
            value = min(1.0, len(overlap) / max(1, len(terms)) * min(profile.weight, 10.0) / 10.0)
            topic_hits.append((value, str(profile.id), overlap))
            topic_evidence_ids.append(str(profile.id))
    topic_value = max((item[0] for item in topic_hits), default=0.0)
    goal_records = []
    cursor = None
    goal_complete = True
    for _ in range(10):
        goal_page = await goals.list_goals(session, GoalFilter(status="active", limit=100, cursor=cursor),
            scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        goal_records.extend(goal_page.items)
        cursor = goal_page.next_cursor
        if cursor is None:
            break
    else:
        goal_complete = False
    goal_value = 0.0
    goal_evidence_ids: list[str] = []
    for goal in goal_records:
        terms = _tokens(" ".join([goal.title, goal.description or "", goal.desired_outcome or ""]))
        overlap = story_tokens.intersection(terms)
        linked = entity_ids.intersection(str(value) for value in goal.entity_ids)
        if overlap or linked:
            goal_value = max(goal_value, min(1.0, len(overlap) / max(1, len(terms)) if terms else 0.0))
            goal_evidence_ids.append(str(goal.id))
            profile_revisions.append({"kind": "goal", "id": str(goal.id), "revision": goal.revision})
            entity_hits.update(linked)
    ages = [max(0.0, (now - item.observed_at.astimezone(UTC)).total_seconds() / 3600) for item in story.evidence]
    recency = max((max(0.0, 1.0 - age / 72.0) for age in ages), default=0.0)
    signals = {
        "topic": RecordedSignal(value=topic_value, available=bool(topic_records), method="weighted_keyword_overlap", evidence_ids=topic_evidence_ids[:100]),
        "entity": RecordedSignal(
            value=1.0 if entity_hits else 0.0,
            available=membership_incomplete is None and bool(entity_ids) and bool(topic_records or goal_records),
            method=membership_incomplete or "canonical_entity_overlap",
            evidence_ids=[] if membership_incomplete else sorted(entity_hits)[:100],
        ),
        "goal": RecordedSignal(value=goal_value, available=bool(goal_records), method="active_goal_lexical_overlap", evidence_ids=goal_evidence_ids[:100]),
        "project": _empty("project_public_owner_unavailable"),
        "recency": RecordedSignal(value=recency, available=bool(ages), method="max(0,1-age_hours/72)", evidence_ids=[str(item.document_version_id) for item in story.evidence[:100]]),
        "importance": RecordedSignal(value=min(1.0, len(sources) / 3.0), available=bool(sources), method="distinct_active_source_proxy", evidence_ids=sorted(sources)[:100]),
        "novelty": RecordedSignal(value=1.0 if story.evidence_count <= 1 else 0.0, available=bool(story.evidence), method="exact_identity_first_support", evidence_ids=[str(item.document_id) for item in story.evidence[:100]]),
    }
    weights = {name: 1.0 / len(SIGNAL_NAMES) for name in SIGNAL_NAMES}
    score = round(sum(weights[name] * signals[name].value for name in SIGNAL_NAMES), 6)
    why = [name for name in SIGNAL_NAMES if signals[name].available and signals[name].value > 0]
    return RelevanceRead(
        score=score, why_relevant=why, signals=signals, weights=weights,
        profile_revisions=profile_revisions[:2000], as_of=now,
        complete=topic_complete and goal_complete and story.evidence_count <= 100 and membership_incomplete is None,
    )
