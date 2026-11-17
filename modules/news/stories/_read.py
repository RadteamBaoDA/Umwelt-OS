"""Public story list/detail reads and the translation-input read."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ValidationFailedError
from core.workspaces import public as workspaces
from core.workspaces.schemas import Scope, WorkspaceContext
from modules.knowledge.documents import public as documents
from modules.news import topics
from modules.news.models import NewsObservation
from modules.news.schemas import (
    StoryDetail,
    StoryEvidence,
    StoryFilter,
    StoryPage,
    StoryRead,
)
from modules.news.stories._common import ALGORITHM_VERSION, _admit, _tokens, _translation_revision
from modules.news.stories._cursors import (
    _decode_cursor,
    _decode_detail_cursor,
    _encode_detail_cursor,
)
from modules.news.stories._live import (
    _enrich_with_relevance,
    _evidence_still_current,
    _filter_live_story,
    _live_story_rows,
    _next_story_cursor,
    _resolve_source_scope,
)
from modules.news.stories._member import _member_get_story, _member_list_stories
from modules.translations.public import TranslationInput


async def list_stories(
    session: AsyncSession,
    filters: StoryFilter,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    image_signer: Callable[[str, bool], str | None] | None = None,
) -> StoryPage:
    """Return a bounded keyset page whose titles and counts use live authorized supports only.

    Read-only; order is admission snapshot, cursor/source resolution, live rows, topic profile,
    per-story filters, relevance preloads, scoring, then the final support recheck.
    """
    access_fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if filters.date_from and filters.date_to and filters.date_from >= filters.date_to:
        raise ValidationFailedError("date_from must be earlier than date_to")
    as_of = datetime.now(UTC)
    cursor = _decode_cursor(filters.cursor, access_fence, filters) if filters.cursor else None
    if isinstance(scope, WorkspaceContext) and scope.role == "member":
        return await _member_list_stories(session, filters, scope, access_fence, cursor, as_of)
    if cursor:
        as_of = cursor.as_of
        source_ids = tuple(cursor.resolved_source_ids)
        source_selection_incomplete = cursor.source_selection_incomplete
        after = (cursor.after_observed_at, cursor.after_story_id)
    else:
        source_ids, source_selection_incomplete = await _resolve_source_scope(
            session,
            tuple(filters.source_ids),
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        after = None
    # Share a single request-scoped document projection cache across candidate reprojection,
    # entity filtering, relevance scoring, and final support verification to avoid N+1 reads.
    projection_cache: dict[UUID, Any] = {}
    live = await _live_story_rows(
        session,
        source_ids,
        as_of,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        after=after,
        projection_cache=projection_cache,
        image_signer=image_signer,
    )
    filtered = []
    page_incomplete_reasons = set(live.incomplete_reasons)
    if source_selection_incomplete:
        page_incomplete_reasons.add("source_selection_limit")
    query_tokens = _tokens(filters.q or "")
    profile = None
    if filters.topic_id:
        profile = await topics.get_topic(
            session, filters.topic_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        if not profile.is_active:
            return StoryPage(items=[], next_cursor=None, as_of=as_of)
    for row in live:
        kept = await _filter_live_story(
            session,
            row,
            filters,
            profile,
            query_tokens,
            page_incomplete_reasons,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            projection_cache=projection_cache,
        )
        if kept is not None:
            filtered.append(kept)
    filtered.sort(
        key=lambda item: live.candidate_keys.get(item[0].id, (as_of, item[0].id)), reverse=True
    )
    page = filtered[: filters.limit + 1]
    has_more = len(page) > filters.limit
    page = page[: filters.limit]
    items = [
        StoryRead(
            id=story.id,
            title=representative.title,
            excerpt=representative.excerpt,
            observed_at=max(ev.observed_at for ev in evidence),
            source_count=len({ev.source_id for ev in evidence}),
            evidence_count=len(evidence),
            evidence=evidence[:100],
            image_url=next(
                (ev.image_url for ev in [representative, *evidence] if ev.image_url), None
            ),
            relevance_state="unavailable",
            translation_revision=_translation_revision(story.id, representative),
            incomplete_reasons=sorted(story_reasons),
        )
        for story, _observations, evidence, representative, story_reasons in page
    ]
    from modules.news.relevance import load_active_goals, load_active_topics

    preloaded_topics = (
        await load_active_topics(
            session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        if items
        else None
    )
    preloaded_goals = (
        await load_active_goals(
            session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        if items
        else None
    )
    scored = []
    for item in items:
        enriched = await _enrich_with_relevance(
            session,
            item,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            as_of=as_of,
            preloaded_topics=preloaded_topics,
            preloaded_goals=preloaded_goals,
            projection_cache=projection_cache,
        )
        if await _evidence_still_current(
            session,
            enriched,
            page,
            source_ids,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            projection_cache=projection_cache,
        ):
            scored.append(enriched)
        else:
            page_incomplete_reasons.add("evidence_changed_during_read")
    next_cursor = _next_story_cursor(
        live,
        page,
        has_more,
        access_fence,
        filters,
        as_of,
        source_ids,
        source_selection_incomplete,
    )
    page_reasons = sorted(
        page_incomplete_reasons | {reason for item in scored for reason in item.incomplete_reasons}
    )
    return StoryPage(
        items=scored,
        next_cursor=next_cursor,
        as_of=as_of,
        truncated=live.truncated,
        incomplete_reasons=page_reasons,
        capability="partial"
        if page_reasons or any(item.relevance_state != "available" for item in scored)
        else "available",
    )


async def _detail_support_is_current(
    session: AsyncSession,
    evidence: list[StoryEvidence],
    generation_by_document: dict[UUID, int],
    source_ids: tuple[UUID, ...],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> bool:
    """Recheck the exact revisions used in a detail response against current Documents/Sources.

    The page query may await scope and source calls, so each distinct (document, version) is
    reprojected immediately before the detached response is built. Stops at the first stale
    item and returns ``False``; reads only.
    """
    for item in evidence:
        current = await documents.get_news_document_projection(
            session,
            item.document_id,
            expected_source_generation=generation_by_document.get(item.document_id),
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if (
            current is None
            or current.document_version_id != item.document_version_id
            or current.source_id != item.source_id
            or current.source_id not in source_ids
        ):
            return False
    return True


async def get_story(
    session: AsyncSession,
    story_id: UUID,
    source_ids: tuple[UUID, ...],
    *,
    evidence_limit: int = 100,
    evidence_cursor: str | None = None,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> StoryDetail | None:
    """Return current excerpts or a title-free partial page with bounded continuation.

    The opaque v2 cursor binds workspace, actor, membership/configuration revision,
    the Story-detail domain, story, the resolved source snapshot, fixed ``as_of``,
    default-source truncation state, sort, and last scanned support key; the
    legacy six-field token is rejected.
    Historical/current authority is reprojected on each page; omitted source IDs
    during continuation reuse the pinned scope and can never widen it. A stale
    support window returns no title or evidence but retains a cursor when later
    retained supports remain; exhaustion without any current support returns None.
    Invalid cursors raise HTTP 422, and current document/source fences are checked
    before detached content is returned.
    """
    access_fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if not 1 <= evidence_limit <= 100:
        raise ValueError("Evidence page size must be between 1 and 100")
    if isinstance(scope, WorkspaceContext) and scope.role == "member":
        return await _member_get_story(
            session,
            story_id,
            source_ids,
            scope,
            access_fence,
            evidence_limit,
            evidence_cursor,
        )
    after_key: tuple[UUID, UUID, UUID] | None = None
    as_of = datetime.now(UTC)
    source_selection_incomplete = False
    if evidence_cursor:
        source_ids, as_of, after_key, source_selection_incomplete = _decode_detail_cursor(
            evidence_cursor,
            access_fence,
            story_id,
            source_ids,
        )
    else:
        source_ids, source_selection_incomplete = await _resolve_source_scope(
            session, source_ids, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
    base_live = await _live_story_rows(
        session,
        source_ids,
        as_of,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        story_id=story_id,
    )
    match = next((item for item in base_live if item[0].id == story_id), None)
    page_live = await _live_story_rows(
        session,
        source_ids,
        as_of,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        story_id=story_id,
        evidence_after=after_key,
        support_limit=evidence_limit,
    )
    page_match = next((item for item in page_live if item[0].id == story_id), None)
    evidence = page_match[2] if page_match else []
    evidence_page = evidence[:evidence_limit]
    next_evidence_cursor = None
    if page_live.next_evidence is not None:
        next_evidence_cursor = _encode_detail_cursor(
            access_fence,
            story_id,
            source_ids,
            as_of,
            page_live.next_evidence,
            source_selection_incomplete,
        )
    # A stale support prefix can hide later current evidence. Continue that
    # bounded scan without borrowing a title from stale observations.
    if match is None and page_match is None:
        if next_evidence_cursor is None:
            return None
        return StoryDetail(
            story=None,
            evidence_cursor=next_evidence_cursor,
            incomplete_reasons=sorted(
                page_live.incomplete_reasons | {"support_scan_limit", "stale_support_omitted"}
            ),
        )
    story_row = match or page_match
    if story_row is None:
        raise RuntimeError("invariant violated: story_row must not be None")
    story, observations, base_evidence = story_row
    title_evidence = base_evidence or (page_match[2] if page_match else [])
    representative = max(
        title_evidence,
        key=lambda item: (item.published_at or item.observed_at, str(item.document_id)),
    )
    incomplete_reasons = (
        base_live.incomplete_reasons
        | page_live.incomplete_reasons
        | {
            reason
            for observation in observations
            if observation.incomplete_reason
            for reason in observation.incomplete_reason.split(",")
        }
    )
    if source_selection_incomplete:
        incomplete_reasons.add("source_selection_limit")
    # The page query may await scope and source calls; recheck the exact revisions
    # used in the returned title immediately before building the detached response.
    page_observations = page_match[1] if page_match else []
    generation_by_document = {
        observation.document_id: observation.source_generation
        for observation in [*observations, *page_observations]
    }
    fence_evidence = list(
        {
            (item.document_id, item.document_version_id): item
            for item in [*base_evidence, *evidence_page]
        }.values()
    )
    if not await _detail_support_is_current(
        session,
        fence_evidence,
        generation_by_document,
        source_ids,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    ):
        if next_evidence_cursor is None:
            return None
        return StoryDetail(
            story=None,
            evidence_cursor=next_evidence_cursor,
            incomplete_reasons=sorted(
                set(incomplete_reasons) | {"evidence_changed_during_read", "stale_support_omitted"}
            ),
        )
    return StoryDetail(
        story=StoryRead(
            id=story.id,
            title=representative.title,
            excerpt=representative.excerpt,
            observed_at=max(item.observed_at for item in title_evidence),
            source_count=len({item.source_id for item in title_evidence}),
            evidence_count=len(base_evidence or title_evidence),
            evidence=evidence_page,
            relevance_state="unavailable",
            translation_revision=_translation_revision(story.id, representative),
            incomplete_reasons=sorted(incomplete_reasons),
        ),
        evidence_cursor=next_evidence_cursor,
        incomplete_reasons=sorted(incomplete_reasons),
    )


async def read_story_translation_input(
    session: AsyncSession,
    *,
    scope: WorkspaceContext,
    story_id: UUID,
    multi_workspace_enabled: bool,
) -> TranslationInput | None:
    """Return the translatable title/excerpt plus a visibility hash, or None when not visible or incomplete."""
    detail = await get_story(
        session,
        story_id,
        (),
        evidence_limit=100,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    story = detail.story if detail else None
    # ponytail: a story with >100 evidence rows is not translatable; page the hash if that matters.
    if detail is None or story is None or detail.evidence_cursor is not None or not story.evidence:
        return None
    versions = {item.document_version_id for item in story.evidence}
    rows = (
        await session.execute(
            select(
                NewsObservation.document_id,
                NewsObservation.document_version_id,
                NewsObservation.source_id,
                NewsObservation.source_generation,
                NewsObservation.local_only,
            ).where(
                NewsObservation.workspace_id == scope.workspace_id,
                NewsObservation.story_id == story_id,
                NewsObservation.algorithm_version == ALGORITHM_VERSION,
                NewsObservation.document_version_id.in_(versions),
            )
        )
    ).all()
    grant_revisions: dict[UUID, int] = {}
    if scope.role == "member":
        grants = await workspaces.read_resource_grants(
            session,
            scope=scope,
            kind="document",
            resource_ids=tuple(sorted({row.document_id for row in rows}, key=str)),
        )
        grant_revisions = {grant.resource_id: grant.share_revision for grant in grants}
    tuples = sorted(
        (
            str(row.document_id),
            str(row.document_version_id),
            str(row.source_id),
            row.source_generation,
            grant_revisions.get(row.document_id, 0),
            scope.membership_revision,
            bool(row.local_only),
        )
        for row in rows
    )
    return TranslationInput(
        workspace_id=scope.workspace_id,
        actor_user_id=scope.user_id,
        resource_type="news_story",
        resource_id=story_id,
        resource_revision=story.translation_revision,
        fields={"title": story.title, "excerpt": story.excerpt},
        visibility_hash=hashlib.sha256(
            json.dumps(tuples, separators=(",", ":")).encode()
        ).hexdigest(),
        source_ids=tuple(sorted({row.source_id for row in rows}, key=str)),
        local_only=any(t[-1] for t in tuples),
    )
