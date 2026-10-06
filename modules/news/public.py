"""Stable topic DTO/query/mutation facade.

The create, update, and delete exports commit the entire supplied SQLAlchemy
session, including unrelated pending changes. Callers own rollback/disposal when
an operation fails before commit and must rollback after failed commit before
reusing that session. Future seed or cross-module units of work must account for
this boundary; these exports are not flush-only composition helpers.

The explicit fictional seed export is flush-only and leaves transaction ownership
to the document-seed coordinator.
"""

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
    validate_export_fences,
    update_topic,
)
from modules.news.seed import ensure_demo_topics
from modules.news.relevance import score_relevance
from modules.news.schemas import (
    CorrelationBucketRead, CorrelationCoverageRead, CorrelationQuery, CorrelationResult,
    CiiUnavailableRead, RelevanceRead, StoryDetail, StoryFilter, StoryPage, StoryRead,
    TrendFilter, TrendPage, TrendRead,
)
from modules.news.correlation import build_correlations
from modules.news.stories import cluster_observation, get_story, list_stories
from modules.news.trends import list_trends
from modules.news.worker import process_news_document_ready, recover_news_work

__all__ = [
    "TopicConflict", "TopicCreate", "TopicExportFence", "TopicExportPage",
    "TopicExportValidation", "TopicFilter", "TopicMissing", "TopicPage",
    "TopicRead", "TopicUpdate", "create_topic", "delete_topic", "export_page", "get_topic",
    "ensure_demo_topics", "list_topics", "update_topic", "validate_export_fences",
    "RelevanceRead", "StoryDetail", "StoryFilter", "StoryPage", "StoryRead",
    "TrendFilter", "TrendPage", "TrendRead", "cluster_observation", "get_story",
    "list_stories", "list_trends", "process_news_document_ready", "recover_news_work",
    "score_relevance", "CorrelationBucketRead", "CorrelationCoverageRead", "CorrelationQuery",
    "CorrelationResult", "CiiUnavailableRead", "build_correlations",
]
