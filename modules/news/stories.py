"""Deterministic bounded story clustering and live-evidence owner queries."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import Select, and_, func, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, Scope, WorkspaceContext
from modules.knowledge.documents import public as documents
from modules.knowledge.documents.models import Document
from modules.knowledge.entities import public as entities
from modules.news import topics
from modules.news.models import NewsObservation, NewsStory, NewsStoryIdentity
from modules.news.schemas import (
    StoryCursor,
    StoryDetail,
    StoryEvidence,
    StoryFilter,
    StoryPage,
    StoryRead,
)
from modules.search import public as search
from modules.sources import public as sources
from modules.sources.models import Source
from modules.translations.schemas import TranslationInput

ALGORITHM_VERSION = 1
MAX_CANDIDATES = 100
FUZZY_WINDOW = timedelta(hours=72)
FUZZY_THRESHOLD = 0.92


async def _admit(session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool) -> AccessFence:
    """Admit story reads (owner or member) before source selection, counts, or enrichment."""
    if type(multi_workspace_enabled) is not bool:
        raise TypeError("The configured multi-workspace feature flag must be a boolean")
    return await workspaces.read_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )


class LiveStoryRows(list[tuple[NewsStory, list[NewsObservation], list[StoryEvidence]]]):
    """Carry whether the bounded database scan omitted additional observations."""

    def __init__(
        self, values: list[tuple[NewsStory, list[NewsObservation], list[StoryEvidence]]],
        truncated: bool, incomplete_reasons: set[str] | None = None,
        next_candidate: tuple[datetime, UUID] | None = None,
        candidate_keys: dict[UUID, tuple[datetime, UUID]] | None = None,
        next_evidence: tuple[UUID, UUID, UUID] | None = None,
    ):
        """Retain bounded live groups with scan truncation and omission reasons."""
        super().__init__(values)
        self.truncated = truncated
        self.incomplete_reasons = incomplete_reasons or set()
        self.next_candidate = next_candidate
        self.candidate_keys = candidate_keys or {}
        self.next_evidence = next_evidence


def _canonical_url(value: str | None) -> str | None:
    """Normalize only URL host/default port/fragment while preserving query semantics."""
    if not value or len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            return None
        host = parsed.hostname.lower()
        port = parsed.port
        netloc = host if port is None or (parsed.scheme.lower(), port) in (("http", 80), ("https", 443)) else f"{host}:{port}"
        return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))
    except ValueError:
        return None


def _identity_keys(url: str | None, content_hash: str) -> list[tuple[str, str]]:
    """Return canonical URL first and exact content hash second for deterministic grouping."""
    canonical = _canonical_url(url)
    values = ([('url', canonical)] if canonical else []) + [('hash', content_hash.lower())]
    return [(kind, hashlib.sha256(f"{kind}:{value}".encode()).hexdigest()) for kind, value in values]


def _tokens(value: str) -> set[str]:
    """Extract bounded lowercase word tokens for transparent lexical comparisons."""
    return {token for token in re.findall(r"[\w]{2,64}", value.casefold()) if len(token) <= 64}


def _initial_signal_provenance() -> dict[str, object]:
    """Initialize all seven signals as explicitly unevaluated pending live profiles."""
    names = ("topic", "entity", "goal", "project", "recency", "importance", "novelty")
    return {
        "formula_version": 1,
        "weights": {name: 1.0 / 7.0 for name in names},
        "signals": {
            name: {"value": 0.0, "available": False, "method": "not_evaluated_at_ingest", "evidence_ids": []}
            for name in names
        },
    }


async def cluster_observation(
    session: AsyncSession, *, document_id: UUID, expected_source_generation: int,
    scope: Scope, multi_workspace_enabled: bool,
) -> UUID | None:
    """Persist one ready version's exact identity or conservative fuzzy grouping decision.

    The caller holds sorted source then document locks and owns the same durable
    outbox transaction. Clustering consumes detached owner projections only;
    equal identities serialize on a PostgreSQL transaction advisory lock, while
    unique version/generation/algorithm identity makes replay idempotent.
    """
    projection = await documents.get_news_document_projection(
        session, document_id, expected_source_generation=expected_source_generation,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if projection is None:
        return None
    prior = await session.scalar(select(NewsObservation).where(
        NewsObservation.workspace_id == scope.workspace_id,
        NewsObservation.document_version_id == projection.document_version_id,
        NewsObservation.source_generation == projection.current_source_generation,
        NewsObservation.algorithm_version == ALGORITHM_VERSION,
    ))
    if prior is not None:
        return prior.story_id
    representative = projection.chunks[0]
    incomplete_reasons: list[str] = []
    if projection.chunks_truncated:
        incomplete_reasons.append("chunk_limit")
    try:
        memberships = await entities.list_version_membership_refs(
            session, projection.document_version_id, [chunk.id for chunk in projection.chunks],
            scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        entity_ids = sorted({str(item.entity_id) for item in memberships})
    except ValueError:
        entity_ids = []
        incomplete_reasons.append("entity_membership_limit")
    title = " ".join(projection.title.split())[:500] or "Untitled"
    excerpt = representative.content[:1000]
    observed_at = projection.observed_at.astimezone(UTC)
    identities = _identity_keys(projection.canonical_url, projection.content_hash)
    kind, key = identities[0]
    for _identity_kind, identity_key in sorted(identities, key=lambda item: item[1]):
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
            {"identity": f"news:{identity_key}"},
        )
    story = None
    for identity_kind, identity_key in identities:
        identity = await session.scalar(select(NewsStoryIdentity).where(
            NewsStoryIdentity.identity_key == identity_key,
            NewsStoryIdentity.workspace_id == scope.workspace_id,
            NewsStoryIdentity.algorithm_version == ALGORITHM_VERSION,
        ))
        if identity is not None:
            kind, key = identity_kind, identity_key
            story = await session.scalar(select(NewsStory).where(
                NewsStory.id == identity.story_id, NewsStory.workspace_id == scope.workspace_id,
            ).with_for_update())
            break
    match_method = kind
    match_evidence: dict[str, object] = {
        "identity_kind": kind, "identity_key": key, "algorithm_version": ALGORITHM_VERSION,
    }
    if story is None:
        fuzzy = await _fuzzy_candidate(
            session, projection=projection, current_entity_ids=set(entity_ids), observed_at=observed_at,
            scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        if fuzzy is not None:
            story, fuzzy_evidence = fuzzy
            match_method = "embedding_entity_time"
            match_evidence.update(fuzzy_evidence)
    if story is None:
        story = NewsStory(workspace_id=scope.workspace_id, identity_key=key, identity_kind=kind, algorithm_version=ALGORITHM_VERSION)
        session.add(story)
        await session.flush()
    for identity_kind, identity_key in identities:
        if await session.scalar(select(NewsStoryIdentity.id).where(
            NewsStoryIdentity.identity_key == identity_key,
            NewsStoryIdentity.workspace_id == scope.workspace_id,
            NewsStoryIdentity.algorithm_version == ALGORITHM_VERSION,
        )) is None:
            session.add(NewsStoryIdentity(
                workspace_id=scope.workspace_id, story_id=story.id, identity_key=identity_key, identity_kind=identity_kind,
                algorithm_version=ALGORITHM_VERSION,
            ))
    session.add(NewsObservation(
        workspace_id=scope.workspace_id, story_id=story.id, document_id=projection.document_id,
        document_version_id=projection.document_version_id, chunk_id=representative.id,
        source_id=projection.source_id, source_generation=projection.current_source_generation,
        version_number=projection.version_number, canonical_url=_canonical_url(projection.canonical_url),
        content_hash=projection.content_hash, title=title, excerpt=excerpt,
        provider=projection.provider, observed_at=observed_at, published_at=projection.published_at,
        local_only=projection.local_only, membership_entity_ids=entity_ids,
        match_method=match_method, match_evidence=match_evidence,
        incomplete_reason=",".join(incomplete_reasons) or None,
        recorded_signals=_initial_signal_provenance(),
    ))
    await session.flush()
    return story.id


async def _fuzzy_candidate(
    session: AsyncSession, *, projection: documents.NewsDocumentProjection,
    current_entity_ids: set[str], observed_at: datetime, scope: Scope, multi_workspace_enabled: bool,
) -> tuple[NewsStory, dict[str, object]] | None:
    """Select one high-confidence same-generation vector/entity/time candidate from at most 100 rows."""
    if projection.local_only or not current_entity_ids:
        return None
    candidate_rows = list((await session.execute(
        select(NewsObservation, NewsStory)
        .join(NewsStory, NewsStory.id == NewsObservation.story_id)
        .where(
            NewsObservation.workspace_id == scope.workspace_id,
            NewsStory.workspace_id == scope.workspace_id,
            NewsObservation.source_id != projection.source_id,
            NewsObservation.observed_at >= observed_at - FUZZY_WINDOW,
            NewsObservation.observed_at <= observed_at + FUZZY_WINDOW,
            NewsObservation.local_only.is_(False),
        ).order_by(NewsObservation.observed_at.desc(), NewsObservation.id).limit(MAX_CANDIDATES + 1)
    )).all())
    if len(candidate_rows) > MAX_CANDIDATES:
        return None
    live_candidates: list[tuple[NewsObservation, NewsStory, list[str]]] = []
    for observation, story in candidate_rows:
        if abs((observation.observed_at - observed_at).total_seconds()) > FUZZY_WINDOW.total_seconds():
            continue
        candidate_projection = await documents.get_news_document_projection(
            session, observation.document_id, expected_source_generation=observation.source_generation,
            scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        if (
            candidate_projection is None
            or candidate_projection.document_version_id != observation.document_version_id
            or observation.chunk_id not in {item.id for item in candidate_projection.chunks}
        ):
            continue
        refs = await entities.list_version_membership_refs(
            session, observation.document_version_id, [observation.chunk_id],
            scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        overlap = sorted(current_entity_ids.intersection(str(item.entity_id) for item in refs))
        if overlap:
            live_candidates.append((observation, story, overlap))
    scored_rows = live_candidates
    if not scored_rows:
        return None
    similarity = await search.compare_news_evidence_embeddings(
        session, projection.chunks[0].id,
        tuple(item.chunk_id for item, _story, _entity_overlap in scored_rows),
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    scores = {item.right_chunk_id: item for item in similarity.items}
    ranked = sorted(
        ((scores[observation.chunk_id].cosine_similarity, str(story.id), story, observation, overlap, scores[observation.chunk_id])
         for observation, story, overlap in scored_rows if observation.chunk_id in scores),
        key=lambda item: (-item[0], item[1], str(item[3].id)),
    )
    if not ranked or ranked[0][0] < FUZZY_THRESHOLD:
        return None
    score, _story_key, story, observation, entity_overlap, vector = ranked[0]
    return story, {
        "cosine_similarity": score, "cosine_threshold": FUZZY_THRESHOLD,
        "time_window_hours": 72, "shared_canonical_entity_ids": entity_overlap,
        "candidate_observation_id": str(observation.id), "candidate_chunk_id": str(observation.chunk_id),
        "generation_id": str(vector.generation_id), "model_id": vector.model_id,
        "model_version": vector.model_version, "response_model_id": vector.response_model_id,
        "dimensions": vector.dimensions, "gateway_identity": vector.gateway_identity,
        "search_capability": similarity.capability,
    }


def _filter_hash(filters: StoryFilter) -> str:
    """Hash every story query constraint except cursor and page size."""
    value = filters.model_dump(exclude={"cursor", "limit"}, mode="json")
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _encode_cursor(cursor: StoryCursor) -> str:
    """Serialize and sign no data, using canonical base64 plus owner/filter binding."""
    raw = cursor.model_dump_json(exclude_none=False)
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(value: str, access_fence: AccessFence, filters: StoryFilter) -> StoryCursor:
    """Reject legacy, cross-workspace, stale-admission, or query-mismatched cursors."""
    try:
        if not value or len(value) > 4096 or "=" in value:
            raise ValueError
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        cursor = StoryCursor.model_validate_json(raw)
        if (
            _encode_cursor(cursor) != value
            or cursor.domain != "news_stories"
            or cursor.workspace_id != access_fence.workspace_id
            or cursor.actor_user_id != access_fence.user_id
            or cursor.membership_revision != access_fence.membership_revision
            or cursor.configuration_revision != access_fence.configuration_revision
            or cursor.filter_hash != _filter_hash(filters)
            or cursor.sort != "observed_at_desc_story_id_desc"
        ):
            raise ValueError
        return cursor
    except (ValueError, UnicodeDecodeError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail="Invalid story cursor") from exc


async def _resolve_source_scope(
    session: AsyncSession, requested_source_ids: tuple[UUID, ...], *, scope: Scope, multi_workspace_enabled: bool,
) -> tuple[tuple[UUID, ...], bool]:
    """Resolve explicit active sources or one bounded default-source page.

    Omitted source IDs select at most 32 active detached gadget sources. A
    continuation token pins those IDs; the partial flag records that more
    active sources existed without exposing their configuration or count.
    """
    if requested_source_ids:
        selected = await sources.get_gadget_sources(session, requested_source_ids,
            scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        return tuple(item.id for item in selected if item.status == "active"), False
    page = await sources.list_active_gadget_sources(session, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, limit=32)
    return tuple(item.id for item in page.items), page.next_cursor is not None


async def _live_story_rows(
    session: AsyncSession, source_ids: tuple[UUID, ...], as_of: datetime, *,
    scope: Scope, multi_workspace_enabled: bool,
    after: tuple[datetime, UUID] | None = None, story_id: UUID | None = None,
    evidence_after: tuple[UUID, UUID, UUID] | None = None,
    support_limit: int = 100,
    candidate_limit: int = 50,
) -> LiveStoryRows:
    """Keyset-page durable story candidates, then reproject a bounded support window.

    The keyset is ordered by latest eligible observation and story ID at the fixed
    ``as_of`` snapshot. Detail callers can constrain one story directly, avoiding a
    global prefix scan. Current Documents/Sources authority is rechecked for output.
    """
    if not source_ids:
        return LiveStoryRows([], False)
    selections = await sources.get_gadget_sources(session, source_ids,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    active = {item.id for item in selections if item.status == "active"}
    if not active:
        return LiveStoryRows([], False)
    scope_status = await documents.news_current_scope_status(
        session, tuple(sorted(active, key=str)), scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    incomplete_reasons: set[str] = set(scope_status.incomplete_reasons)
    latest_observed = func.max(NewsObservation.observed_at)
    candidate_query = select(NewsObservation.story_id, latest_observed.label("latest_observed_at")).where(
        NewsObservation.workspace_id == scope.workspace_id,
        NewsObservation.source_id.in_(active),
        NewsObservation.algorithm_version == ALGORITHM_VERSION,
        NewsObservation.created_at <= as_of,
        NewsObservation.observed_at <= as_of,
    ).group_by(NewsObservation.story_id)
    if story_id is not None:
        candidate_query = candidate_query.where(NewsObservation.story_id == story_id)
    if story_id is not None and evidence_after is not None:
        candidate_query = candidate_query.where(tuple_(
            NewsObservation.source_id, NewsObservation.document_version_id, NewsObservation.chunk_id,
        ) > evidence_after)
    if after is not None and story_id is None:
        candidate_query = candidate_query.having(
            tuple_(latest_observed, NewsObservation.story_id) < tuple_(after[0], after[1])
        )
    candidate_rows = list((await session.execute(
        candidate_query.order_by(latest_observed.desc(), NewsObservation.story_id.desc()).limit(candidate_limit + 1)
    )).all())
    has_more = story_id is None and len(candidate_rows) > candidate_limit
    candidate_rows = candidate_rows[:candidate_limit]
    candidate_ids = [row.story_id for row in candidate_rows]
    if not candidate_ids:
        return LiveStoryRows([], False, incomplete_reasons)
    support_order_by = (
        (NewsObservation.source_id, NewsObservation.document_version_id, NewsObservation.chunk_id)
        if story_id is not None else
        (NewsObservation.observed_at.desc(), NewsObservation.id)
    )
    support_rank = select(
        NewsObservation.id.label("observation_id"),
        func.row_number().over(
            partition_by=NewsObservation.story_id,
            order_by=support_order_by,
        ).label("support_rank"),
    ).where(
        NewsObservation.workspace_id == scope.workspace_id,
        NewsObservation.story_id.in_(candidate_ids),
        NewsObservation.source_id.in_(active),
        NewsObservation.algorithm_version == ALGORITHM_VERSION,
        NewsObservation.created_at <= as_of,
        NewsObservation.observed_at <= as_of,
        *([tuple_(
            NewsObservation.source_id, NewsObservation.document_version_id, NewsObservation.chunk_id,
        ) > evidence_after] if story_id is not None and evidence_after is not None else []),
    ).subquery()
    # The subquery column is untyped to SQLAlchemy's stubs; the select order fixes the row shape.
    rows = cast(list[tuple[NewsStory, NewsObservation, int]], list((await session.execute(
        select(NewsStory, NewsObservation, support_rank.c.support_rank)
        .join(NewsObservation, NewsObservation.story_id == NewsStory.id)
        .join(support_rank, support_rank.c.observation_id == NewsObservation.id)
        .where(support_rank.c.support_rank <= support_limit + 1,
               NewsStory.workspace_id == scope.workspace_id, NewsObservation.workspace_id == scope.workspace_id)
        .order_by(*(
            (NewsObservation.story_id, NewsObservation.source_id,
             NewsObservation.document_version_id, NewsObservation.chunk_id)
            if story_id is not None else
            (NewsObservation.story_id, NewsObservation.observed_at.desc(), NewsObservation.id)
        ))
    )).all()))
    grouped: dict[UUID, tuple[NewsStory, list[NewsObservation], list[StoryEvidence]]] = {}
    last_evidence_key: dict[UUID, tuple[UUID, UUID, UUID]] = {}
    next_evidence = None
    for story, observation, support_order in rows:
        if support_order > support_limit:
            incomplete_reasons.add("support_scan_limit")
            next_evidence = last_evidence_key.get(story.id)
            continue
        last_evidence_key[story.id] = (
            observation.source_id, observation.document_version_id, observation.chunk_id,
        )
        current = await documents.get_news_document_projection(
        session, observation.document_id, expected_source_generation=observation.source_generation,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        if current is None or current.document_version_id != observation.document_version_id:
            if current is None and await documents.news_projection_scope_unavailable(
                session, observation.document_id, observation.source_generation,
                scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            ):
                incomplete_reasons.add("scope_unavailable")
            continue
        chunk = next((item for item in current.chunks if item.id == observation.chunk_id), None)
        if chunk is None:
            continue
        selections_by_id = {item.id: item for item in selections}
        source = selections_by_id.get(current.source_id)
        if source is None or source.status != "active":
            continue
        entry = StoryEvidence(
            document_id=current.document_id, document_version_id=current.document_version_id,
            chunk_id=chunk.id, source_id=current.source_id, source_name=current.source_name,
            source_type=current.source_type, provider=current.provider, url=_canonical_url(current.canonical_url),
            title=current.title, excerpt=chunk.content[:1000], observed_at=current.observed_at,
            published_at=current.published_at,
        )
        existing = grouped.setdefault(story.id, (story, [], []))
        existing[1].append(observation)
        existing[2].append(entry)
    next_candidate = None
    if has_more and candidate_rows and story_id is None:
        last = candidate_rows[-1]
        next_candidate = (last.latest_observed_at, last.story_id)
        incomplete_reasons.add("candidate_scan_limit")
    candidate_keys = {row.story_id: (row.latest_observed_at, row.story_id) for row in candidate_rows}
    return LiveStoryRows(
        list(grouped.values()), has_more, incomplete_reasons, next_candidate,
        candidate_keys, next_evidence,
    )


async def _current_entity_ids(
    session: AsyncSession, observations: list[NewsObservation], *, scope: Scope, multi_workspace_enabled: bool,
) -> tuple[set[str], str | None]:
    """Resolve current entity IDs, returning a reason when a bounded scan is incomplete.

    Documents and Entities permission/deletion failures retain their normal behavior;
    chunk or membership overflow degrades this optional filter without inventing an
    empty match set. The reason is one of the fixed UI-safe codes or None.
    """
    output: set[str] = set()
    incomplete_reason = None
    for observation in observations[:100]:
        current = await documents.get_news_document_projection(
            session, observation.document_id, expected_source_generation=observation.source_generation,
            scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        if current is None or current.document_version_id != observation.document_version_id:
            continue
        if current.chunks_truncated:
            output.clear()
            incomplete_reason = "chunk_limit"
            break
        try:
            refs = await entities.list_version_membership_refs(
                session, current.document_version_id, [item.id for item in current.chunks],
                scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
        except ValueError:
            output.clear()
            incomplete_reason = "entity_membership_limit"
            break
        output.update(str(item.entity_id) for item in refs)
    return output, incomplete_reason


async def list_stories(
    session: AsyncSession, filters: StoryFilter, *, scope: Scope, multi_workspace_enabled: bool,
) -> StoryPage:
    """Return a bounded keyset page whose titles and counts use live authorized supports only."""
    access_fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if filters.date_from and filters.date_to and filters.date_from >= filters.date_to:
        raise HTTPException(status_code=422, detail="date_from must be earlier than date_to")
    as_of = datetime.now(UTC)
    cursor = _decode_cursor(filters.cursor, access_fence, filters) if filters.cursor else None
    if isinstance(scope, WorkspaceContext) and scope.role != "owner":
        return await _member_list_stories(session, filters, scope, access_fence, cursor, as_of)
    if cursor:
        as_of = cursor.as_of
        source_ids = tuple(cursor.resolved_source_ids)
        source_selection_incomplete = cursor.source_selection_incomplete
        after = (cursor.after_observed_at, cursor.after_story_id)
    else:
        source_ids, source_selection_incomplete = await _resolve_source_scope(
            session, tuple(filters.source_ids), scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        after = None
    live = await _live_story_rows(session, source_ids, as_of, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, after=after)
    filtered = []
    page_incomplete_reasons = set(live.incomplete_reasons)
    if source_selection_incomplete:
        page_incomplete_reasons.add("source_selection_limit")
    query_tokens = _tokens(filters.q or "")
    profile = None
    if filters.topic_id:
        profile = await topics.get_topic(session, filters.topic_id, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled)
        if not profile.is_active:
            return StoryPage(items=[], next_cursor=None, as_of=as_of)
    for story, observations, evidence in live:
        story_incomplete_reasons = {
            reason for observation in observations if observation.incomplete_reason
            for reason in observation.incomplete_reason.split(",")
        }
        if (filters.date_from or filters.date_to) and not any(
            (filters.date_from is None or item.observed_at >= filters.date_from)
            and (filters.date_to is None or item.observed_at < filters.date_to)
            for item in evidence
        ):
            continue
        current_entities: set[str] | None = None
        if filters.entity_id or profile:
            current_entities, membership_incomplete = await _current_entity_ids(session, observations,
                scope=scope, multi_workspace_enabled=multi_workspace_enabled)
            if membership_incomplete:
                story_incomplete_reasons.add(membership_incomplete)
                page_incomplete_reasons.add(membership_incomplete)
        if (
            filters.entity_id and not membership_incomplete
            and str(filters.entity_id) not in (current_entities or set())
        ):
            continue
        if profile:
            profile_tokens = _tokens(" ".join([profile.name, profile.description or "", *profile.keywords]))
            lexical_hit = any(profile_tokens.intersection(_tokens(item.title + " " + item.excerpt)) for item in evidence)
            entity_hit = bool({str(value) for value in profile.entity_ids}.intersection(current_entities or set()))
            if not lexical_hit and not entity_hit and not membership_incomplete:
                continue
        if query_tokens and not any(query_tokens.issubset(_tokens(item.title + " " + item.excerpt)) for item in evidence):
            continue
        representative = max(evidence, key=lambda item: (item.published_at or item.observed_at, str(item.document_id)))
        filtered.append((story, observations, evidence, representative, story_incomplete_reasons))
    filtered.sort(key=lambda item: live.candidate_keys.get(item[0].id, (as_of, item[0].id)), reverse=True)
    page = filtered[:filters.limit + 1]
    has_more = len(page) > filters.limit
    page = page[:filters.limit]
    items = [StoryRead(
        id=story.id, title=representative.title, excerpt=representative.excerpt,
        observed_at=max(ev.observed_at for ev in evidence), source_count=len({ev.source_id for ev in evidence}),
        evidence_count=len(evidence), evidence=evidence[:100], relevance_state="unavailable",
        translation_revision=_translation_revision(story.id, representative),
        incomplete_reasons=sorted(story_reasons),
    ) for story, _observations, evidence, representative, story_reasons in page]
    from modules.news.relevance import score_relevance

    scored = []
    for item in items:
        relevance = await score_relevance(session, item, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, as_of=as_of)
        entity_reason = {
            "membership_limit": "entity_membership_limit",
            "chunk_limit": "chunk_limit",
        }.get(relevance.signals["entity"].method)
        enriched = item.model_copy(update={
            "relevance": relevance.score, "why_relevant": relevance.why_relevant,
            "relevance_signals": {name: value.model_dump() for name, value in relevance.signals.items()},
            "relevance_weights": relevance.weights, "relevance_as_of": relevance.as_of,
            "relevance_profile_revisions": relevance.profile_revisions,
            "relevance_state": "available" if all(value.available for value in relevance.signals.values()) else "partial",
            "incomplete_reasons": sorted(set(item.incomplete_reasons) | (
                {entity_reason} if entity_reason else set()
            )),
        })
        # Profiles and membership reads above can await long enough for evidence to be
        # deleted or replaced; never return a title or score after its support changes.
        still_current = True
        story_row = next((row for row in page if row[0].id == enriched.id), None)
        generation_by_document = {
            observation.document_id: observation.source_generation
            for observation in story_row[1]
        } if story_row is not None else {}
        for story_evidence in enriched.evidence:
            current = await documents.get_news_document_projection(
                session, story_evidence.document_id,
                expected_source_generation=generation_by_document.get(story_evidence.document_id),
                scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
            if (
                current is None or current.document_version_id != story_evidence.document_version_id
                or current.source_id != story_evidence.source_id or current.source_id not in source_ids
            ):
                still_current = False
                break
        if still_current:
            scored.append(enriched)
        else:
            page_incomplete_reasons.add("evidence_changed_during_read")
    next_cursor = None
    continuation_key = None
    if has_more and page:
        continuation_key = live.candidate_keys.get(page[-1][0].id)
    elif live.next_candidate is not None:
        continuation_key = live.next_candidate
    if continuation_key is not None:
        next_cursor = _encode_cursor(StoryCursor(
            domain="news_stories", workspace_id=access_fence.workspace_id,
            actor_user_id=access_fence.user_id,
            membership_revision=access_fence.membership_revision,
            configuration_revision=access_fence.configuration_revision,
            filter_hash=_filter_hash(filters), sort="observed_at_desc_story_id_desc", as_of=as_of,
            after_observed_at=continuation_key[0], after_story_id=continuation_key[1],
            resolved_source_ids=list(source_ids), source_selection_incomplete=source_selection_incomplete,
        ))
    page_reasons = sorted(page_incomplete_reasons | {
        reason for item in scored for reason in item.incomplete_reasons
    })
    return StoryPage(items=scored, next_cursor=next_cursor, as_of=as_of, truncated=live.truncated,
                     incomplete_reasons=page_reasons,
                     capability="partial" if page_reasons or any(item.relevance_state != "available" for item in scored) else "available")


DETAIL_CURSOR_DOMAIN = "news_story_detail"
DETAIL_CURSOR_SORT = "support_key_asc"
_DETAIL_CURSOR_FIELDS = {
    "v", "domain", "workspace_id", "actor_user_id", "membership_revision", "configuration_revision",
    "story_id", "source_ids", "as_of", "after", "source_selection_incomplete", "sort",
}


def _encode_detail_cursor(
    fence: AccessFence, story_id: UUID, source_ids: tuple[UUID, ...], as_of: datetime,
    after: tuple[UUID, UUID, UUID], source_selection_incomplete: bool,
) -> str:
    """Bind a Story detail evidence token to the admitted workspace, actor, revisions and snapshot."""
    raw = json.dumps({
        "v": 2, "domain": DETAIL_CURSOR_DOMAIN, "workspace_id": str(fence.workspace_id),
        "actor_user_id": str(fence.user_id), "membership_revision": fence.membership_revision,
        "configuration_revision": fence.configuration_revision, "story_id": str(story_id),
        "source_ids": sorted(str(value) for value in source_ids), "as_of": as_of.isoformat(),
        "after": [str(value) for value in after], "source_selection_incomplete": source_selection_incomplete,
        "sort": DETAIL_CURSOR_SORT,
    }, sort_keys=True, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_detail_cursor(
    value: str, fence: AccessFence, story_id: UUID, requested_source_ids: tuple[UUID, ...],
) -> tuple[tuple[UUID, ...], datetime, tuple[UUID, UUID, UUID], bool]:
    """Reject legacy (six-field), cross-workspace, stale-revision or mismatched detail tokens with HTTP 422."""
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        parts = json.loads(raw)
        if (
            not isinstance(parts, dict) or set(parts) != _DETAIL_CURSOR_FIELDS
            or parts["v"] != 2 or parts["domain"] != DETAIL_CURSOR_DOMAIN or parts["sort"] != DETAIL_CURSOR_SORT
            or parts["workspace_id"] != str(fence.workspace_id)
            or parts["actor_user_id"] != str(fence.user_id)
            or parts["membership_revision"] != fence.membership_revision
            or parts["configuration_revision"] != fence.configuration_revision
            or parts["story_id"] != str(story_id)
            or not isinstance(parts["source_ids"], list) or len(parts["source_ids"]) > 32
            or len(set(parts["source_ids"])) != len(parts["source_ids"])
            or (requested_source_ids and sorted(map(str, requested_source_ids)) != sorted(parts["source_ids"]))
            or not isinstance(parts["source_selection_incomplete"], bool)
            or not isinstance(parts["after"], list) or len(parts["after"]) != 3
            or base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=") != value
        ):
            raise ValueError
        as_of = datetime.fromisoformat(parts["as_of"])
        if as_of.utcoffset() is None:
            raise ValueError
        after = (UUID(parts["after"][0]), UUID(parts["after"][1]), UUID(parts["after"][2]))
        return tuple(UUID(item) for item in parts["source_ids"]), as_of, after, parts["source_selection_incomplete"]
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, binascii.Error) as exc:
        raise HTTPException(status_code=422, detail="Invalid story evidence cursor") from exc


async def get_story(
    session: AsyncSession, story_id: UUID, source_ids: tuple[UUID, ...], *,
    evidence_limit: int = 100, evidence_cursor: str | None = None,
    scope: Scope, multi_workspace_enabled: bool,
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
    access_fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not 1 <= evidence_limit <= 100:
        raise ValueError("Evidence page size must be between 1 and 100")
    if isinstance(scope, WorkspaceContext) and scope.role != "owner":
        return await _member_get_story(
            session, story_id, source_ids, scope, access_fence, evidence_limit, evidence_cursor,
        )
    after_key: tuple[UUID, UUID, UUID] | None = None
    as_of = datetime.now(UTC)
    source_selection_incomplete = False
    if evidence_cursor:
        source_ids, as_of, after_key, source_selection_incomplete = _decode_detail_cursor(
            evidence_cursor, access_fence, story_id, source_ids,
        )
    else:
        source_ids, source_selection_incomplete = await _resolve_source_scope(session, source_ids,
            scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    base_live = await _live_story_rows(session, source_ids, as_of, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, story_id=story_id)
    match = next((item for item in base_live if item[0].id == story_id), None)
    page_live = await _live_story_rows(
        session, source_ids, as_of, scope=scope, multi_workspace_enabled=multi_workspace_enabled, story_id=story_id,
        evidence_after=after_key, support_limit=evidence_limit,
    )
    page_match = next((item for item in page_live if item[0].id == story_id), None)
    evidence = page_match[2] if page_match else []
    evidence_page = evidence[:evidence_limit]
    next_evidence_cursor = None
    if page_live.next_evidence is not None:
        next_evidence_cursor = _encode_detail_cursor(
            access_fence, story_id, source_ids, as_of, page_live.next_evidence, source_selection_incomplete,
        )
    # A stale support prefix can hide later current evidence. Continue that
    # bounded scan without borrowing a title from stale observations.
    if match is None and page_match is None:
        if next_evidence_cursor is None:
            return None
        return StoryDetail(
            story=None, evidence_cursor=next_evidence_cursor,
            incomplete_reasons=sorted(page_live.incomplete_reasons | {"support_scan_limit", "stale_support_omitted"}),
        )
    story_row = match or page_match
    assert story_row is not None
    story, observations, base_evidence = story_row
    title_evidence = base_evidence or (page_match[2] if page_match else [])
    representative = max(title_evidence, key=lambda item: (item.published_at or item.observed_at, str(item.document_id)))
    incomplete_reasons = base_live.incomplete_reasons | page_live.incomplete_reasons | {
        reason for observation in observations if observation.incomplete_reason
        for reason in observation.incomplete_reason.split(",")
    }
    if source_selection_incomplete:
        incomplete_reasons.add("source_selection_limit")
    # The page query may await scope and source calls; recheck the exact revisions
    # used in the returned title immediately before building the detached response.
    page_observations = page_match[1] if page_match else []
    generation_by_document = {
        observation.document_id: observation.source_generation
        for observation in [*observations, *page_observations]
    }
    fence_evidence = {
        (item.document_id, item.document_version_id): item
        for item in [*base_evidence, *evidence_page]
    }.values()
    for item in fence_evidence:
        current = await documents.get_news_document_projection(
            session, item.document_id,
            expected_source_generation=generation_by_document.get(item.document_id),
            scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        if (
            current is None or current.document_version_id != item.document_version_id
            or current.source_id != item.source_id or current.source_id not in source_ids
        ):
            if next_evidence_cursor is None:
                return None
            return StoryDetail(
                story=None, evidence_cursor=next_evidence_cursor,
                incomplete_reasons=sorted(set(incomplete_reasons) | {"evidence_changed_during_read", "stale_support_omitted"}),
            )
    return StoryDetail(story=StoryRead(
        id=story.id, title=representative.title, excerpt=representative.excerpt,
        observed_at=max(item.observed_at for item in title_evidence),
        source_count=len({item.source_id for item in title_evidence}), evidence_count=len(base_evidence or title_evidence),
        evidence=evidence_page, relevance_state="unavailable",
        translation_revision=_translation_revision(story.id, representative),
        incomplete_reasons=sorted(incomplete_reasons),
    ), evidence_cursor=next_evidence_cursor, incomplete_reasons=sorted(incomplete_reasons))


# --- Member projection: grant-first evidence; no topics, relevance, entities or trends -------------

_LIVE_STATUS = ("ready", "succeeded")


def _translation_revision(story_id: UUID, representative: StoryEvidence) -> str:
    """Hash the story, its representative version and its title/excerpt text (32 hex chars)."""
    text_hash = hashlib.sha256(f"{representative.title}\0{representative.excerpt}".encode()).hexdigest()
    raw = f"{story_id}:{representative.document_version_id}:{text_hash}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _member_obs(scope: WorkspaceContext, as_of: datetime, source_ids: tuple[UUID, ...], *columns: Any) -> Select[Any]:
    """Select live observations whose document is granted to the member; the grant filter precedes any LIMIT."""
    query = (
        select(*columns).select_from(NewsObservation)
        .join(Document, and_(
            Document.id == NewsObservation.document_id, Document.workspace_id == scope.workspace_id,
            Document.current_version == NewsObservation.version_number,
            Document.extraction_status.in_(_LIVE_STATUS),
        ))
        .join(Source, and_(
            Source.id == NewsObservation.source_id, Source.workspace_id == scope.workspace_id,
            Source.status == "active", Source.generation == NewsObservation.source_generation,
        ))
        .where(
            NewsObservation.workspace_id == scope.workspace_id,
            NewsObservation.algorithm_version == ALGORITHM_VERSION,
            NewsObservation.document_id.in_(workspaces.granted_resource_ids(scope=scope, kind="document")),
            NewsObservation.created_at <= as_of, NewsObservation.observed_at <= as_of,
        )
    )
    return query.where(NewsObservation.source_id.in_(source_ids)) if source_ids else query


def _member_evidence(observation: NewsObservation, source: Source) -> StoryEvidence:
    """Detach one authorized observation; title and excerpt never come from hidden rows."""
    return StoryEvidence(
        document_id=observation.document_id, document_version_id=observation.document_version_id,
        chunk_id=observation.chunk_id, source_id=observation.source_id, source_name=source.name,
        source_type=source.type, provider=observation.provider, url=_canonical_url(observation.canonical_url),
        title=observation.title, excerpt=observation.excerpt[:1000], observed_at=observation.observed_at,
        published_at=observation.published_at,
    )


def _representative(evidence: list[StoryEvidence]) -> StoryEvidence:
    """Pick the newest published/observed evidence, ties by document ID."""
    return max(evidence, key=lambda item: (item.published_at or item.observed_at, str(item.document_id)))


async def _member_list_stories(
    session: AsyncSession, filters: StoryFilter, scope: WorkspaceContext, fence: AccessFence,
    cursor: StoryCursor | None, as_of: datetime,
) -> StoryPage:
    """Page stories over the member's authorized evidence only; zero authorized evidence hides the story."""
    if filters.topic_id or filters.entity_id:
        raise HTTPException(status_code=403, detail="Workspace owner required")
    if cursor:
        as_of = cursor.as_of
    source_ids = tuple(cursor.resolved_source_ids) if cursor else tuple(filters.source_ids)
    latest = func.max(NewsObservation.observed_at)
    query = _member_obs(scope, as_of, source_ids, NewsObservation.story_id, latest.label("latest")).group_by(
        NewsObservation.story_id)
    if cursor:
        query = query.having(tuple_(latest, NewsObservation.story_id) < tuple_(
            cursor.after_observed_at, cursor.after_story_id))
    candidates = list((await session.execute(
        query.order_by(latest.desc(), NewsObservation.story_id.desc()).limit(filters.limit + 1)
    )).all())
    has_more = len(candidates) > filters.limit
    candidates = candidates[:filters.limit]
    ids = [row.story_id for row in candidates]
    evidence: dict[UUID, list[StoryEvidence]] = {}
    counts: dict[UUID, tuple[int, int]] = {}
    if ids:
        rank = _member_obs(
            scope, as_of, source_ids, NewsObservation.id.label("oid"),
            func.row_number().over(
                partition_by=NewsObservation.story_id,
                order_by=(NewsObservation.observed_at.desc(), NewsObservation.id),
            ).label("rank"),
        ).where(NewsObservation.story_id.in_(ids)).subquery()
        rows = (await session.execute(
            select(NewsObservation, Source)
            .join(rank, rank.c.oid == NewsObservation.id)
            .join(Source, and_(Source.id == NewsObservation.source_id, Source.workspace_id == scope.workspace_id))
            .where(rank.c.rank <= 100)
            .order_by(NewsObservation.story_id, NewsObservation.observed_at.desc(), NewsObservation.id)
        )).all()
        for observation, source in rows:
            evidence.setdefault(observation.story_id, []).append(_member_evidence(observation, source))
        counts = {row.story_id: (row.evidence_count, row.source_count) for row in (await session.execute(
            _member_obs(
                scope, as_of, source_ids, NewsObservation.story_id,
                func.count().label("evidence_count"),
                func.count(NewsObservation.source_id.distinct()).label("source_count"),
            ).where(NewsObservation.story_id.in_(ids)).group_by(NewsObservation.story_id)
        )).all()}
    query_tokens = _tokens(filters.q or "")
    items = []
    for row in candidates:
        story_evidence = evidence.get(row.story_id)
        if not story_evidence:
            continue
        if (filters.date_from or filters.date_to) and not any(
            (filters.date_from is None or item.observed_at >= filters.date_from)
            and (filters.date_to is None or item.observed_at < filters.date_to) for item in story_evidence
        ):
            continue
        if query_tokens and not any(
            query_tokens.issubset(_tokens(item.title + " " + item.excerpt)) for item in story_evidence
        ):
            continue
        rep = _representative(story_evidence)
        evidence_count, source_count = counts.get(
            row.story_id, (len(story_evidence), len({item.source_id for item in story_evidence})))
        items.append(StoryRead(
            id=row.story_id, title=rep.title, excerpt=rep.excerpt, observed_at=row.latest,
            source_count=source_count, evidence_count=evidence_count, evidence=story_evidence,
            relevance_state="unavailable", translation_revision=_translation_revision(row.story_id, rep),
        ))
    next_cursor = None
    if has_more and candidates:
        last = candidates[-1]
        next_cursor = _encode_cursor(StoryCursor(
            domain="news_stories", workspace_id=fence.workspace_id, actor_user_id=fence.user_id,
            membership_revision=fence.membership_revision, configuration_revision=fence.configuration_revision,
            filter_hash=_filter_hash(filters), sort="observed_at_desc_story_id_desc", as_of=as_of,
            after_observed_at=last.latest, after_story_id=last.story_id, resolved_source_ids=list(source_ids),
        ))
    return StoryPage(items=items, next_cursor=next_cursor, as_of=as_of, capability="partial")


async def _member_get_story(
    session: AsyncSession, story_id: UUID, source_ids: tuple[UUID, ...], scope: WorkspaceContext,
    fence: AccessFence, evidence_limit: int, evidence_cursor: str | None,
) -> StoryDetail | None:
    """Return one story's authorized evidence page; a story with no authorized evidence is None (404)."""
    as_of, after = datetime.now(UTC), None
    if evidence_cursor:
        source_ids, as_of, after, _incomplete = _decode_detail_cursor(evidence_cursor, fence, story_id, source_ids)
    own = NewsObservation.story_id == story_id
    total = (await session.execute(_member_obs(
        scope, as_of, source_ids, func.count().label("n"),
        func.count(NewsObservation.source_id.distinct()).label("s"),
        func.max(NewsObservation.observed_at).label("latest"),
    ).where(own))).one()
    if not total.n:
        return None
    rep_row = (await session.execute(
        _member_obs(scope, as_of, source_ids, NewsObservation, Source).where(own).order_by(
            func.coalesce(NewsObservation.published_at, NewsObservation.observed_at).desc(),
            NewsObservation.document_id.desc(),
        ).limit(1)
    )).one()
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
            fence, story_id, source_ids, as_of, (last.source_id, last.document_version_id, last.chunk_id), False,
        )
    return StoryDetail(story=StoryRead(
        id=story_id, title=rep.title, excerpt=rep.excerpt, observed_at=total.latest,
        source_count=total.s, evidence_count=total.n, evidence=[_member_evidence(o, s) for o, s in page],
        relevance_state="unavailable", translation_revision=_translation_revision(story_id, rep),
    ), evidence_cursor=next_cursor)


async def read_story_translation_input(
    session: AsyncSession, *, scope: WorkspaceContext, story_id: UUID, multi_workspace_enabled: bool,
) -> TranslationInput | None:
    """Return the translatable title/excerpt plus a visibility hash, or None when not visible or incomplete."""
    detail = await get_story(
        session, story_id, (), evidence_limit=100, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    story = detail.story if detail else None
    # ponytail: a story with >100 evidence rows is not translatable; page the hash if that matters.
    if detail is None or story is None or detail.evidence_cursor is not None or not story.evidence:
        return None
    versions = {item.document_version_id for item in story.evidence}
    rows = (await session.execute(
        select(
            NewsObservation.document_id, NewsObservation.document_version_id, NewsObservation.source_id,
            NewsObservation.source_generation, NewsObservation.local_only,
        ).where(
            NewsObservation.workspace_id == scope.workspace_id, NewsObservation.story_id == story_id,
            NewsObservation.algorithm_version == ALGORITHM_VERSION,
            NewsObservation.document_version_id.in_(versions),
        )
    )).all()
    grant_revisions: dict[UUID, int] = {}
    if scope.role != "owner":
        grants = await workspaces.read_resource_grants(
            session, scope=scope, kind="document",
            resource_ids=tuple(sorted({row.document_id for row in rows}, key=str)),
        )
        grant_revisions = {grant.resource_id: grant.share_revision for grant in grants}
    tuples = sorted(
        (str(row.document_id), str(row.document_version_id), str(row.source_id), row.source_generation,
         grant_revisions.get(row.document_id, 0), scope.membership_revision, bool(row.local_only))
        for row in rows
    )
    return TranslationInput(
        workspace_id=scope.workspace_id, actor_user_id=scope.user_id, resource_type="news_story",
        resource_id=story_id, resource_revision=story.translation_revision,
        fields={"title": story.title, "excerpt": story.excerpt},
        visibility_hash=hashlib.sha256(json.dumps(tuples, separators=(",", ":")).encode()).hexdigest(),
        source_ids=tuple(sorted({row.source_id for row in rows}, key=str)), local_only=any(t[-1] for t in tuples),
    )
