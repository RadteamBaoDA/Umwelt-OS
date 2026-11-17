"""Member-projection story list/detail reads served from stored member evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ForbiddenError
from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, WorkspaceContext
from modules.knowledge.documents.public import DocumentReadProjection as Document
from modules.news.models import NewsObservation
from modules.news.schemas import (
    StoryCursor,
    StoryDetail,
    StoryEvidence,
    StoryFilter,
    StoryPage,
    StoryRead,
)
from modules.news.stories._common import (
    _LIVE_STATUS,
    ALGORITHM_VERSION,
    _canonical_url,
    _stored_attribution,
    _tokens,
    _translation_revision,
)
from modules.news.stories._cursors import (
    _decode_detail_cursor,
    _encode_cursor,
    _encode_detail_cursor,
    _filter_hash,
)
from modules.sources.public import SourceReadProjection as Source


def _member_obs(
    scope: WorkspaceContext, as_of: datetime, source_ids: tuple[UUID, ...], *columns: Any
) -> Select[Any]:
    """Select live observations whose document is granted to the member; the grant filter precedes any LIMIT."""
    query = (
        select(*columns)
        .select_from(NewsObservation)
        .join(
            Document,
            and_(
                Document.id == NewsObservation.document_id,
                Document.workspace_id == scope.workspace_id,
                Document.current_version == NewsObservation.version_number,
                Document.extraction_status.in_(_LIVE_STATUS),
            ),
        )
        .join(
            Source,
            and_(
                Source.id == NewsObservation.source_id,
                Source.workspace_id == scope.workspace_id,
                Source.status == "active",
                Source.generation == NewsObservation.source_generation,
            ),
        )
        .where(
            NewsObservation.workspace_id == scope.workspace_id,
            NewsObservation.algorithm_version == ALGORITHM_VERSION,
            NewsObservation.document_id.in_(
                workspaces.granted_resource_ids(scope=scope, kind="document")
            ),
            NewsObservation.created_at <= as_of,
            NewsObservation.observed_at <= as_of,
        )
    )
    return query.where(NewsObservation.source_id.in_(source_ids)) if source_ids else query


def _member_evidence(observation: NewsObservation, source: Source) -> StoryEvidence:
    """Detach one authorized observation; title and excerpt never come from hidden rows."""
    return StoryEvidence(
        document_id=observation.document_id,
        document_version_id=observation.document_version_id,
        chunk_id=observation.chunk_id,
        source_id=observation.source_id,
        source_name=source.name,
        source_type=source.type,
        provider=observation.provider,
        url=_canonical_url(observation.canonical_url),
        title=observation.title,
        excerpt=observation.excerpt[:1000],
        observed_at=observation.observed_at,
        published_at=observation.published_at,
        **_stored_attribution(observation),
    )


def _representative(evidence: list[StoryEvidence]) -> StoryEvidence:
    """Pick the newest published/observed evidence, ties by document ID."""
    return max(
        evidence, key=lambda item: (item.published_at or item.observed_at, str(item.document_id))
    )


async def _member_list_stories(
    session: AsyncSession,
    filters: StoryFilter,
    scope: WorkspaceContext,
    fence: AccessFence,
    cursor: StoryCursor | None,
    as_of: datetime,
) -> StoryPage:
    """Page stories over the member's authorized evidence only; zero authorized evidence hides the story."""
    if filters.topic_id or filters.entity_id:
        raise ForbiddenError("Workspace owner required")
    if cursor:
        as_of = cursor.as_of
    source_ids = tuple(cursor.resolved_source_ids) if cursor else tuple(filters.source_ids)
    latest = func.max(NewsObservation.observed_at)
    query = _member_obs(
        scope, as_of, source_ids, NewsObservation.story_id, latest.label("latest")
    ).group_by(NewsObservation.story_id)
    if cursor:
        query = query.having(
            tuple_(latest, NewsObservation.story_id)
            < tuple_(cursor.after_observed_at, cursor.after_story_id)
        )
    candidates = list(
        (
            await session.execute(
                query.order_by(latest.desc(), NewsObservation.story_id.desc()).limit(
                    filters.limit + 1
                )
            )
        ).all()
    )
    has_more = len(candidates) > filters.limit
    candidates = candidates[: filters.limit]
    ids = [row.story_id for row in candidates]
    evidence: dict[UUID, list[StoryEvidence]] = {}
    counts: dict[UUID, tuple[int, int]] = {}
    if ids:
        rank = (
            _member_obs(
                scope,
                as_of,
                source_ids,
                NewsObservation.id.label("oid"),
                func.row_number()
                .over(
                    partition_by=NewsObservation.story_id,
                    order_by=(NewsObservation.observed_at.desc(), NewsObservation.id),
                )
                .label("rank"),
            )
            .where(NewsObservation.story_id.in_(ids))
            .subquery()
        )
        rows = (
            await session.execute(
                select(NewsObservation, Source)
                .join(rank, rank.c.oid == NewsObservation.id)
                .join(
                    Source,
                    and_(
                        Source.id == NewsObservation.source_id,
                        Source.workspace_id == scope.workspace_id,
                    ),
                )
                .where(rank.c.rank <= 100)
                .order_by(
                    NewsObservation.story_id, NewsObservation.observed_at.desc(), NewsObservation.id
                )
            )
        ).all()
        for observation, source in rows:
            evidence.setdefault(observation.story_id, []).append(
                _member_evidence(observation, source)
            )
        counts = {
            row.story_id: (row.evidence_count, row.source_count)
            for row in (
                await session.execute(
                    _member_obs(
                        scope,
                        as_of,
                        source_ids,
                        NewsObservation.story_id,
                        func.count().label("evidence_count"),
                        func.count(NewsObservation.source_id.distinct()).label("source_count"),
                    )
                    .where(NewsObservation.story_id.in_(ids))
                    .group_by(NewsObservation.story_id)
                )
            ).all()
        }
    query_tokens = _tokens(filters.q or "")
    items = []
    for row in candidates:
        story_evidence = evidence.get(row.story_id)
        if not story_evidence:
            continue
        if (filters.date_from or filters.date_to) and not any(
            (filters.date_from is None or item.observed_at >= filters.date_from)
            and (filters.date_to is None or item.observed_at < filters.date_to)
            for item in story_evidence
        ):
            continue
        if query_tokens and not any(
            query_tokens.issubset(_tokens(item.title + " " + item.excerpt))
            for item in story_evidence
        ):
            continue
        rep = _representative(story_evidence)
        evidence_count, source_count = counts.get(
            row.story_id, (len(story_evidence), len({item.source_id for item in story_evidence}))
        )
        items.append(
            StoryRead(
                id=row.story_id,
                title=rep.title,
                excerpt=rep.excerpt,
                observed_at=row.latest,
                source_count=source_count,
                evidence_count=evidence_count,
                evidence=story_evidence,
                relevance_state="unavailable",
                translation_revision=_translation_revision(row.story_id, rep),
            )
        )
    next_cursor = None
    if has_more and candidates:
        last = candidates[-1]
        next_cursor = _encode_cursor(
            StoryCursor(
                domain="news_stories",
                workspace_id=fence.workspace_id,
                actor_user_id=fence.user_id,
                membership_revision=fence.membership_revision,
                configuration_revision=fence.configuration_revision,
                filter_hash=_filter_hash(filters),
                sort="observed_at_desc_story_id_desc",
                as_of=as_of,
                after_observed_at=last.latest,
                after_story_id=last.story_id,
                resolved_source_ids=list(source_ids),
            )
        )
    return StoryPage(items=items, next_cursor=next_cursor, as_of=as_of, capability="partial")


async def _member_get_story(
    session: AsyncSession,
    story_id: UUID,
    source_ids: tuple[UUID, ...],
    scope: WorkspaceContext,
    fence: AccessFence,
    evidence_limit: int,
    evidence_cursor: str | None,
) -> StoryDetail | None:
    """Return one story's authorized evidence page; a story with no authorized evidence is None (404)."""
    as_of, after = datetime.now(UTC), None
    if evidence_cursor:
        source_ids, as_of, after, _incomplete = _decode_detail_cursor(
            evidence_cursor, fence, story_id, source_ids
        )
    own = NewsObservation.story_id == story_id
    total = (
        await session.execute(
            _member_obs(
                scope,
                as_of,
                source_ids,
                func.count().label("n"),
                func.count(NewsObservation.source_id.distinct()).label("s"),
                func.max(NewsObservation.observed_at).label("latest"),
            ).where(own)
        )
    ).one()
    if not total.n:
        return None
    rep_row = (
        await session.execute(
            _member_obs(scope, as_of, source_ids, NewsObservation, Source)
            .where(own)
            .order_by(
                func.coalesce(NewsObservation.published_at, NewsObservation.observed_at).desc(),
                NewsObservation.document_id.desc(),
            )
            .limit(1)
        )
    ).one()
    rep = _member_evidence(rep_row[0], rep_row[1])
    key = (NewsObservation.source_id, NewsObservation.document_version_id, NewsObservation.chunk_id)
    page_query = _member_obs(scope, as_of, source_ids, NewsObservation, Source).where(own)
    if after is not None:
        page_query = page_query.where(tuple_(*key) > after)
    page = list((await session.execute(page_query.order_by(*key).limit(evidence_limit + 1))).all())
    more = len(page) > evidence_limit
    page = page[:evidence_limit]
    next_cursor = None
    if more:
        last = page[-1][0]
        next_cursor = _encode_detail_cursor(
            fence,
            story_id,
            source_ids,
            as_of,
            (last.source_id, last.document_version_id, last.chunk_id),
            False,
        )
    return StoryDetail(
        story=StoryRead(
            id=story_id,
            title=rep.title,
            excerpt=rep.excerpt,
            observed_at=total.latest,
            source_count=total.s,
            evidence_count=total.n,
            evidence=[_member_evidence(o, s) for o, s in page],
            relevance_state="unavailable",
            translation_revision=_translation_revision(story_id, rep),
        ),
        evidence_cursor=next_cursor,
    )
