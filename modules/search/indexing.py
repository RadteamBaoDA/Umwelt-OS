import json
import math
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.heavy_work import bounded_heavy_work
from core.model_gateway.client import ModelGateway, ModelGatewayError, PrivacyPolicyDenied
from core.model_gateway.policy import may_send
from core.model_gateway.schemas import ModelMapping, RequestPolicy
from core.realtime import commit_with_replay, make_index_change
from modules.knowledge.documents.models import Document, DocumentChunk, DocumentVersion
from modules.knowledge.documents.public import backfill_current_chunks
from modules.search.models import IndexGeneration, SearchIndexItem
from modules.settings import public as ai_settings
from modules.sources import public as sources
from modules.sources.models import Source

MAX_VECTOR_DIMENSIONS = 2000  # pgvector HNSW vector index limit.


@dataclass(frozen=True)
class IndexProjection:
    """Snapshot public generation counters used to decide whether to publish."""
    generation_id: UUID
    status: str
    indexed_items: int
    failed_items: int


async def _index_projection(
    session: AsyncSession, generation_id: UUID
) -> IndexProjection | None:
    """Read a generation's lifecycle and succeeded/failed item counts."""
    row = await session.execute(
        select(IndexGeneration.id, IndexGeneration.status)
        .where(IndexGeneration.id == generation_id)
    )
    generation = row.one_or_none()
    if generation is None:
        return None
    counts = dict((await session.execute(
        select(SearchIndexItem.status, func.count())
        .where(SearchIndexItem.generation_id == generation_id)
        .group_by(SearchIndexItem.status)
    )).all())
    return IndexProjection(
        generation_id=generation.id,
        status=generation.status,
        indexed_items=counts.get("succeeded", 0),
        failed_items=counts.get("failed", 0),
    )


async def _commit_index_change(
    session: AsyncSession,
    before: IndexProjection | None,
    *,
    generation_id: UUID,
) -> None:
    """Commit index state and publish an event only when its projection changed.

    The transaction commits even when no event draft is needed; callers must not
    assume an unchanged projection leaves their session uncommitted.
    """
    await session.flush()
    after = await _index_projection(session, generation_id)
    drafts = []
    if after is not None and after != before:
        drafts.append(make_index_change(
            after.generation_id,
            after.status,
            after.indexed_items,
            after.failed_items,
        ))
    await commit_with_replay(session, drafts)


def embedding_values(response: object, expected_dimensions: int | None = None) -> tuple[list[float], str | None]:
    """Validate one finite, nonzero, bounded embedding and return its model identity."""
    if not isinstance(response, dict) or not isinstance(response.get("data"), list) or len(response["data"]) != 1:
        raise ValueError("Invalid embedding response")
    returned_model = response.get("model")
    if returned_model is not None and (not isinstance(returned_model, str) or not returned_model.strip()):
        raise ValueError("Embedding response model identity is invalid")
    row = response["data"][0]
    if not isinstance(row, dict) or not isinstance(row.get("embedding"), list):
        raise ValueError("Invalid embedding response")
    values = row["embedding"]
    if not 1 <= len(values) <= MAX_VECTOR_DIMENSIONS or expected_dimensions not in (None, len(values)):
        raise ValueError("Embedding dimensions do not match the index generation")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in values):
        raise ValueError("Embedding contains invalid values")
    if not any(value != 0 for value in values):
        raise ValueError("Embedding cannot be a zero vector")
    return [float(value) for value in values], returned_model


def gateway(config, redis: Redis, before_send=None) -> ModelGateway:
    """Construct the model gateway with configured timeout, identity, and endpoint limits."""
    return ModelGateway(redis, config.omniroute_base_url, config.omniroute_api_key,
        config.endpoint_destination_id or "omniroute", config.request_timeout_seconds,
        gateway_identity=config.gateway_identity, before_send=before_send,
        approved_endpoint_cidrs=config.endpoint_allowed_cidrs)


async def configured_embedding(session: AsyncSession, settings: Settings, redis: Redis):
    """Return current gateway config, embedding alias, and privacy-constrained policy."""
    config = await ai_settings.get_ai_execution_config(session, settings, redis)
    destination = config.endpoint_destination_id
    privacy = config.privacy
    policy = RequestPolicy(
        embeddings_allowed=privacy.allow_remote_embeddings,
        permitted_destinations=frozenset({destination} if destination else set()),
        embedding_destinations=frozenset(privacy.embedding_destinations),
        configuration_revision=config.configuration_revision,
    )
    return config, config.aliases.get("embedding"), policy


def eligible_chunks():
    """Select active-source chunks from ready current versions, excluding local-only data."""
    return (
        select(DocumentChunk.id, DocumentChunk.content, Source.id)
        .join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(
            Document.current_version == DocumentVersion.version_number,
            Document.extraction_status.in_(("ready", "succeeded")),
            Source.status == "active", Source.local_only.is_(False),
        )
    )


async def create_generation(session: AsyncSession, mapping: ModelMapping, gateway_identity: str) -> IndexGeneration:
    """Serialize generation creation and reuse only an in-flight generation for this gateway."""
    await session.execute(text("SELECT pg_advisory_xact_lock(4603201)"))
    existing = await session.scalar(select(IndexGeneration).where(
        IndexGeneration.status.in_(("queued", "running")),
    ).order_by(IndexGeneration.created_at).limit(1))
    if existing is not None:
        if existing.gateway_identity != gateway_identity:
            raise ValueError("An index generation for another gateway is still running")
        await session.commit()
        return existing
    generation = IndexGeneration(model_id=mapping.model, model_version=mapping.version, gateway_identity=gateway_identity)
    session.add(generation)
    await session.flush()
    await _commit_index_change(session, None, generation_id=generation.id)
    await session.refresh(generation)
    return generation


@bounded_heavy_work
async def index_pending_chunks(ctx: dict[str, object]) -> int:
    """Index a bounded number of eligible chunks under source locks and current AI policy."""
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    redis = cast(Redis, ctx["redis"])
    settings = cast(Settings, ctx["settings"])
    async with factory() as session:
        await backfill_current_chunks(session)
    async with factory() as session:
        generation = await session.scalar(
            select(IndexGeneration)
            .where(IndexGeneration.status.in_(("queued", "running")))
            .order_by(IndexGeneration.created_at, IndexGeneration.id)
            .limit(1)
        )
        if generation is None:
            generation = await session.scalar(select(IndexGeneration).where(IndexGeneration.status == "active"))
        if generation is None:
            return 0
        generation_id = generation.id

    try:
        async with factory() as session:
            config, mapping, policy = await configured_embedding(session, settings, redis)
        if not policy.embeddings_allowed:
            return 0
        if mapping is None or mapping.model != generation.model_id or mapping.version != generation.model_version or config.gateway_identity != generation.gateway_identity:
            raise ValueError("Embedding model or gateway identity changed")
    except Exception:
        async with factory() as session:
            generation = await session.get(IndexGeneration, generation_id, with_for_update=True)
            if generation is not None and generation.status != "active":
                before = await _index_projection(session, generation_id)
                generation.status = "failed"
                generation.error_code = "model_unavailable"
                await _commit_index_change(session, before, generation_id=generation_id)
        return 0

    completed = 0
    for _ in range(2):
        async with factory() as session:
            generation = await session.get(IndexGeneration, generation_id, with_for_update=True)
            if generation is None or generation.status not in {"queued", "running", "active"}:
                break
            before = await _index_projection(session, generation_id)
            if generation.status == "queued":
                generation.status = "running"
            row = (await session.execute(
                eligible_chunks().outerjoin(
                    SearchIndexItem,
                    (SearchIndexItem.chunk_id == DocumentChunk.id) & (SearchIndexItem.generation_id == generation_id),
                ).where((SearchIndexItem.id.is_(None)) | (SearchIndexItem.status == "pending")).order_by(DocumentChunk.id).limit(1)
            )).first()
            if row is None:
                if generation.status == "running":
                    await session.execute(text(
                        "DELETE FROM search_index_items i WHERE i.generation_id = :generation_id "
                        "AND i.status IN ('pending', 'failed') AND NOT EXISTS ("
                        "SELECT 1 FROM document_chunks c "
                        "JOIN document_versions v ON v.id = c.document_version_id "
                        "JOIN documents d ON d.id = v.document_id "
                        "JOIN sources s ON s.id = d.source_id "
                        "WHERE c.id = i.chunk_id AND v.version_number = d.current_version "
                        "AND d.extraction_status IN ('ready', 'succeeded') "
                        "AND s.status = 'active' AND s.local_only = false)"
                    ), {"generation_id": generation_id})
                    failed = await session.scalar(select(func.count()).select_from(SearchIndexItem).where(
                        SearchIndexItem.generation_id == generation_id, SearchIndexItem.status != "succeeded",
                    ))
                    if failed:
                        generation.status = "failed"
                        generation.error_code = "item_failed"
                    elif generation.dimensions is None:
                        generation.status = "failed"
                        generation.error_code = "no_indexable_chunks"
                    else:
                        await activate_generation(session, generation)
                await _commit_index_change(session, before, generation_id=generation_id)
                break
            chunk_id, content, source_id = row
            item = await session.scalar(select(SearchIndexItem).where(
                SearchIndexItem.generation_id == generation_id, SearchIndexItem.chunk_id == chunk_id,
            ))
            if item is None:
                item = SearchIndexItem(generation_id=generation_id, chunk_id=chunk_id)
                session.add(item)
                await session.flush()
            item_id = item.id
            dimensions = generation.dimensions
            await _commit_index_change(session, before, generation_id=generation_id)
        try:
            async with factory() as session:
                # Hold the source lock across transport so archive/purge cannot race a send.
                source = await sources.lock_source(session, source_id)
                current = await session.scalar(
                    select(DocumentChunk.id)
                    .join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
                    .join(Document, Document.id == DocumentVersion.document_id)
                    .where(DocumentChunk.id == chunk_id,
                           Document.current_version == DocumentVersion.version_number,
                           Document.extraction_status.in_(("ready", "succeeded")))
                )
                if source is None or source.status != "active" or source.local_only or current is None:
                    item = await session.get(SearchIndexItem, item_id, with_for_update=True)
                    if item is not None:
                        before = await _index_projection(session, generation_id)
                        await session.delete(item)
                        await _commit_index_change(session, before, generation_id=generation_id)
                    continue
                config, mapping, policy = await configured_embedding(session, settings, redis)
                if not policy.embeddings_allowed:
                    break
                if (
                    mapping is None or mapping.model != generation.model_id
                    or mapping.version != generation.model_version
                    or config.gateway_identity != generation.gateway_identity
                ):
                    raise ValueError("Embedding model or gateway identity changed")

                async def recheck_send() -> None:
                    """Re-read AI settings before the request and enforce the current send policy."""
                    latest, latest_mapping, latest_policy = await configured_embedding(session, settings, redis)
                    if (latest.gateway_identity != config.gateway_identity or latest_mapping != mapping
                            or not may_send(latest_policy, "embedding", latest_mapping,
                                            latest.endpoint_destination_id or "omniroute",
                                            bool(latest.omniroute_api_key), "embeddings")):
                        raise PrivacyPolicyDenied("Embedding send denied by current settings")

                response = await gateway(config, redis, recheck_send).embed("embedding", mapping, policy, [content])
                values, returned_model = embedding_values(response, dimensions)
                generation = await session.get(IndexGeneration, generation_id, with_for_update=True)
                item = await session.get(SearchIndexItem, item_id, with_for_update=True)
                if generation is None or item is None or generation.status not in {"running", "active"}:
                    if item is not None:
                        before = await _index_projection(session, item.generation_id)
                        await session.delete(item)
                        await _commit_index_change(
                            session, before, generation_id=item.generation_id
                        )
                    continue
                before = await _index_projection(session, generation_id)
                if generation.dimensions is None:
                    generation.response_model_id = returned_model
                    generation.dimensions = len(values)
                elif returned_model != generation.response_model_id:
                    generation.status = "failed"
                    generation.error_code = "model_identity_changed"
                    item.status = "failed"
                    item.error_code = "model_identity_changed"
                    await _commit_index_change(session, before, generation_id=generation_id)
                    break
                elif generation.dimensions != len(values):
                    raise ValueError("Embedding dimensions changed during indexing")
                await session.execute(text("UPDATE search_index_items SET embedding = CAST(:embedding AS vector) WHERE id = :item_id"),
                                      {"embedding": json.dumps(values), "item_id": item_id})
                item.status = "succeeded"
                item.error_code = None
                await _commit_index_change(session, before, generation_id=generation_id)
                completed += 1
        except (ModelGatewayError, RedisError, ValueError):
            async with factory() as session:
                item = await session.get(SearchIndexItem, item_id, with_for_update=True)
                if item is not None:
                    before = await _index_projection(session, item.generation_id)
                    item.status = "failed"
                    item.error_code = "embedding_failed"
                    await _commit_index_change(
                        session, before, generation_id=item.generation_id
                    )
    return completed


async def activate_generation(session: AsyncSession, generation: IndexGeneration) -> None:
    """Create the bounded vector index and atomically retire the prior active generation."""
    if generation.dimensions is None or not 1 <= generation.dimensions <= MAX_VECTOR_DIMENSIONS:
        raise ValueError("Invalid generation dimensions")
    # Identifier and dimensions originate from a UUID and a bounded integer, never request text.
    index_name = f"ix_search_vector_{generation.id.hex}"
    await session.execute(text(
        f"CREATE INDEX IF NOT EXISTS {index_name} ON search_index_items "
        f"USING hnsw ((embedding::vector({generation.dimensions})) vector_cosine_ops) "
        f"WHERE generation_id = '{generation.id}' AND status = 'succeeded'"
    ))
    prior = await session.scalar(select(IndexGeneration).where(IndexGeneration.status == "active").with_for_update())
    if prior is not None:
        prior.status = "retired"
        await session.flush()
    generation.status = "active"
    generation.error_code = None
