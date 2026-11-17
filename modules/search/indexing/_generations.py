"""Index-generation creation, deduplication and automatic reconciliation."""

from datetime import UTC, datetime

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.errors import ConflictError, DomainError
from core.model_gateway.schemas import ModelMapping
from core.realtime import commit_with_replay
from core.worker_cursors import read_cursor, write_cursor
from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope
from modules.knowledge.documents import public as documents
from modules.knowledge.documents.public import (
    DocumentChunkReadProjection as DocumentChunk,
)
from modules.search.indexing._common import (
    _CURSOR_KEYS,
    AUTO_INDEX_CURSOR_KEY,
    AUTO_INDEX_RETRY_DELAY,
    EmbeddingAuthority,
    _admit,
    _commit_index_change,
    capture_authority,
    configured_embedding,
    eligible_chunks,
)
from modules.search.models import IndexGeneration
from modules.settings import public as ai_settings


async def _dedupe_or_create(
    session: AsyncSession,
    mapping: ModelMapping,
    model_identity: str,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    honor_retry_delay: bool = False,
) -> IndexGeneration | None:
    """Reuse or create the workspace generation; the caller already holds the generation mutex.

    Returns None only when ``honor_retry_delay`` finds a recent matching failed run, so
    the retry decision is made under serialization rather than from a stale pre-lock read.
    ``model_identity`` (stored in the legacy ``gateway_identity`` column) identifies the embedding model/provider
    space only, so web search, prompt or preference saves never force a rebuild.
    """
    if honor_retry_delay:
        latest = await session.scalar(
            select(IndexGeneration)
            .where(
                IndexGeneration.workspace_id == scope.workspace_id,
            )
            .order_by(IndexGeneration.created_at.desc(), IndexGeneration.id)
            .limit(1)
        )
        if (
            latest is not None
            and latest.status == "failed"
            and latest.model_id == mapping.model
            and latest.model_version == mapping.version
            and latest.gateway_identity == model_identity
            and datetime.now(UTC) - latest.updated_at < AUTO_INDEX_RETRY_DELAY
        ):
            await session.rollback()
            return None
    existing = await session.scalar(
        select(IndexGeneration)
        .where(
            IndexGeneration.workspace_id == scope.workspace_id,
            IndexGeneration.status.in_(("queued", "running", "active")),
        )
        .order_by(IndexGeneration.created_at.desc(), IndexGeneration.id)
        .limit(1)
        .with_for_update()
    )
    if existing is not None and existing.status in {"queued", "running"}:
        if (
            existing.gateway_identity != model_identity
            or existing.model_id != mapping.model
            or existing.model_version != mapping.version
        ):
            raise ValueError("An index generation for another gateway is still running")
        await commit_with_replay(
            session,
            [],
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
        )
        return existing
    if (
        existing is not None
        and existing.status == "active"
        and existing.gateway_identity == model_identity
        and existing.model_id == mapping.model
        and existing.model_version == mapping.version
    ):
        await commit_with_replay(
            session,
            [],
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
        )
        return existing
    generation = IndexGeneration(
        workspace_id=scope.workspace_id,
        model_id=mapping.model,
        model_version=mapping.version,
        gateway_identity=model_identity,
    )
    session.add(generation)
    await session.flush()
    await _commit_index_change(
        session,
        None,
        generation_id=generation.id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    await session.refresh(generation)
    return generation


async def _take_generation_mutex(session: AsyncSession, scope: Scope) -> None:
    """Serialize generation creation for one workspace with a transaction advisory lock."""
    await session.execute(
        text(
            "SELECT pg_advisory_xact_lock(hashtextextended('search.generation:' || :workspace_id, 0))"
        ),
        {"workspace_id": str(scope.workspace_id)},
    )


async def create_generation(
    session: AsyncSession,
    mapping: ModelMapping,
    model_identity: str,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> IndexGeneration:
    """Create or reuse one admitted workspace generation under its durable advisory lock.

    Callers that observed configuration earlier use ``create_generation_for_authority``,
    which compares the original access fence and configuration under the same mutex.
    """
    access_fence = await _admit(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        lock=True,
    )
    await _take_generation_mutex(session, scope)
    generation = await _dedupe_or_create(
        session,
        mapping,
        model_identity,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    if generation is None:  # unreachable: retry delay is only honored by the authority seam
        raise RuntimeError("Generation creation returned no row")
    return generation


async def create_generation_for_authority(
    session: AsyncSession,
    authority: EmbeddingAuthority,
    settings: Settings,
    redis: Redis,
    *,
    scope: Scope,
    honor_retry_delay: bool = False,
) -> IndexGeneration | None:
    """Publish a generation only if the originally observed authority still holds under the mutex.

    Order: locked access fence compared with the original, generation mutex, fresh module and
    configuration read compared with the original, failed-run retry decision, then dedupe or
    create. A change raises 409 (never rebases); None means the retry delay applies.
    """
    multi_workspace_enabled = settings.multi_workspace_enabled
    access_fence = await _admit(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        lock=True,
        expected=authority.fence,
    )
    await _take_generation_mutex(session, scope)
    if not await ai_settings.module_is_enabled(
        session,
        "search",
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    ):
        raise ConflictError(detail="Search module is disabled")
    config, mapping, policy = await configured_embedding(session, settings, redis, scope=scope)
    if (
        mapping is None
        or not authority.unchanged(config, mapping, policy)
        or not authority.permitted()
    ):
        raise ConflictError(detail="Search configuration changed")
    return await _dedupe_or_create(
        session,
        mapping,
        config.model_identity,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        honor_retry_delay=honor_retry_delay,
    )


async def _reconcile_automatic_generations(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    redis: Redis,
    ctx: dict[str, object],
) -> None:
    """Create eligible per-workspace generations from bounded identity-only Documents discovery.

    Existing ready chunks are required. Each candidate is independently resolved, admitted,
    checked for module/config/privacy eligibility and committed under its workspace generation
    mutex, which compares the original access fence and configuration and decides failed-run
    retry. Unsupported or unconfigured work remains pending; this reconciliation never calls a
    provider or sends document content. The cursor survives Redis loss in worker-local state.
    """
    after = await read_cursor(ctx, AUTO_INDEX_CURSOR_KEY, _CURSOR_KEYS)
    async with factory() as session:
        workspace_ids = await documents.list_indexable_workspace_ids(
            session, after=after, limit=100
        )
        await session.rollback()
    if not workspace_ids and after is not None:
        async with factory() as session:
            workspace_ids = await documents.list_indexable_workspace_ids(session, limit=100)
            await session.rollback()
    if not workspace_ids:
        await write_cursor(ctx, AUTO_INDEX_CURSOR_KEY, None, _CURSOR_KEYS)
        return

    for workspace_id in workspace_ids:
        try:
            async with factory() as session:
                owner = await workspaces.resolve_workspace_owner_context(
                    session,
                    workspace_id,
                    multi_workspace_enabled=settings.multi_workspace_enabled,
                )
                if owner is None:
                    await session.rollback()
                    continue
                scope = InternalJobScope(
                    workspace_id=workspace_id,
                    actor_user_id=owner.user_id,
                    membership_revision=owner.membership_revision,
                )
                authority = await capture_authority(session, settings, redis, scope=scope)
                if (
                    not await ai_settings.module_is_enabled(
                        session,
                        "search",
                        scope=scope,
                        multi_workspace_enabled=settings.multi_workspace_enabled,
                    )
                    or not authority.permitted()
                ):
                    await session.rollback()
                    continue
                has_chunks = await session.scalar(
                    eligible_chunks(workspace_id=workspace_id)
                    .with_only_columns(DocumentChunk.id)
                    .limit(1)
                )
                if has_chunks is None:
                    await session.rollback()
                    continue
                await create_generation_for_authority(
                    session,
                    authority,
                    settings,
                    redis,
                    scope=scope,
                    honor_retry_delay=True,
                )
        except Exception as exc:  # one unavailable tenant must not starve later workspace IDs
            if isinstance(exc, ValueError) or (
                isinstance(exc, (DomainError, HTTPException))
                and exc.status_code in {401, 403, 404, 409}
            ):
                continue
            raise
        finally:
            await write_cursor(ctx, AUTO_INDEX_CURSOR_KEY, workspace_id, _CURSOR_KEYS)
