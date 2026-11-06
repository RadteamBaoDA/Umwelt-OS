"""Live (current-revision) story row assembly, filtering, relevance and evidence checks."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.schemas import AccessFence, Scope
from modules.knowledge.documents import public as documents
from modules.knowledge.entities import public as entities
from modules.news.models import NewsObservation, NewsStory
from modules.news.schemas import (
    StoryCursor,
    StoryEvidence,
    StoryFilter,
    StoryRead,
)
from modules.news.stories._common import (
    ALGORITHM_VERSION,
    LiveStoryRows,
    _attribution,
    _canonical_url,
    _plain_excerpt,
    _tokens,
)
from modules.news.stories._cursors import _encode_cursor, _filter_hash
from modules.sources import public as sources


async def _resolve_source_scope(
    session: AsyncSession,
    requested_source_ids: tuple[UUID, ...],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> tuple[tuple[UUID, ...], bool]:
    """Resolve explicit active sources or one bounded default-source page.

    Omitted source IDs select at most 32 active detached gadget sources. A
    continuation token pins those IDs; the partial flag records that more
    active sources existed without exposing their configuration or count.
    """
    if requested_source_ids:
        selected = await sources.get_gadget_sources(
            session,
            requested_source_ids,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        return tuple(item.id for item in selected if item.status == "active"), False
    page = await sources.list_active_gadget_sources(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, limit=32
    )
    return tuple(item.id for item in page.items), page.next_cursor is not None


async def _current_projection(
    session: AsyncSession,
    observation: NewsObservation,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    projection_cache: dict[UUID, Any] | None,
) -> Any:
    """Return the document projection for an observation's source generation, via the request cache.

    A cached projection from another source generation counts as absent (``None``) and is not
    refetched; a fresh read is cached only when it is non-``None``. Read-only.
    """
    if projection_cache is not None and observation.document_id in projection_cache:
        cached = projection_cache[observation.document_id]
        return (
            cached
            if cached is not None
            and cached.current_source_generation == observation.source_generation
            else None
        )
    current = await documents.get_news_document_projection(
        session,
        observation.document_id,
        expected_source_generation=observation.source_generation,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if projection_cache is not None and current is not None:
        projection_cache[observation.document_id] = current
    return current


async def _candidate_story_page(
    session: AsyncSession,
    active: set[UUID],
    as_of: datetime,
    *,
    scope: Scope,
    story_id: UUID | None,
    after: tuple[datetime, UUID] | None,
    evidence_after: tuple[UUID, UUID, UUID] | None,
    candidate_limit: int,
) -> tuple[list[Any], bool]:
    """Keyset-select candidate story IDs (latest eligible observation desc, story ID desc).

    Fetches ``candidate_limit + 1`` rows to detect another page; returns the bounded rows and
    whether more candidates exist (always ``False`` for single-story detail reads). One read.
    """
    latest_observed = func.max(NewsObservation.observed_at)
    candidate_query = (
        select(NewsObservation.story_id, latest_observed.label("latest_observed_at"))
        .where(
            NewsObservation.workspace_id == scope.workspace_id,
            NewsObservation.source_id.in_(active),
            NewsObservation.algorithm_version == ALGORITHM_VERSION,
            NewsObservation.created_at <= as_of,
            NewsObservation.observed_at <= as_of,
        )
        .group_by(NewsObservation.story_id)
    )
    if story_id is not None:
        candidate_query = candidate_query.where(NewsObservation.story_id == story_id)
    if story_id is not None and evidence_after is not None:
        candidate_query = candidate_query.where(
            tuple_(
                NewsObservation.source_id,
                NewsObservation.document_version_id,
                NewsObservation.chunk_id,
            )
            > evidence_after
        )
    if after is not None and story_id is None:
        candidate_query = candidate_query.having(
            tuple_(latest_observed, NewsObservation.story_id) < tuple_(after[0], after[1])
        )
    candidate_rows = list(
        (
            await session.execute(
                candidate_query.order_by(
                    latest_observed.desc(), NewsObservation.story_id.desc()
                ).limit(candidate_limit + 1)
            )
        ).all()
    )
    has_more = story_id is None and len(candidate_rows) > candidate_limit
    return candidate_rows[:candidate_limit], has_more


async def _ranked_support_rows(
    session: AsyncSession,
    candidate_ids: list[UUID],
    active: set[UUID],
    as_of: datetime,
    *,
    scope: Scope,
    story_id: UUID | None,
    evidence_after: tuple[UUID, UUID, UUID] | None,
    support_limit: int,
) -> list[tuple[NewsStory, NewsObservation, int]]:
    """Read at most ``support_limit + 1`` ranked support rows per candidate story.

    Detail reads rank by support key; list reads by newest observation. The extra row lets the
    caller detect a truncated support window. One read.
    """
    support_order_by = (
        (NewsObservation.source_id, NewsObservation.document_version_id, NewsObservation.chunk_id)
        if story_id is not None
        else (NewsObservation.observed_at.desc(), NewsObservation.id)
    )
    support_rank = (
        select(
            NewsObservation.id.label("observation_id"),
            func.row_number()
            .over(
                partition_by=NewsObservation.story_id,
                order_by=support_order_by,
            )
            .label("support_rank"),
        )
        .where(
            NewsObservation.workspace_id == scope.workspace_id,
            NewsObservation.story_id.in_(candidate_ids),
            NewsObservation.source_id.in_(active),
            NewsObservation.algorithm_version == ALGORITHM_VERSION,
            NewsObservation.created_at <= as_of,
            NewsObservation.observed_at <= as_of,
            *(
                [
                    tuple_(
                        NewsObservation.source_id,
                        NewsObservation.document_version_id,
                        NewsObservation.chunk_id,
                    )
                    > evidence_after
                ]
                if story_id is not None and evidence_after is not None
                else []
            ),
        )
        .subquery()
    )
    # The subquery column is untyped to SQLAlchemy's stubs; the select order fixes the row shape.
    return cast(
        list[tuple[NewsStory, NewsObservation, int]],
        list(
            (
                await session.execute(
                    select(NewsStory, NewsObservation, support_rank.c.support_rank)
                    .join(NewsObservation, NewsObservation.story_id == NewsStory.id)
                    .join(support_rank, support_rank.c.observation_id == NewsObservation.id)
                    .where(
                        support_rank.c.support_rank <= support_limit + 1,
                        NewsStory.workspace_id == scope.workspace_id,
                        NewsObservation.workspace_id == scope.workspace_id,
                    )
                    .order_by(
                        *(
                            (
                                NewsObservation.story_id,
                                NewsObservation.source_id,
                                NewsObservation.document_version_id,
                                NewsObservation.chunk_id,
                            )
                            if story_id is not None
                            else (
                                NewsObservation.story_id,
                                NewsObservation.observed_at.desc(),
                                NewsObservation.id,
                            )
                        )
                    )
                )
            ).all()
        ),
    )


async def _group_live_supports(
    session: AsyncSession,
    rows: list[tuple[NewsStory, NewsObservation, int]],
    selections: Sequence[Any],
    incomplete_reasons: set[str],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    support_limit: int,
    projection_cache: dict[UUID, Any] | None,
) -> tuple[
    dict[UUID, tuple[NewsStory, list[NewsObservation], list[StoryEvidence]]],
    tuple[UUID, UUID, UUID] | None,
]:
    """Reproject each ranked support against current Documents/Sources authority and group by story.

    Rows beyond ``support_limit`` only mark ``support_scan_limit`` and set the continuation key
    to the last in-window support. Unavailable scope adds ``scope_unavailable``. ``incomplete_reasons``
    is mutated in place; reads only.
    """
    grouped: dict[UUID, tuple[NewsStory, list[NewsObservation], list[StoryEvidence]]] = {}
    last_evidence_key: dict[UUID, tuple[UUID, UUID, UUID]] = {}
    next_evidence = None
    selections_by_id = {item.id: item for item in selections}
    for story, observation, support_order in rows:
        if support_order > support_limit:
            incomplete_reasons.add("support_scan_limit")
            next_evidence = last_evidence_key.get(story.id)
            continue
        last_evidence_key[story.id] = (
            observation.source_id,
            observation.document_version_id,
            observation.chunk_id,
        )
        current = await _current_projection(
            session,
            observation,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            projection_cache=projection_cache,
        )
        if current is None or current.document_version_id != observation.document_version_id:
            if current is None and await documents.news_projection_scope_unavailable(
                session,
                observation.document_id,
                observation.source_generation,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            ):
                incomplete_reasons.add("scope_unavailable")
            continue
        chunk = next((item for item in current.chunks if item.id == observation.chunk_id), None)
        if chunk is None:
            continue
        source = selections_by_id.get(current.source_id)
        if source is None or source.status != "active":
            continue
        entry = StoryEvidence(
            document_id=current.document_id,
            document_version_id=current.document_version_id,
            chunk_id=chunk.id,
            source_id=current.source_id,
            source_name=current.source_name,
            source_type=current.source_type,
            provider=current.provider,
            url=_canonical_url(current.canonical_url),
            title=current.title,
            excerpt=_plain_excerpt(current.title, chunk.content),
            observed_at=current.observed_at,
            published_at=current.published_at,
            **_attribution(current.provider_metadata),
        )
        existing = grouped.setdefault(story.id, (story, [], []))
        existing[1].append(observation)
        existing[2].append(entry)
    return grouped, next_evidence


async def _live_story_rows(
    session: AsyncSession,
    source_ids: tuple[UUID, ...],
    as_of: datetime,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    after: tuple[datetime, UUID] | None = None,
    story_id: UUID | None = None,
    evidence_after: tuple[UUID, UUID, UUID] | None = None,
    support_limit: int = 100,
    candidate_limit: int = 50,
    projection_cache: dict[UUID, Any] | None = None,
) -> LiveStoryRows:
    """Keyset-page durable story candidates, then reproject a bounded support window.

    The keyset is ordered by latest eligible observation and story ID at the fixed
    ``as_of`` snapshot. Detail callers can constrain one story directly, avoiding a
    global prefix scan. Current Documents/Sources authority is rechecked for output,
    optionally populating ``projection_cache`` so downstream scoring and filtering
    Within the same request can reuse live document projections without N+1 queries.
    Read-only; order is source selection, scope status, candidate page, support rows, reprojection.
    """
    if not source_ids:
        return LiveStoryRows([], False)
    selections = await sources.get_gadget_sources(
        session, source_ids, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    active = {item.id for item in selections if item.status == "active"}
    if not active:
        return LiveStoryRows([], False)
    scope_status = await documents.news_current_scope_status(
        session,
        tuple(sorted(active, key=str)),
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    incomplete_reasons: set[str] = set(scope_status.incomplete_reasons)
    candidate_rows, has_more = await _candidate_story_page(
        session,
        active,
        as_of,
        scope=scope,
        story_id=story_id,
        after=after,
        evidence_after=evidence_after,
        candidate_limit=candidate_limit,
    )
    candidate_ids = [row.story_id for row in candidate_rows]
    if not candidate_ids:
        return LiveStoryRows([], False, incomplete_reasons)
    rows = await _ranked_support_rows(
        session,
        candidate_ids,
        active,
        as_of,
        scope=scope,
        story_id=story_id,
        evidence_after=evidence_after,
        support_limit=support_limit,
    )
    grouped, next_evidence = await _group_live_supports(
        session,
        rows,
        selections,
        incomplete_reasons,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        support_limit=support_limit,
        projection_cache=projection_cache,
    )
    next_candidate = None
    if has_more and candidate_rows and story_id is None:
        last = candidate_rows[-1]
        next_candidate = (last.latest_observed_at, last.story_id)
        incomplete_reasons.add("candidate_scan_limit")
    candidate_keys = {
        row.story_id: (row.latest_observed_at, row.story_id) for row in candidate_rows
    }
    return LiveStoryRows(
        list(grouped.values()),
        has_more,
        incomplete_reasons,
        next_candidate,
        candidate_keys,
        next_evidence,
    )


async def _current_entity_ids(
    session: AsyncSession,
    observations: list[NewsObservation],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    projection_cache: dict[UUID, Any] | None = None,
) -> tuple[set[str], str | None]:
    """Resolve current entity IDs, returning a reason when a bounded scan is incomplete.

    Documents and Entities permission/deletion failures retain their normal behavior;
    chunk or membership overflow degrades this optional filter without inventing an
    empty match set. The reason is one of the fixed UI-safe codes or None.
    """
    output: set[str] = set()
    incomplete_reason = None
    for observation in observations[:100]:
        current = await _current_projection(
            session,
            observation,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            projection_cache=projection_cache,
        )
        if current is None or current.document_version_id != observation.document_version_id:
            continue
        if current.chunks_truncated:
            output.clear()
            incomplete_reason = "chunk_limit"
            break
        try:
            refs = await entities.list_version_membership_refs(
                session,
                current.document_version_id,
                [item.id for item in current.chunks],
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        except ValueError:
            output.clear()
            incomplete_reason = "entity_membership_limit"
            break
        output.update(str(item.entity_id) for item in refs)
    return output, incomplete_reason


async def _filter_live_story(
    session: AsyncSession,
    row: tuple[NewsStory, list[NewsObservation], list[StoryEvidence]],
    filters: StoryFilter,
    profile: Any,
    query_tokens: set[str],
    page_incomplete_reasons: set[str],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    projection_cache: dict[UUID, Any],
) -> tuple[NewsStory, list[NewsObservation], list[StoryEvidence], StoryEvidence, set[str]] | None:
    """Apply date, entity, topic-profile and text filters to one live story.

    Returns ``(story, observations, evidence, representative, story_reasons)`` or ``None`` when the
    story is filtered out. Entity-membership overflow marks both the story and
    ``page_incomplete_reasons`` (mutated in place) and keeps the story rather than inventing an
    empty match. Reads only (through the shared ``projection_cache``).
    """
    story, observations, evidence = row
    story_incomplete_reasons = {
        reason
        for observation in observations
        if observation.incomplete_reason
        for reason in observation.incomplete_reason.split(",")
    }
    if (filters.date_from or filters.date_to) and not any(
        (filters.date_from is None or item.observed_at >= filters.date_from)
        and (filters.date_to is None or item.observed_at < filters.date_to)
        for item in evidence
    ):
        return None
    current_entities: set[str] | None = None
    membership_incomplete = None
    if filters.entity_id or profile:
        current_entities, membership_incomplete = await _current_entity_ids(
            session,
            observations,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            projection_cache=projection_cache,
        )
        if membership_incomplete:
            story_incomplete_reasons.add(membership_incomplete)
            page_incomplete_reasons.add(membership_incomplete)
    if (
        filters.entity_id
        and not membership_incomplete
        and str(filters.entity_id) not in (current_entities or set())
    ):
        return None
    if profile:
        profile_tokens = _tokens(
            " ".join([profile.name, profile.description or "", *profile.keywords])
        )
        lexical_hit = any(
            profile_tokens.intersection(_tokens(item.title + " " + item.excerpt))
            for item in evidence
        )
        entity_hit = bool(
            {str(value) for value in profile.entity_ids}.intersection(current_entities or set())
        )
        if not lexical_hit and not entity_hit and not membership_incomplete:
            return None
    if query_tokens and not any(
        query_tokens.issubset(_tokens(item.title + " " + item.excerpt)) for item in evidence
    ):
        return None
    representative = max(
        evidence, key=lambda item: (item.published_at or item.observed_at, str(item.document_id))
    )
    return story, observations, evidence, representative, story_incomplete_reasons


async def _enrich_with_relevance(
    session: AsyncSession,
    item: StoryRead,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    as_of: datetime,
    preloaded_topics: list[Any] | None,
    preloaded_goals: list[Any] | None,
    projection_cache: dict[UUID, Any],
) -> StoryRead:
    """Return a copy of ``item`` carrying its seven-signal relevance and any entity-scan reason."""
    from modules.news.relevance import score_relevance

    relevance = await score_relevance(
        session,
        item,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        as_of=as_of,
        preloaded_topics=preloaded_topics,
        preloaded_goals=preloaded_goals,
        projection_cache=projection_cache,
    )
    entity_reason = {
        "membership_limit": "entity_membership_limit",
        "chunk_limit": "chunk_limit",
    }.get(relevance.signals["entity"].method)
    return item.model_copy(
        update={
            "relevance": relevance.score,
            "why_relevant": relevance.why_relevant,
            "relevance_signals": {
                name: value.model_dump() for name, value in relevance.signals.items()
            },
            "relevance_weights": relevance.weights,
            "relevance_as_of": relevance.as_of,
            "relevance_profile_revisions": relevance.profile_revisions,
            "relevance_state": "available"
            if all(value.available for value in relevance.signals.values())
            else "partial",
            "incomplete_reasons": sorted(
                set(item.incomplete_reasons) | ({entity_reason} if entity_reason else set())
            ),
        }
    )


async def _evidence_still_current(
    session: AsyncSession,
    enriched: StoryRead,
    page: list[Any],
    source_ids: tuple[UUID, ...],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    projection_cache: dict[UUID, Any],
) -> bool:
    """Verify each evidence item against the resolved projection cache.

    Falls back to a direct projection read only when a document is absent from the cache (and
    caches a non-``None`` result), so the page's support stays valid at response time. Stops at
    the first stale item. Read-only.
    """
    story_row = next((row for row in page if row[0].id == enriched.id), None)
    generation_by_document = (
        {observation.document_id: observation.source_generation for observation in story_row[1]}
        if story_row is not None
        else {}
    )
    for story_evidence in enriched.evidence:
        expected_gen = generation_by_document.get(story_evidence.document_id)
        if story_evidence.document_id in projection_cache:
            cached = projection_cache[story_evidence.document_id]
            current = (
                cached
                if cached is not None
                and (expected_gen is None or cached.current_source_generation == expected_gen)
                else None
            )
        else:
            current = await documents.get_news_document_projection(
                session,
                story_evidence.document_id,
                expected_source_generation=expected_gen,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
            if current is not None:
                projection_cache[story_evidence.document_id] = current
        if (
            current is None
            or current.document_version_id != story_evidence.document_version_id
            or current.source_id != story_evidence.source_id
            or current.source_id not in source_ids
        ):
            return False
    return True


def _next_story_cursor(
    live: LiveStoryRows,
    page: list[Any],
    has_more: bool,
    access_fence: AccessFence,
    filters: StoryFilter,
    as_of: datetime,
    source_ids: tuple[UUID, ...],
    source_selection_incomplete: bool,
) -> str | None:
    """Encode the continuation cursor from the last returned story or the scan-limit candidate. Pure."""
    continuation_key = None
    if has_more and page:
        continuation_key = live.candidate_keys.get(page[-1][0].id)
    elif live.next_candidate is not None:
        continuation_key = live.next_candidate
    if continuation_key is None:
        return None
    return _encode_cursor(
        StoryCursor(
            domain="news_stories",
            workspace_id=access_fence.workspace_id,
            actor_user_id=access_fence.user_id,
            membership_revision=access_fence.membership_revision,
            configuration_revision=access_fence.configuration_revision,
            filter_hash=_filter_hash(filters),
            sort="observed_at_desc_story_id_desc",
            as_of=as_of,
            after_observed_at=continuation_key[0],
            after_story_id=continuation_key[1],
            resolved_source_ids=list(source_ids),
            source_selection_incomplete=source_selection_incomplete,
        )
    )
