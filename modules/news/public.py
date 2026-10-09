"""Stable topic DTO/query/mutation facade.

The create, update, and delete exports commit the entire supplied SQLAlchemy
session, including unrelated pending changes. Callers own rollback/disposal when
an operation fails before commit and must rollback after failed commit before
reusing that session. Future seed or cross-module units of work must account for
this boundary; these exports are not flush-only composition helpers.

The explicit fictional seed export is flush-only and leaves transaction ownership
to the document-seed coordinator.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.schemas import Scope
from modules.news.correlation import build_correlations
from modules.news.relevance import score_relevance
from modules.news.schemas import (
    BriefStorySupport,
    CiiUnavailableRead,
    CorrelationBucketRead,
    CorrelationCoverageRead,
    CorrelationQuery,
    CorrelationResult,
    RelevanceRead,
    StoryDetail,
    StoryFilter,
    StoryPage,
    StoryRead,
    TrendFilter,
    TrendPage,
    TrendRead,
)
from modules.news.seed import ensure_demo_topics
from modules.news.stories import (
    cluster_observation,
    get_story,
    list_stories,
    read_story_translation_input,
)
from modules.news.topics import (
    TopicConflict,
    TopicCreate,
    TopicExportFence,
    TopicExportPage,
    TopicExportValidation,
    TopicFilter,
    TopicMissing,
    TopicPage,
    TopicRead,
    TopicUpdate,
    create_topic,
    delete_topic,
    export_page,
    get_topic,
    list_topics,
    live_topic_ids,
    resolve_topic_terms,
    update_topic,
    validate_export_fences,
)
from modules.news.trends import list_trends
from modules.news.worker import process_news_document_ready, recover_news_work

__all__ = [
    "CiiUnavailableRead",
    "CorrelationBucketRead",
    "CorrelationCoverageRead",
    "CorrelationQuery",
    "CorrelationResult",
    "RelevanceRead",
    "StoryDetail",
    "StoryFilter",
    "StoryPage",
    "StoryRead",
    "TopicConflict",
    "TopicCreate",
    "TopicExportFence",
    "TopicExportPage",
    "TopicExportValidation",
    "TopicFilter",
    "TopicMissing",
    "TopicPage",
    "TopicRead",
    "TopicUpdate",
    "TrendFilter",
    "TrendPage",
    "TrendRead",
    "brief_story_support",
    "build_correlations",
    "cluster_observation",
    "create_topic",
    "delete_topic",
    "ensure_demo_topics",
    "export_page",
    "get_story",
    "get_topic",
    "list_stories",
    "list_topics",
    "list_trends",
    "live_topic_ids",
    "process_news_document_ready",
    "read_story_translation_input",
    "recover_news_work",
    "resolve_topic_terms",
    "score_relevance",
    "update_topic",
    "validate_export_fences",
]


async def brief_story_support(
    session: AsyncSession, story_id: UUID, *,
    expected_title: str | None, expected_source_ids: list[str],
    scope: Scope, multi_workspace_enabled: bool,
) -> BriefStorySupport:
    """Resolve the exact complete live support set for one Dashboard story fact.

    The bounded detail reader supplies at most 100 canonical document/version/chunk
    identities. A continuation, omitted support, stale projection, or mismatch with
    the widget snapshot returns ``complete=False`` without truncating lineage.
    """
    if (not 1 <= len(expected_source_ids) <= 32
            or len(expected_source_ids) != len(set(expected_source_ids))):
        return BriefStorySupport(
            story_id=story_id, title=expected_title or "", source_ids=[], evidence=[], complete=False,
        )
    try:
        source_ids = tuple(UUID(value) for value in expected_source_ids)
    except (TypeError, ValueError):
        return BriefStorySupport(
            story_id=story_id, title=expected_title or "", source_ids=[], evidence=[], complete=False,
        )
    detail = await get_story(session, story_id, source_ids, evidence_limit=100,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    story = detail.story if detail else None
    if (detail is None or story is None or detail.evidence_cursor is not None or story.incomplete_reasons
            or (expected_title is not None and story.title != expected_title)
            or story.evidence_count != len(story.evidence)
            or not story.evidence or len(story.evidence) > 100):
        return BriefStorySupport(
            story_id=story_id, title=expected_title or "", source_ids=[], evidence=[], complete=False,
        )
    source_set = sorted({item.source_id for item in story.evidence}, key=str)
    if source_set != sorted(source_ids, key=str):
        return BriefStorySupport(
            story_id=story_id, title=expected_title or "", source_ids=[], evidence=[], complete=False,
        )
    refs = [
        {"document_id": item.document_id, "document_version_id": item.document_version_id,
         "chunk_id": item.chunk_id, "source_id": item.source_id}
        for item in story.evidence
    ]
    if len({(ref["document_id"], ref["document_version_id"], ref["chunk_id"]) for ref in refs}) != len(refs):
        return BriefStorySupport(
            story_id=story_id, title=expected_title or "", source_ids=[], evidence=[], complete=False,
        )
    return BriefStorySupport(
        story_id=story_id, title=story.title, source_ids=source_set, evidence=refs, complete=True,
    )
