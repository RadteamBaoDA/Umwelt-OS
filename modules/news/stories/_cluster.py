"""Observation-to-story clustering: identity-key lookup, fuzzy candidate matching and registration."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.schemas import Scope
from modules.connectors.public import get_provider_spec
from modules.knowledge.documents import public as documents
from modules.knowledge.entities import public as entities
from modules.news.models import NewsObservation, NewsStory, NewsStoryIdentity
from modules.news.stories._common import (
    ALGORITHM_VERSION,
    FUZZY_THRESHOLD,
    FUZZY_WINDOW,
    MAX_CANDIDATES,
    _attribution,
    _canonical_url,
    _identity_keys,
    _initial_signal_provenance,
    _plain_excerpt,
)
from modules.search import public as search


async def _lock_and_find_identity_story(
    session: AsyncSession,
    identities: list[tuple[str, str]],
    *,
    scope: Scope,
) -> tuple[str, str, NewsStory | None]:
    """Serialize equal identities, then return the first known identity's locked story.

    Takes one transaction advisory lock per identity key in sorted key order (deadlock-free
    across concurrent writers), then probes identities in canonical-URL-first order and
    row-locks the first matching story (``FOR UPDATE``). Returns ``(kind, key, story)``;
    without a match the kind/key are the primary identity and the story is ``None``.
    """
    kind, key = identities[0]
    for _identity_kind, identity_key in sorted(identities, key=lambda item: item[1]):
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
            {"identity": f"news:{identity_key}"},
        )
    story = None
    for identity_kind, identity_key in identities:
        identity = await session.scalar(
            select(NewsStoryIdentity).where(
                NewsStoryIdentity.identity_key == identity_key,
                NewsStoryIdentity.workspace_id == scope.workspace_id,
                NewsStoryIdentity.algorithm_version == ALGORITHM_VERSION,
            )
        )
        if identity is not None:
            kind, key = identity_kind, identity_key
            story = await session.scalar(
                select(NewsStory)
                .where(
                    NewsStory.id == identity.story_id,
                    NewsStory.workspace_id == scope.workspace_id,
                )
                .with_for_update()
            )
            break
    return kind, key, story


async def _register_story_identities(
    session: AsyncSession,
    story: NewsStory,
    identities: list[tuple[str, str]],
    *,
    scope: Scope,
) -> None:
    """Attach each not-yet-known identity key to ``story`` (adds rows; the caller flushes/commits)."""
    for identity_kind, identity_key in identities:
        if (
            await session.scalar(
                select(NewsStoryIdentity.id).where(
                    NewsStoryIdentity.identity_key == identity_key,
                    NewsStoryIdentity.workspace_id == scope.workspace_id,
                    NewsStoryIdentity.algorithm_version == ALGORITHM_VERSION,
                )
            )
            is None
        ):
            session.add(
                NewsStoryIdentity(
                    workspace_id=scope.workspace_id,
                    story_id=story.id,
                    identity_key=identity_key,
                    identity_kind=identity_kind,
                    algorithm_version=ALGORITHM_VERSION,
                )
            )


async def cluster_observation(
    session: AsyncSession,
    *,
    document_id: UUID,
    expected_source_generation: int,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> UUID | None:
    """Persist one ready version's exact identity or conservative fuzzy grouping decision.

    The caller holds sorted source then document locks and owns the same durable
    outbox transaction. Clustering consumes detached owner projections only;
    equal identities serialize on a PostgreSQL transaction advisory lock, while
    unique version/generation/algorithm identity makes replay idempotent.
    """
    projection = await documents.get_news_document_projection(
        session,
        document_id,
        expected_source_generation=expected_source_generation,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if projection is None:
        return None
    prior = await session.scalar(
        select(NewsObservation).where(
            NewsObservation.workspace_id == scope.workspace_id,
            NewsObservation.document_version_id == projection.document_version_id,
            NewsObservation.source_generation == projection.current_source_generation,
            NewsObservation.algorithm_version == ALGORITHM_VERSION,
        )
    )
    if prior is not None:
        return prior.story_id
    # Measurements (weather, ISS position, prices) are observations, not articles: keep them out of stories.
    spec = get_provider_spec(projection.provider)
    if (spec is not None and spec.data_kind == "measurement") or (
        projection.provider_metadata is not None
        and projection.provider_metadata.world_data is not None
    ):
        return None
    representative = projection.chunks[0]
    incomplete_reasons: list[str] = []
    if projection.chunks_truncated:
        incomplete_reasons.append("chunk_limit")
    try:
        memberships = await entities.list_version_membership_refs(
            session,
            projection.document_version_id,
            [chunk.id for chunk in projection.chunks],
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        entity_ids = sorted({str(item.entity_id) for item in memberships})
    except ValueError:
        entity_ids = []
        incomplete_reasons.append("entity_membership_limit")
    title = " ".join(projection.title.split())[:500] or "Untitled"
    excerpt = _plain_excerpt(title, representative.content)
    observed_at = projection.observed_at.astimezone(UTC)
    identities = _identity_keys(projection.canonical_url, projection.content_hash)
    kind, key, story = await _lock_and_find_identity_story(session, identities, scope=scope)
    match_method = kind
    match_evidence: dict[str, object] = {
        "identity_kind": kind,
        "identity_key": key,
        "algorithm_version": ALGORITHM_VERSION,
    }
    if story is None:
        fuzzy = await _fuzzy_candidate(
            session,
            projection=projection,
            current_entity_ids=set(entity_ids),
            observed_at=observed_at,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if fuzzy is not None:
            story, fuzzy_evidence = fuzzy
            match_method = "embedding_entity_time"
            match_evidence.update(fuzzy_evidence)
    attribution = _attribution(projection.provider_metadata)
    if attribution:
        match_evidence["attribution"] = attribution
    if story is None:
        story = NewsStory(
            workspace_id=scope.workspace_id,
            identity_key=key,
            identity_kind=kind,
            algorithm_version=ALGORITHM_VERSION,
        )
        session.add(story)
        await session.flush()
    await _register_story_identities(session, story, identities, scope=scope)
    session.add(
        NewsObservation(
            workspace_id=scope.workspace_id,
            story_id=story.id,
            document_id=projection.document_id,
            document_version_id=projection.document_version_id,
            chunk_id=representative.id,
            source_id=projection.source_id,
            source_generation=projection.current_source_generation,
            version_number=projection.version_number,
            canonical_url=_canonical_url(projection.canonical_url),
            content_hash=projection.content_hash,
            title=title,
            excerpt=excerpt,
            provider=projection.provider,
            observed_at=observed_at,
            published_at=projection.published_at,
            local_only=projection.local_only,
            membership_entity_ids=entity_ids,
            match_method=match_method,
            match_evidence=match_evidence,
            incomplete_reason=",".join(incomplete_reasons) or None,
            recorded_signals=_initial_signal_provenance(),
        )
    )
    await session.flush()
    return story.id


async def _live_fuzzy_candidates(
    session: AsyncSession,
    candidate_rows: list[Any],
    *,
    current_entity_ids: set[str],
    observed_at: datetime,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[tuple[NewsObservation, NewsStory, list[str]]]:
    """Keep only candidate ``(observation, story)`` rows whose evidence is still current and entity-overlapping.

    A row survives when it is inside the fuzzy time window, its document projection still carries the observed
    version and chunk (read through the owner contract at the observation's source generation), and it shares at
    least one canonical entity with the new observation. Returns ``(observation, story, sorted_overlap_ids)`` in
    input order; issues one projection read and one membership read per surviving-window row.
    """
    live_candidates: list[tuple[NewsObservation, NewsStory, list[str]]] = []
    for observation, story in candidate_rows:
        if (
            abs((observation.observed_at - observed_at).total_seconds())
            > FUZZY_WINDOW.total_seconds()
        ):
            continue
        candidate_projection = await documents.get_news_document_projection(
            session,
            observation.document_id,
            expected_source_generation=observation.source_generation,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if (
            candidate_projection is None
            or candidate_projection.document_version_id != observation.document_version_id
            or observation.chunk_id not in {item.id for item in candidate_projection.chunks}
        ):
            continue
        refs = await entities.list_version_membership_refs(
            session,
            observation.document_version_id,
            [observation.chunk_id],
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        overlap = sorted(current_entity_ids.intersection(str(item.entity_id) for item in refs))
        if overlap:
            live_candidates.append((observation, story, overlap))
    return live_candidates


async def _fuzzy_candidate(
    session: AsyncSession,
    *,
    projection: documents.NewsDocumentProjection,
    current_entity_ids: set[str],
    observed_at: datetime,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> tuple[NewsStory, dict[str, object]] | None:
    """Select one high-confidence same-generation vector/entity/time candidate from at most 100 rows."""
    if projection.local_only or not current_entity_ids:
        return None
    candidate_rows = list(
        (
            await session.execute(
                select(NewsObservation, NewsStory)
                .join(NewsStory, NewsStory.id == NewsObservation.story_id)
                .where(
                    NewsObservation.workspace_id == scope.workspace_id,
                    NewsStory.workspace_id == scope.workspace_id,
                    NewsObservation.source_id != projection.source_id,
                    NewsObservation.observed_at >= observed_at - FUZZY_WINDOW,
                    NewsObservation.observed_at <= observed_at + FUZZY_WINDOW,
                    NewsObservation.local_only.is_(False),
                )
                .order_by(NewsObservation.observed_at.desc(), NewsObservation.id)
                .limit(MAX_CANDIDATES + 1)
            )
        ).all()
    )
    if len(candidate_rows) > MAX_CANDIDATES:
        return None
    scored_rows = await _live_fuzzy_candidates(
        session,
        candidate_rows,
        current_entity_ids=current_entity_ids,
        observed_at=observed_at,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if not scored_rows:
        return None
    similarity = await search.compare_news_evidence_embeddings(
        session,
        projection.chunks[0].id,
        tuple(item.chunk_id for item, _story, _entity_overlap in scored_rows),
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    scores = {item.right_chunk_id: item for item in similarity.items}
    ranked = sorted(
        (
            (
                scores[observation.chunk_id].cosine_similarity,
                str(story.id),
                story,
                observation,
                overlap,
                scores[observation.chunk_id],
            )
            for observation, story, overlap in scored_rows
            if observation.chunk_id in scores
        ),
        key=lambda item: (-item[0], item[1], str(item[3].id)),
    )
    if not ranked or ranked[0][0] < FUZZY_THRESHOLD:
        return None
    score, _story_key, story, observation, entity_overlap, vector = ranked[0]
    return story, {
        "cosine_similarity": score,
        "cosine_threshold": FUZZY_THRESHOLD,
        "time_window_hours": 72,
        "shared_canonical_entity_ids": entity_overlap,
        "candidate_observation_id": str(observation.id),
        "candidate_chunk_id": str(observation.chunk_id),
        "generation_id": str(vector.generation_id),
        "model_id": vector.model_id,
        "model_version": vector.model_version,
        "response_model_id": vector.response_model_id,
        "dimensions": vector.dimensions,
        "gateway_identity": vector.gateway_identity,
        "search_capability": similarity.capability,
    }
