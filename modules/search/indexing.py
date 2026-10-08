import json
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.heavy_work import bounded_heavy_work
from core.model_gateway.client import ModelGateway, ModelGatewayError, PrivacyPolicyDenied
from core.model_gateway.policy import may_send
from core.model_gateway.schemas import AIExecutionConfig, ModelMapping, RequestPolicy
from core.realtime import commit_with_replay, make_index_change
from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope, WorkspaceContext
from modules.knowledge.documents import public as documents
from modules.knowledge.documents.models import Document, DocumentChunk, DocumentVersion
from modules.search.models import IndexGeneration, SearchIndexItem
from modules.settings import public as ai_settings
from modules.sources import public as sources
from modules.sources.models import Source

MAX_VECTOR_DIMENSIONS = 2000  # pgvector HNSW vector index limit.
AUTO_INDEX_CURSOR_KEY = "search:auto:generation-cursor"
ACTIVE_INDEX_CURSOR_KEY = "search:index:generation-cursor"
AUTO_INDEX_RETRY_DELAY = timedelta(minutes=15)


def _actor(scope: Scope) -> int:
    """Return the owner actor bound to a workspace request or durable job scope."""
    return scope.actor_user_id if isinstance(scope, InternalJobScope) else scope.user_id


async def _admit(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool, lock: bool = False,
    expected: AccessFence | None = None,
) -> AccessFence:
    """Admit Search access before its own roots, with optional ordered publication locking."""
    from fastapi import HTTPException

    if isinstance(scope, WorkspaceContext) and scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")
    if lock:
        return await workspaces.lock_access_fence(
            session, scope=scope, expected=expected, multi_workspace_enabled=multi_workspace_enabled,
        )
    return await workspaces.read_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )


@dataclass(frozen=True)
class IndexProjection:
    """Snapshot public generation counters used to decide whether to publish."""
    generation_id: UUID
    status: str
    indexed_items: int
    failed_items: int


async def _index_projection(
    session: AsyncSession, generation_id: UUID, *, scope: Scope,
) -> IndexProjection | None:
    """Read a generation's lifecycle and succeeded/failed item counts."""
    row = await session.execute(
        select(IndexGeneration.id, IndexGeneration.status)
        .where(IndexGeneration.id == generation_id, IndexGeneration.workspace_id == scope.workspace_id)
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
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> None:
    """Commit index state and publish an event only when its projection changed.

    The transaction commits even when no event draft is needed; callers must not
    assume an unchanged projection leaves their session uncommitted.
    """
    await session.flush()
    after = await _index_projection(session, generation_id, scope=scope)
    drafts = []
    if after is not None and after != before:
        drafts.append(make_index_change(
            after.generation_id,
            after.status,
            after.indexed_items,
            after.failed_items,
            scope=scope,
        ))
    await commit_with_replay(
        session, drafts, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )


def embedding_values(response: object, expected_dimensions: int | None = None) -> tuple[list[float], str | None]:
    """Validate one finite, nonzero, bounded embedding and return its model identity."""
    if not isinstance(response, dict) or not isinstance(response.get("data"), list) or len(response["data"]) != 1:
        raise ValueError("Invalid embedding response")
    returned_model = response.get("model")
    if returned_model is not None and (not isinstance(returned_model, str) or not returned_model.strip()):
        raise ValueError("Embedding response model identity is invalid")
    row = response["data"][0]
    if not isinstance(row, dict) or not isinstance(row.get("embedding"), list):
        raise ValueError("Invalid embedding response")  # noqa: TRY004  # ValueError is part of the contract; TypeError would change behavior
    values = row["embedding"]
    if not 1 <= len(values) <= MAX_VECTOR_DIMENSIONS or expected_dimensions not in (None, len(values)):
        raise ValueError("Embedding dimensions do not match the index generation")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in values):
        raise ValueError("Embedding contains invalid values")
    if not any(value != 0 for value in values):
        raise ValueError("Embedding cannot be a zero vector")
    return [float(value) for value in values], returned_model


def gateway(config: AIExecutionConfig, redis: Redis, before_send: Callable[[], Awaitable[None]] | None = None) -> ModelGateway:
    """Construct the model gateway with configured timeout, identity, and endpoint limits."""
    return ModelGateway(redis, config.omniroute_base_url, config.omniroute_api_key,
        config.endpoint_destination_id or "omniroute", config.request_timeout_seconds,
        gateway_identity=config.gateway_identity, before_send=before_send,
        approved_endpoint_cidrs=config.endpoint_allowed_cidrs)


async def configured_embedding(
    session: AsyncSession, settings: Settings, redis: Redis, *, scope: Scope,
) -> tuple[AIExecutionConfig, ModelMapping | None, RequestPolicy]:
    """Return scoped gateway config, embedding alias and privacy-constrained policy."""
    config = await ai_settings.get_ai_execution_config(session, settings, redis, scope=scope)
    destination = config.endpoint_destination_id
    privacy = config.privacy
    policy = RequestPolicy(
        embeddings_allowed=privacy.allow_remote_embeddings,
        permitted_destinations=frozenset({destination} if destination else set()),
        embedding_destinations=frozenset(privacy.embedding_destinations),
        configuration_revision=config.configuration_revision,
    )
    return config, config.aliases.get("embedding"), policy


def eligible_chunks(*, workspace_id: UUID) -> Select[UUID, str, UUID]:
    """Select one workspace's active-source chunks from ready current versions."""
    return (
        select(DocumentChunk.id, DocumentChunk.content, Source.id)
        .join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(
            Document.current_version == DocumentVersion.version_number,
            Document.workspace_id == workspace_id,
            Document.extraction_status.in_(("ready", "succeeded")),
            Source.status == "active", Source.local_only.is_(False),
        )
    )


async def create_generation(
    session: AsyncSession, mapping: ModelMapping, gateway_identity: str, *,
    scope: Scope, multi_workspace_enabled: bool,
) -> IndexGeneration:
    """Create or reuse one admitted workspace generation under its durable advisory lock."""
    access_fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True,
    )
    await session.execute(text(
        "SELECT pg_advisory_xact_lock(hashtextextended('search.generation:' || :workspace_id, 0))"
    ), {"workspace_id": str(scope.workspace_id)})
    existing = await session.scalar(select(IndexGeneration).where(
        IndexGeneration.workspace_id == scope.workspace_id,
        IndexGeneration.status.in_(("queued", "running", "active")),
    ).order_by(IndexGeneration.created_at.desc(), IndexGeneration.id).limit(1).with_for_update())
    if existing is not None and existing.status in {"queued", "running"}:
        if (existing.gateway_identity != gateway_identity or existing.model_id != mapping.model
                or existing.model_version != mapping.version):
            raise ValueError("An index generation for another gateway is still running")
        await commit_with_replay(
            session, [], scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
        )
        return existing
    if (existing is not None and existing.status == "active"
            and existing.gateway_identity == gateway_identity and existing.model_id == mapping.model
            and existing.model_version == mapping.version):
        await commit_with_replay(
            session, [], scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
        )
        return existing
    generation = IndexGeneration(
        workspace_id=scope.workspace_id, model_id=mapping.model,
        model_version=mapping.version, gateway_identity=gateway_identity,
    )
    session.add(generation)
    await session.flush()
    await _commit_index_change(
        session, None, generation_id=generation.id, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
    )
    await session.refresh(generation)
    return generation


async def _reconcile_automatic_generations(
    factory: async_sessionmaker[AsyncSession], settings: Settings, redis: Redis,
) -> None:
    """Create eligible per-workspace generations from bounded identity-only Documents discovery.

    Existing ready chunks are required. Each candidate is independently resolved, admitted,
    checked for module/config/privacy eligibility and committed under its workspace generation
    lock. Unsupported or unconfigured work remains pending; this reconciliation never calls a
    provider or sends document content.
    """
    after: UUID | None = None
    try:
        raw_cursor = await redis.get(AUTO_INDEX_CURSOR_KEY)
        if raw_cursor:
            after = UUID(raw_cursor.decode() if isinstance(raw_cursor, bytes) else str(raw_cursor))
    except (RedisError, ValueError, UnicodeDecodeError):
        after = None
    async with factory() as session:
        workspace_ids = await documents.list_indexable_workspace_ids(session, after=after, limit=100)
        await session.rollback()
    if not workspace_ids and after is not None:
        async with factory() as session:
            workspace_ids = await documents.list_indexable_workspace_ids(session, limit=100)
            await session.rollback()
    if not workspace_ids:
        try:
            await redis.delete(AUTO_INDEX_CURSOR_KEY)
        except RedisError:
            pass
        return

    for workspace_id in workspace_ids:
        try:
            async with factory() as session:
                owner = await workspaces.resolve_workspace_owner_context(
                    session, workspace_id, multi_workspace_enabled=settings.multi_workspace_enabled,
                )
                if owner is None:
                    await session.rollback()
                    continue
                scope = InternalJobScope(
                    workspace_id=workspace_id, actor_user_id=owner.user_id,
                    membership_revision=owner.membership_revision,
                )
                access_fence = await _admit(
                    session, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled,
                    lock=True,
                )
                enabled = await ai_settings.module_is_enabled(
                    session, "search", scope=scope,
                    multi_workspace_enabled=settings.multi_workspace_enabled,
                )
                if not enabled:
                    await session.rollback()
                    continue
                config, mapping, policy = await configured_embedding(session, settings, redis, scope=scope)
                destination = config.endpoint_destination_id or "omniroute"
                if (config.endpoint_policy_denied or not may_send(
                    policy, "embedding", mapping, destination,
                    bool(config.omniroute_api_key), "embeddings",
                )):
                    await session.rollback()
                    continue
                has_chunks = await session.scalar(
                    eligible_chunks(workspace_id=workspace_id).with_only_columns(DocumentChunk.id).limit(1)
                )
                if has_chunks is None:
                    await session.rollback()
                    continue
                latest = await session.scalar(select(IndexGeneration).where(
                    IndexGeneration.workspace_id == workspace_id,
                ).order_by(IndexGeneration.created_at.desc(), IndexGeneration.id).limit(1))
                if (latest is not None and latest.status == "failed"
                        and latest.model_id == mapping.model and latest.model_version == mapping.version
                        and latest.gateway_identity == config.gateway_identity
                        and datetime.now(UTC) - latest.updated_at < AUTO_INDEX_RETRY_DELAY):
                    await session.rollback()
                    continue
                # create_generation reacquires the same transaction lock harmlessly and commits
                # the original access fence with any generation/event publication.
                await create_generation(
                    session, mapping, config.gateway_identity, scope=scope,
                    multi_workspace_enabled=settings.multi_workspace_enabled,
                )
        except Exception as exc:  # noqa: BLE001  # one unavailable tenant must not starve later workspace IDs
            from fastapi import HTTPException

            if not isinstance(exc, HTTPException) or exc.status_code not in {401, 403, 404, 409}:
                raise
        finally:
            try:
                await redis.set(AUTO_INDEX_CURSOR_KEY, str(workspace_id))
            except RedisError:
                pass


@bounded_heavy_work
async def index_pending_chunks(ctx: dict[str, object]) -> int:
    """Reconcile and index one workspace at a time under current owner, module and AI policy."""
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    redis = cast(Redis, ctx["redis"])
    settings = cast(Settings, ctx["settings"])
    async with factory() as session:
        await documents.backfill_current_chunks(session, multi_workspace_enabled=settings.multi_workspace_enabled)
    await _reconcile_automatic_generations(factory, settings, redis)

    active_after: UUID | None = None
    try:
        raw_cursor = await redis.get(ACTIVE_INDEX_CURSOR_KEY)
        if raw_cursor:
            active_after = UUID(raw_cursor.decode() if isinstance(raw_cursor, bytes) else str(raw_cursor))
    except (RedisError, ValueError, UnicodeDecodeError):
        active_after = None
    async with factory() as session:
        generation_identity = (await session.execute(
            select(IndexGeneration.id, IndexGeneration.workspace_id)
            .where(IndexGeneration.status.in_(("queued", "running")))
            .order_by(IndexGeneration.created_at, IndexGeneration.id)
            .limit(1)
        )).one_or_none()
        if generation_identity is None:
            active_query = select(IndexGeneration.id, IndexGeneration.workspace_id).where(IndexGeneration.status == "active")
            if active_after is not None:
                active_query = active_query.where(IndexGeneration.workspace_id > active_after)
            generation_identity = (await session.execute(
                active_query.order_by(IndexGeneration.workspace_id).limit(1)
            )).one_or_none()
            if generation_identity is None and active_after is not None:
                generation_identity = (await session.execute(select(
                    IndexGeneration.id, IndexGeneration.workspace_id,
                ).where(
                    IndexGeneration.status == "active",
                ).order_by(IndexGeneration.workspace_id).limit(1))).one_or_none()
        if generation_identity is None:
            return 0
        generation_id, workspace_id = generation_identity
        await session.rollback()
    try:
        await redis.set(ACTIVE_INDEX_CURSOR_KEY, str(workspace_id))
    except RedisError:
        pass

    from fastapi import HTTPException

    async with factory() as session:
        owner = await workspaces.resolve_workspace_owner_context(
            session, workspace_id, multi_workspace_enabled=settings.multi_workspace_enabled,
        )
        if owner is None:
            await session.rollback()
            return 0
        scope = InternalJobScope(
            workspace_id=workspace_id, actor_user_id=owner.user_id,
            membership_revision=owner.membership_revision,
        )
        try:
            access_fence = await _admit(
                session, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled,
            )
            if not await ai_settings.module_is_enabled(
                session, "search", scope=scope,
                multi_workspace_enabled=settings.multi_workspace_enabled,
            ):
                await session.rollback()
                return 0
            config, mapping, policy = await configured_embedding(session, settings, redis, scope=scope)
        except HTTPException as exc:
            await session.rollback()
            if exc.status_code in {401, 403, 404, 409}:
                return 0
            raise
        generation = await session.scalar(select(IndexGeneration).where(
            IndexGeneration.id == generation_id, IndexGeneration.workspace_id == workspace_id,
        ))
        if generation is None:
            await session.rollback()
            return 0
        if not policy.embeddings_allowed:
            await session.rollback()
            return 0
        if (mapping is None or mapping.model != generation.model_id
                or mapping.version != generation.model_version
                or config.gateway_identity != generation.gateway_identity):
            if generation.status != "active":
                before = await _index_projection(session, generation_id, scope=scope)
                generation.status, generation.error_code = "failed", "model_unavailable"
                await _commit_index_change(
                    session, before, generation_id=generation_id, scope=scope,
                    multi_workspace_enabled=settings.multi_workspace_enabled, access_fence=access_fence,
                )
            else:
                await session.rollback()
            return 0
        await session.rollback()

    completed = 0
    for _ in range(2):
        async with factory() as session:
            access_fence = await _admit(
                session, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled,
            )
            if not await ai_settings.module_is_enabled(
                session, "search", scope=scope,
                multi_workspace_enabled=settings.multi_workspace_enabled,
            ):
                await session.rollback()
                break
            generation = await session.scalar(select(IndexGeneration).where(
                IndexGeneration.id == generation_id, IndexGeneration.workspace_id == workspace_id,
            ).with_for_update())
            if generation is None or generation.status not in {"queued", "running", "active"}:
                await session.rollback()
                break
            before = await _index_projection(session, generation_id, scope=scope)
            if generation.status == "queued":
                generation.status = "running"
            row = (await session.execute(
                eligible_chunks(workspace_id=workspace_id).outerjoin(
                    SearchIndexItem,
                    (SearchIndexItem.chunk_id == DocumentChunk.id) & (SearchIndexItem.generation_id == generation_id),
                ).where((SearchIndexItem.id.is_(None)) | (SearchIndexItem.status == "pending"))
                .order_by(DocumentChunk.id).limit(1)
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
                        "AND d.workspace_id = :workspace_id "
                        "AND d.extraction_status IN ('ready', 'succeeded') "
                        "AND s.status = 'active' AND s.local_only = false)"
                    ), {"generation_id": generation_id, "workspace_id": str(workspace_id)})
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
                        await activate_generation(session, generation, scope=scope)
                await _commit_index_change(
                    session, before, generation_id=generation_id, scope=scope,
                    multi_workspace_enabled=settings.multi_workspace_enabled, access_fence=access_fence,
                )
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
            await _commit_index_change(
                session, before, generation_id=generation_id, scope=scope,
                multi_workspace_enabled=settings.multi_workspace_enabled, access_fence=access_fence,
            )
        try:
            async with factory() as session:
                access_fence = await _admit(
                    session, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled,
                )
                if not await ai_settings.module_is_enabled(
                    session, "search", scope=scope,
                    multi_workspace_enabled=settings.multi_workspace_enabled,
                ):
                    await session.rollback()
                    break
                # Hold the source lock across transport so archive/purge cannot race a send.
                source = await sources.lock_source(
                    session, source_id, scope=scope,
                    multi_workspace_enabled=settings.multi_workspace_enabled,
                    expected_access_fence=access_fence,
                )
                current = await session.scalar(
                    select(DocumentChunk.id)
                    .join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
                    .join(Document, Document.id == DocumentVersion.document_id)
                    .where(DocumentChunk.id == chunk_id, Document.workspace_id == workspace_id,
                           Document.current_version == DocumentVersion.version_number,
                           Document.extraction_status.in_(("ready", "succeeded")))
                )
                if source is None or source.status != "active" or source.local_only or current is None:
                    item = await session.scalar(select(SearchIndexItem).join(
                        IndexGeneration, IndexGeneration.id == SearchIndexItem.generation_id,
                    ).where(
                        SearchIndexItem.id == item_id,
                        IndexGeneration.workspace_id == workspace_id,
                    ).with_for_update())
                    if item is not None:
                        before = await _index_projection(session, generation_id, scope=scope)
                        await session.delete(item)
                        await _commit_index_change(
                            session, before, generation_id=generation_id, scope=scope,
                            multi_workspace_enabled=settings.multi_workspace_enabled, access_fence=access_fence,
                        )
                    continue
                config, mapping, policy = await configured_embedding(session, settings, redis, scope=scope)
                if config.endpoint_policy_denied or not may_send(
                    policy, "embedding", mapping, config.endpoint_destination_id or "omniroute",
                    bool(config.omniroute_api_key), "embeddings",
                ):
                    await session.rollback()
                    break
                if (
                    mapping is None or mapping.model != generation.model_id
                    or mapping.version != generation.model_version
                    or config.gateway_identity != generation.gateway_identity
                ):
                    raise ValueError("Embedding model or gateway identity changed")

                async def recheck_send() -> None:
                    """Re-read AI settings before the request and enforce the current send policy."""
                    latest, latest_mapping, latest_policy = await configured_embedding(
                        session, settings, redis, scope=scope,
                    )
                    if (latest.gateway_identity != config.gateway_identity or latest_mapping != mapping  # noqa: B023  # closure is awaited within the same loop iteration
                            or not may_send(latest_policy, "embedding", latest_mapping,
                                            latest.endpoint_destination_id or "omniroute",
                                            bool(latest.omniroute_api_key), "embeddings")):
                        raise PrivacyPolicyDenied("Embedding send denied by current settings")

                response = await gateway(config, redis, recheck_send).embed("embedding", mapping, policy, [content])
                values, returned_model = embedding_values(response, dimensions)
                generation = await session.scalar(select(IndexGeneration).where(
                    IndexGeneration.id == generation_id, IndexGeneration.workspace_id == workspace_id,
                ).with_for_update())
                item = await session.scalar(select(SearchIndexItem).join(
                    IndexGeneration, IndexGeneration.id == SearchIndexItem.generation_id,
                ).where(
                    SearchIndexItem.id == item_id, IndexGeneration.workspace_id == workspace_id,
                ).with_for_update())
                if generation is None or item is None or generation.status not in {"running", "active"}:
                    if item is not None:
                        before = await _index_projection(session, item.generation_id, scope=scope)
                        await session.delete(item)
                        await _commit_index_change(
                            session, before, generation_id=item.generation_id, scope=scope,
                            multi_workspace_enabled=settings.multi_workspace_enabled, access_fence=access_fence,
                        )
                    continue
                before = await _index_projection(session, generation_id, scope=scope)
                if generation.dimensions is None:
                    generation.response_model_id = returned_model
                    generation.dimensions = len(values)
                elif returned_model != generation.response_model_id:
                    generation.status = "failed"
                    generation.error_code = "model_identity_changed"
                    item.status = "failed"
                    item.error_code = "model_identity_changed"
                    await _commit_index_change(
                        session, before, generation_id=generation_id, scope=scope,
                        multi_workspace_enabled=settings.multi_workspace_enabled, access_fence=access_fence,
                    )
                    break
                elif generation.dimensions != len(values):
                    raise ValueError("Embedding dimensions changed during indexing")
                await session.execute(text("UPDATE search_index_items SET embedding = CAST(:embedding AS vector) WHERE id = :item_id"),
                                      {"embedding": json.dumps(values), "item_id": item_id})
                item.status = "succeeded"
                item.error_code = None
                await _commit_index_change(
                    session, before, generation_id=generation_id, scope=scope,
                    multi_workspace_enabled=settings.multi_workspace_enabled, access_fence=access_fence,
                )
                completed += 1
        except (ModelGatewayError, RedisError, ValueError):
            async with factory() as session:
                try:
                    failure_fence = await _admit(
                        session, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled,
                    )
                except HTTPException:
                    await session.rollback()
                    continue
                item = await session.scalar(select(SearchIndexItem).join(
                    IndexGeneration, IndexGeneration.id == SearchIndexItem.generation_id,
                ).where(
                    SearchIndexItem.id == item_id, IndexGeneration.workspace_id == workspace_id,
                ).with_for_update())
                if item is not None:
                    before = await _index_projection(session, item.generation_id, scope=scope)
                    item.status = "failed"
                    item.error_code = "embedding_failed"
                    await _commit_index_change(
                        session, before, generation_id=item.generation_id, scope=scope,
                        multi_workspace_enabled=settings.multi_workspace_enabled, access_fence=failure_fence,
                    )
    return completed


async def activate_generation(
    session: AsyncSession, generation: IndexGeneration, *, scope: Scope,
) -> None:
    """Create one generation's vector index and retire only its admitted workspace predecessor."""
    if generation.dimensions is None or not 1 <= generation.dimensions <= MAX_VECTOR_DIMENSIONS:
        raise ValueError("Invalid generation dimensions")
    # Identifier and dimensions originate from a UUID and a bounded integer, never request text.
    index_name = f"ix_search_vector_{generation.id.hex}"
    await session.execute(text(
        f"CREATE INDEX IF NOT EXISTS {index_name} ON search_index_items "
        f"USING hnsw ((embedding::vector({generation.dimensions})) vector_cosine_ops) "
        f"WHERE generation_id = '{generation.id}' AND status = 'succeeded'"
    ))
    prior = await session.scalar(select(IndexGeneration).where(
        IndexGeneration.workspace_id == scope.workspace_id,
        IndexGeneration.status == "active",
        IndexGeneration.id != generation.id,
    ).with_for_update())
    if prior is not None:
        prior.status = "retired"
        await session.flush()
    generation.status = "active"
    generation.error_code = None
