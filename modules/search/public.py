import base64
import binascii
import hashlib
import json
import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import Select, and_, false, func, or_, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from core.config import Settings
from core.model_gateway.client import ModelGatewayError, PrivacyPolicyDenied
from core.model_gateway.policy import may_send
from core.model_gateway.schemas import AIExecutionConfig, ModelMapping, RequestPolicy
from core.tools.schemas import ToolDestination, ToolOutputFence
from modules.goals.schemas import GoalFilter, GoalPage
from modules.knowledge.documents.models import Document, DocumentChunk, DocumentVersion
from modules.search.indexing import configured_embedding, embedding_values, gateway
from modules.search.models import IndexGeneration, SearchIndexItem
from modules.search.schemas import (
    Citation,
    SearchHit,
    SearchIndexStatus,
    SearchRequest,
    SearchResponse,
    SearchSource,
)
from modules.sources.models import Source
from modules.tasks.schemas import TaskFilter, TaskPage

MAX_CANDIDATES = 500
MAX_RANKED_CANDIDATES = MAX_CANDIDATES * 2
FALLBACK_WARNING = "Semantic search unavailable"


@dataclass(frozen=True)
class NewsSimilarityRead:
    """Return stored-vector cosine similarity plus its active generation identity."""
    left_chunk_id: UUID
    right_chunk_id: UUID
    cosine_similarity: float
    generation_id: UUID
    model_id: str
    model_version: str | None
    response_model_id: str | None
    dimensions: int
    gateway_identity: str | None


@dataclass(frozen=True)
class NewsSimilarityResult:
    """Distinguish usable bounded matches from missing or incompatible index capability."""
    items: tuple[NewsSimilarityRead, ...]
    capability: str


async def compare_news_evidence_embeddings(
    session: AsyncSession, incoming_chunk_id: UUID, candidate_chunk_ids: tuple[UUID, ...],
) -> NewsSimilarityResult:
    """Compare only active stored embeddings for current visible news chunks.

    No gateway call or embedding generation occurs here. Both sides must be
    succeeded in the same active generation and current ready documents from
    active remotely indexable sources. At most 100 candidates are inspected.
    """
    if len(candidate_chunk_ids) > 100 or len(set(candidate_chunk_ids)) != len(candidate_chunk_ids):
        raise ValueError("News similarity accepts at most 100 unique candidates")
    if not candidate_chunk_ids:
        return NewsSimilarityResult(items=(), capability="no_candidates")
    generation = await session.scalar(select(IndexGeneration).where(IndexGeneration.status == "active"))
    if generation is None or generation.dimensions is None:
        return NewsSimilarityResult(items=(), capability="index_unavailable")
    incoming = aliased(SearchIndexItem, name="news_incoming_index_item")
    candidate = aliased(SearchIndexItem, name="news_candidate_index_item")
    left_source = aliased(Source, name="news_incoming_source")
    right_source = aliased(Source, name="news_candidate_source")
    left_doc = aliased(Document, name="news_incoming_document")
    right_doc = aliased(Document, name="news_candidate_document")
    left_version = aliased(DocumentVersion, name="news_incoming_version")
    right_version = aliased(DocumentVersion, name="news_candidate_version")
    left_chunk = aliased(DocumentChunk, name="news_incoming_chunk")
    right_chunk = aliased(DocumentChunk, name="news_candidate_chunk")
    stmt = (
        select(
            incoming.chunk_id, candidate.chunk_id,
            (1.0 - incoming.embedding.op("<=>")(candidate.embedding)).label("similarity"),
            IndexGeneration.id, IndexGeneration.model_id, IndexGeneration.model_version,
            IndexGeneration.response_model_id, IndexGeneration.dimensions, IndexGeneration.gateway_identity,
        )
        .join(IndexGeneration, IndexGeneration.id == incoming.generation_id)
        .join(candidate, (candidate.generation_id == incoming.generation_id) & (candidate.status == "succeeded"))
        .join(left_chunk, left_chunk.id == incoming.chunk_id)
        .join(left_version, left_version.id == left_chunk.document_version_id)
        .join(left_doc, left_doc.id == left_version.document_id)
        .join(left_source, left_source.id == left_doc.source_id)
        .join(right_chunk, right_chunk.id == candidate.chunk_id)
        .join(right_version, right_version.id == right_chunk.document_version_id)
        .join(right_doc, right_doc.id == right_version.document_id)
        .join(right_source, right_source.id == right_doc.source_id)
        .where(
            incoming.chunk_id == incoming_chunk_id, incoming.status == "succeeded",
            incoming.generation_id == generation.id, IndexGeneration.status == "active",
            candidate.chunk_id.in_(candidate_chunk_ids),
            left_doc.current_version == left_version.version_number,
            right_doc.current_version == right_version.version_number,
            left_doc.extraction_status.in_(("ready", "succeeded")),
            right_doc.extraction_status.in_(("ready", "succeeded")),
            left_source.status == "active", right_source.status == "active",
            left_source.local_only.is_(False), right_source.local_only.is_(False),
        )
    )
    rows: list[Any] = list((await session.execute(stmt.limit(101))).all())
    if len(rows) > 100:
        return NewsSimilarityResult(items=(), capability="candidate_limit_exceeded")
    if not rows:
        return NewsSimilarityResult(items=(), capability="embeddings_unavailable")
    values = []
    for left_id, right_id, score, generation_id, model, version, response, dimensions, gateway_identity in rows:
        value = float(score)
        if not math.isfinite(value) or value < -1.00001 or value > 1.00001:
            return NewsSimilarityResult(items=(), capability="invalid_vector_result")
        values.append(NewsSimilarityRead(
            left_chunk_id=left_id, right_chunk_id=right_id,
            cosine_similarity=max(-1.0, min(1.0, value)), generation_id=generation_id,
            model_id=model, model_version=version, response_model_id=response,
            dimensions=dimensions, gateway_identity=gateway_identity,
        ))
    return NewsSimilarityResult(items=tuple(values), capability="available")


def _cursor_scope(
    request: SearchRequest, destination: ToolDestination = ToolDestination.LOCAL,
    source_generation_fences: dict[UUID, int] | None = None,
) -> str:
    """Bind cursors to request, destination and exact source generations."""
    content = request.model_dump(exclude={"cursor", "limit"}, mode="json")
    content["destination"] = destination.value
    content["source_generations"] = sorted(
        ((str(source_id), generation) for source_id, generation in (source_generation_fences or {}).items())
    )
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:16]


def _offset(
    request: SearchRequest, destination: ToolDestination = ToolDestination.LOCAL,
    source_generation_fences: dict[UUID, int] | None = None,
) -> int:
    """Decode a canonical cursor bound to request, destination class and supported offsets."""
    if request.cursor is None:
        return 0
    try:
        raw = base64.urlsafe_b64decode(request.cursor + "=" * (-len(request.cursor) % 4)).decode()
        scope, position = raw.split(":", 1)
        offset = int(position)
        if scope != _cursor_scope(request, destination, source_generation_fences) or offset < 0 or offset > MAX_RANKED_CANDIDATES or _encode_cursor(request, offset, destination, source_generation_fences) != request.cursor:
            raise ValueError
        return offset
    except (ValueError, UnicodeDecodeError, IndexError, binascii.Error) as exc:
        raise HTTPException(status_code=422, detail="Invalid search cursor") from exc


def _encode_cursor(
    request: SearchRequest, offset: int, destination: ToolDestination = ToolDestination.LOCAL,
    source_generation_fences: dict[UUID, int] | None = None,
) -> str:
    """Encode request/destination scope and ranked offset as unpadded URL-safe base64."""
    return base64.urlsafe_b64encode(
        f"{_cursor_scope(request, destination, source_generation_fences)}:{offset}".encode()
    ).decode().rstrip("=")


def _filters(
    statement: Select[Any], request: SearchRequest, destination: ToolDestination = ToolDestination.LOCAL,
    source_generation_fences: dict[UUID, int] | None = None,
) -> Select[Any]:
    """Apply privacy, source-generation, type and effective-date predicates before ranking."""
    filters = request.filters
    if destination != ToolDestination.LOCAL:
        statement = statement.where(Source.local_only.is_(False))
    if source_generation_fences is not None:
        generation_conditions = [
            and_(Document.source_id == source_id, Source.generation == generation)
            for source_id, generation in source_generation_fences.items()
        ]
        statement = statement.where(or_(*generation_conditions) if generation_conditions else false())
    if filters.source_ids:
        statement = statement.where(Document.source_id.in_(filters.source_ids))
    if filters.content_types:
        statement = statement.where(Document.content_type.in_(filters.content_types))
    if filters.date_from is not None:
        statement = statement.where(func.coalesce(Document.published_at, Document.observed_at, DocumentVersion.observed_at) >= filters.date_from)
    if filters.date_to is not None:
        statement = statement.where(func.coalesce(Document.published_at, Document.observed_at, DocumentVersion.observed_at) <= filters.date_to)
    return statement


def _visible_rows(
    *columns: Any, destination: ToolDestination = ToolDestination.LOCAL,
    source_generation_fences: dict[UUID, int] | None = None,
) -> Select[Any]:
    """Build a fresh active/current query with destination and optional generation fences."""
    statement = (
        select(*columns)
        .join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(
            Document.current_version == DocumentVersion.version_number,
            Document.extraction_status.in_(("ready", "succeeded")),
            Source.status == "active",
        )
    )
    if destination != ToolDestination.LOCAL:
        statement = statement.where(Source.local_only.is_(False))
    if source_generation_fences is not None:
        fence_conditions = [
            and_(Document.source_id == source_id, Source.generation == generation)
            for source_id, generation in source_generation_fences.items()
        ]
        statement = statement.where(or_(*fence_conditions) if fence_conditions else false())
    return statement


async def _lexical_ids(
    session: AsyncSession, request: SearchRequest,
    destination: ToolDestination = ToolDestination.LOCAL,
    source_generation_fences: dict[UUID, int] | None = None,
) -> list[UUID]:
    """Retrieve bounded candidates after active/current, privacy and generation SQL filters."""
    vector = func.to_tsvector(text("'simple'"), DocumentChunk.content)
    query = func.websearch_to_tsquery(text("'simple'"), request.query)
    statement = _filters(
        _visible_rows(DocumentChunk.id, destination=destination), request, destination,
        source_generation_fences,
    ).where(vector.op("@@")(query)).order_by(
        func.ts_rank_cd(vector, query).desc(), DocumentChunk.id,
    ).limit(MAX_CANDIDATES)
    return list((await session.scalars(statement)).all())


async def _vector_ids(
    session: AsyncSession, request: SearchRequest, generation: IndexGeneration,
    values: list[float], destination: ToolDestination = ToolDestination.LOCAL,
    source_generation_fences: dict[UUID, int] | None = None,
) -> list[UUID]:
    """Retrieve bounded cosine candidates after destination and generation fences."""
    dimensions = generation.dimensions
    if dimensions is None:
        return []
    clauses = [
        "i.generation_id = :generation_id", "i.status = 'succeeded'", "i.embedding IS NOT NULL",
        "v.version_number = d.current_version", "d.extraction_status IN ('ready', 'succeeded')",
        "s.status = 'active'",
    ]
    if destination != ToolDestination.LOCAL:
        clauses.append("s.local_only = false")
    params: dict[str, object] = {"generation_id": generation.id, "embedding": json.dumps(values), "limit": MAX_CANDIDATES}
    if source_generation_fences is not None:
        if not source_generation_fences:
            clauses.append("false")
        else:
            fence_clauses = []
            for index, (source_id, source_generation) in enumerate(source_generation_fences.items()):
                source_key = f"fence_source_{index}"
                generation_key = f"fence_generation_{index}"
                fence_clauses.append(
                    f"(d.source_id = CAST(:{source_key} AS uuid) AND s.generation = :{generation_key})"
                )
                params[source_key] = str(source_id)
                params[generation_key] = source_generation
            # Keep every candidate inside the exact owner snapshot before vector LIMIT.
            clauses.append("(" + " OR ".join(fence_clauses) + ")")
    if request.filters.source_ids:
        clauses.append("d.source_id = ANY(CAST(:source_ids AS uuid[]))")
        params["source_ids"] = request.filters.source_ids
    if request.filters.content_types:
        clauses.append("d.content_type = ANY(CAST(:content_types AS text[]))")
        params["content_types"] = request.filters.content_types
    if request.filters.date_from is not None:
        clauses.append("coalesce(d.published_at, d.observed_at, v.observed_at) >= :date_from")
        params["date_from"] = request.filters.date_from
    if request.filters.date_to is not None:
        clauses.append("coalesce(d.published_at, d.observed_at, v.observed_at) <= :date_to")
        params["date_to"] = request.filters.date_to
    statement = text(
        "SELECT i.chunk_id FROM search_index_items i "
        "JOIN document_chunks c ON c.id = i.chunk_id "
        "JOIN document_versions v ON v.id = c.document_version_id "
        "JOIN documents d ON d.id = v.document_id "
        "JOIN sources s ON s.id = d.source_id "
        f"WHERE {' AND '.join(clauses)} "
        f"ORDER BY i.embedding::vector({dimensions}) <=> CAST(:embedding AS vector({dimensions})), i.chunk_id "
        "LIMIT :limit"
    )
    return list((await session.scalars(statement, params)).all())


async def search(
    session: AsyncSession, redis: Redis, settings: Settings, request: SearchRequest,
    *, destination: ToolDestination = ToolDestination.LOCAL,
    source_generation_fences: dict[UUID, int] | None = None,
    before_embedding_send: Callable[[AIExecutionConfig, ModelMapping | None, RequestPolicy, dict[UUID, int]], Awaitable[None]] | None = None,
) -> SearchResponse:
    """Retrieve and return current evidence under a trusted destination privacy class.

    Remote destinations exclude local-only sources in lexical/vector candidates and final
    hydration, bind cursors to that destination, and recheck exact chunk/version/source generation
    after any embedding await. This owner-side check is immediately before returning the result;
    a future remote sender must perform its own fresh recheck immediately before transmission.
    When supplied, ``before_embedding_send`` runs after the existing embedding privacy/revision
    recheck on every gateway attempt and receives the fresh config, mapping, policy, and original
    pre-await source generations; its authorization or cancellation exceptions propagate.
    """
    if source_generation_fences is not None and len(source_generation_fences) > 100:
        raise ValueError("Search source fence exceeds its supported bound")
    if destination != ToolDestination.LOCAL and not source_generation_fences:
        raise ValueError("Remote search requires current source-generation fences")
    offset = _offset(request, destination, source_generation_fences)
    lexical = await _lexical_ids(session, request, destination, source_generation_fences)
    vector: list[UUID] = []
    effective_mode = "lexical"
    warnings: list[str] = []
    if request.mode == "hybrid":
        generation = await session.scalar(select(IndexGeneration).where(IndexGeneration.status == "active"))
        try:
            config, mapping, policy = await configured_embedding(session, settings, redis)
            if config.endpoint_policy_denied:
                raise ValueError("Saved endpoint is denied by deployment network policy")
            if (
                generation is None or generation.dimensions is None or mapping is None
                or mapping.model != generation.model_id or mapping.version != generation.model_version
                or generation.gateway_identity != config.gateway_identity or not policy.embeddings_allowed
            ):
                raise ValueError("No permitted active embedding generation")
            async def recheck_send() -> None:
                """Reload gateway and privacy state immediately before embedding the query."""
                latest, latest_mapping, latest_policy = await configured_embedding(session, settings, redis)
                if (latest.configuration_revision != config.configuration_revision
                        or latest.endpoint_destination_id != config.endpoint_destination_id
                        or latest.gateway_identity != config.gateway_identity or latest_mapping != mapping
                        or not may_send(latest_policy, "embedding", latest_mapping,
                                        latest.endpoint_destination_id or "omniroute",
                                        bool(latest.omniroute_api_key), "embeddings")):
                    raise PrivacyPolicyDenied("Search embedding denied by current settings")
                if before_embedding_send is not None:
                    await before_embedding_send(
                        latest, latest_mapping, latest_policy, dict(source_generation_fences or {}),
                    )

            response = await gateway(config, redis, recheck_send).embed("embedding", mapping, policy, [request.query])
            values, returned_model = embedding_values(response, generation.dimensions)
            if returned_model != generation.response_model_id:
                raise ValueError("Embedding response identity changed")
            vector = await _vector_ids(
                session, request, generation, values, destination, source_generation_fences,
            )
            effective_mode = "hybrid"
        except (ModelGatewayError, RedisError, ValueError, OSError):
            warnings.append(FALLBACK_WARNING)
        except DBAPIError:
            await session.rollback()
            warnings.append(FALLBACK_WARNING)
        except HTTPException as exc:
            if exc.status_code != 503:
                raise
            warnings.append(FALLBACK_WARNING)
    ranked: dict[UUID, float] = {}
    for candidates in (lexical, vector) if effective_mode == "hybrid" else (lexical,):
        for rank, chunk_id in enumerate(candidates, 1):
            ranked[chunk_id] = ranked.get(chunk_id, 0.0) + 1 / (60 + rank)
    ordered = sorted(ranked, key=lambda chunk_id: (-ranked[chunk_id], str(chunk_id)))
    selected = ordered[offset:offset + request.limit + 1]
    # Hydrate only current/active rows allowed by the original source generations and destination.
    rows: Sequence[Any] = (await session.execute(_filters(
        _visible_rows(
            DocumentChunk.id,
            DocumentChunk.content,
            DocumentVersion.id,
            DocumentVersion.version_number,
            DocumentVersion.observed_at,
            Document.id,
            Document.title,
            Document.observed_at,
            Document.published_at,
            Document.content_type,
            Document.canonical_url,
            Source.id,
            Source.name,
            Source.type,
            Source.generation,
            destination=destination,
            source_generation_fences=source_generation_fences,
        ), request, destination, source_generation_fences,
    ).where(DocumentChunk.id.in_(selected)))).all() if selected else []
    visible = {row[0]: row for row in rows}
    items = []
    expected_fences: dict[UUID, tuple[UUID, UUID, UUID, int]] = {}
    for chunk_id in selected[:request.limit]:
        if chunk_id not in visible:
            continue
        (
            chunk_id, content, version_id, version_number, version_observed, document_id,
            title, document_observed, published_at, content_type, canonical_url,
            source_id, source_name, source_type, source_generation,
        ) = visible[chunk_id]
        expected_fences[chunk_id] = (document_id, version_id, source_id, source_generation)
        excerpt = content[:500]
        items.append(SearchHit(
            document_id=document_id, document_version_id=version_id, version_number=version_number, chunk_id=chunk_id,
            title=title, excerpt=excerpt, score=ranked[chunk_id],
            source=SearchSource(id=source_id, name=source_name, type=source_type),
            observed_at=document_observed or version_observed,
            published_at=published_at, content_type=content_type,
            citation=Citation(sourceId=source_id, documentId=document_id, chunkId=chunk_id,
                              title=title, url=canonical_url,
                              observedAt=document_observed or version_observed, quote=excerpt),
        ))
    current_ids = await _revalidate_tool_result_fences(
        session, expected_fences, destination, source_generation_fences,
    )
    items = [item for item in items if item.chunk_id in current_ids]
    next_cursor = _encode_cursor(
        request, offset + request.limit, destination, source_generation_fences,
    ) if len(selected) > request.limit else None
    return SearchResponse(items=items, next_cursor=next_cursor, effective_mode=effective_mode, warnings=warnings)


async def _revalidate_tool_result_fences(
    session: AsyncSession,
    expected: dict[UUID, tuple[UUID, UUID, UUID, int]],
    destination: ToolDestination,
    source_generation_fences: dict[UUID, int] | None,
) -> set[UUID]:
    """Return exact active/current source-generation tuples still eligible in a fresh query.

    Remote local-only sources, changed owner-supplied source generations, stale versions and
    deleted chunks are rejected after provider/retrieval awaits and immediately before return.
    This is a read-time check, not an atomic guarantee for a later network send.
    """
    if not expected:
        return set()
    statement = (
        select(DocumentChunk.id, DocumentVersion.id, Document.id, Source.id, Source.generation)
        .join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(
            DocumentChunk.id.in_(expected),
            Document.current_version == DocumentVersion.version_number,
            Document.extraction_status.in_(("ready", "succeeded")),
            Source.status == "active",
        )
    )
    if destination != ToolDestination.LOCAL:
        statement = statement.where(Source.local_only.is_(False))
    if source_generation_fences is not None:
        conditions = [
            and_(Document.source_id == source_id, Source.generation == generation)
            for source_id, generation in source_generation_fences.items()
        ]
        statement = statement.where(or_(*conditions) if conditions else false())
    rows = (await session.execute(statement)).all()
    valid: set[UUID] = set()
    for chunk_id, version_id, document_id, source_id, generation in rows:
        if expected.get(chunk_id) == (document_id, version_id, source_id, generation):
            valid.add(chunk_id)
    return valid


async def revalidate_tool_search_fences(
    session: AsyncSession,
    fences: Sequence[ToolOutputFence],
    *,
    source_ids: frozenset[UUID],
    owner_all: bool = False,
    destination: ToolDestination = ToolDestination.REMOTE,
) -> bool:
    """Require every bounded native Search result fence to remain exact and currently eligible.

    This public Search-owner contract accepts only chunk result DTOs and checks explicit source
    scope before delegating to Search's existing current-version/deletion/privacy projection.
    """
    if len(fences) > 100:
        return False
    expected: dict[UUID, tuple[UUID, UUID, UUID, int]] = {}
    source_generations: dict[UUID, int] = {}
    for fence in fences:
        if (
            not isinstance(fence, ToolOutputFence)
            or not isinstance(fence.document_id, UUID)
            or not isinstance(fence.document_version_id, UUID)
            or not isinstance(fence.source_id, UUID)
            or type(fence.source_generation) is not int or fence.source_generation < 1
            or not isinstance(fence.chunk_id, UUID)
        ):
            return False
        if not owner_all and fence.source_id not in source_ids:
            return False
        if fence.chunk_id in expected:
            return False
        previous_generation = source_generations.setdefault(fence.source_id, fence.source_generation)
        if previous_generation != fence.source_generation:
            return False
        expected[fence.chunk_id] = (
            fence.document_id, fence.document_version_id,
            fence.source_id, fence.source_generation,
        )
    if not expected:
        return True
    valid = await _revalidate_tool_result_fences(
        session, expected, destination, source_generations,
    )
    return valid == set(expected)


async def index_status(session: AsyncSession) -> SearchIndexStatus:
    """Return counters for the latest generation or an unavailable empty state."""
    generation = await session.scalar(select(IndexGeneration).order_by(IndexGeneration.created_at.desc()).limit(1))
    if generation is None:
        return SearchIndexStatus(run_id=None, status="unavailable", model_id=None, dimensions=None, indexed_items=0, failed_items=0)
    counts = dict((await session.execute(
        select(SearchIndexItem.status, func.count()).where(SearchIndexItem.generation_id == generation.id).group_by(SearchIndexItem.status)
    )).all())
    return SearchIndexStatus(run_id=generation.id, status=generation.status, model_id=generation.model_id,
                             dimensions=generation.dimensions, indexed_items=counts.get("succeeded", 0),
                             failed_items=counts.get("failed", 0))


async def search_tasks_and_goals(
    session: AsyncSession,
    owner_id: int,
    task_filter: TaskFilter,
    goal_filter: GoalFilter,
) -> tuple[TaskPage, GoalPage]:
    """Return bounded owner pages by delegating all task and goal reads to their public APIs.

    The caller supplies filters built from the authenticated owner's query. Each domain
    keeps its own cursor; task tombstone fences and reconciled goal projections remain
    controlled by the owning public list contract. This path performs reads only and
    returns owner DTOs without assigning a synthetic relevance score.
    """
    if not task_filter.q or not task_filter.q.strip():
        # Keep a whitespace query from becoming an unfiltered owner list request.
        return TaskPage(items=[]), GoalPage(items=[])

    from modules.goals import public as goals
    from modules.tasks import public as tasks

    task_page = await tasks.list_tasks(session, owner_id, task_filter)
    goal_page = await goals.list_goals(session, owner_id, goal_filter)
    return task_page, goal_page
