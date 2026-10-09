import json
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from fastapi import HTTPException
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.heavy_work import bounded_heavy_work
from core.job_denial import admit_retry_stale, denial_code, terminalize
from core.model_gateway.client import ModelGateway, ModelGatewayError, PrivacyPolicyDenied
from core.model_gateway.policy import may_send
from core.model_gateway.schemas import AIExecutionConfig, ModelMapping, RequestPolicy
from core.realtime import commit_with_replay, make_index_change
from core.worker_cursors import STATE_KEY, read_cursor, write_cursor
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
QUEUED_INDEX_CURSOR_KEY = "search:index:queued-cursor"
_CURSOR_KEYS = frozenset({AUTO_INDEX_CURSOR_KEY, ACTIVE_INDEX_CURSOR_KEY, QUEUED_INDEX_CURSOR_KEY})
SCAN_PAGE = 20  # identities scanned per status class per invocation; bounds skip cost
AUTO_INDEX_RETRY_DELAY = timedelta(minutes=15)
logger = logging.getLogger(__name__)


def _actor(scope: Scope) -> int:
    """Return the owner actor bound to a workspace request or durable job scope."""
    return scope.actor_user_id if isinstance(scope, InternalJobScope) else scope.user_id


async def _admit(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool, lock: bool = False,
    expected: AccessFence | None = None,
) -> AccessFence:
    """Admit Search access before its own roots, with optional ordered publication locking."""
    if isinstance(scope, WorkspaceContext) and scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")
    if lock:
        return await workspaces.lock_access_fence(
            session, scope=scope, expected=expected, multi_workspace_enabled=multi_workspace_enabled,
        )
    fence = await workspaces.read_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if expected is not None and fence != expected:
        # read_access_fence has no expected keyword; compare the full detached value here.
        raise HTTPException(status_code=409, detail="Workspace access changed")
    return fence


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


def gateway(
    config: AIExecutionConfig, redis: Redis, before_send: Callable[[], Awaitable[None]], *, scope: Scope,
) -> ModelGateway:
    """Construct the model gateway bound to one scope, config revision, identity and fresh send check."""
    return ModelGateway(redis, config.omniroute_base_url, config.omniroute_api_key,
        config.endpoint_destination_id or "omniroute", config.request_timeout_seconds,
        scope=scope, gateway_identity=config.gateway_identity,
        configuration_revision=config.configuration_revision, before_send=before_send,
        approved_endpoint_cidrs=config.endpoint_allowed_cidrs)


async def configured_embedding(
    session: AsyncSession, settings: Settings, redis: Redis, *, scope: Scope,
) -> tuple[AIExecutionConfig, ModelMapping | None, RequestPolicy]:
    """Return scoped gateway config, embedding alias and privacy-constrained policy."""
    config = await ai_settings.get_ai_execution_config(session, settings, redis, scope=scope)
    destination = config.endpoint_destination_id
    privacy = config.privacy
    policy = RequestPolicy(
        workspace_id=config.workspace_id,
        actor_user_id=config.actor_user_id,
        membership_revision=config.membership_revision,
        gateway_identity=config.gateway_identity,
        embeddings_allowed=privacy.allow_remote_embeddings,
        permitted_destinations=frozenset({destination} if destination else set()),
        embedding_destinations=frozenset(privacy.embedding_destinations),
        configuration_revision=config.configuration_revision,
    )
    return config, config.aliases.get("embedding"), policy


@dataclass(frozen=True)
class EmbeddingAuthority:
    """Detached original access fence plus config/mapping/policy observed by one invocation.

    This is preparation, never authorization: every later publication compares it again
    under the ordered locks and aborts, never rebases, when anything differs.
    """
    fence: AccessFence
    config: AIExecutionConfig
    mapping: ModelMapping | None
    policy: RequestPolicy

    def permitted(self) -> bool:
        """Return whether the observed configuration may send embeddings at all."""
        return (self.mapping is not None and not self.config.endpoint_policy_denied
                and may_send(self.policy, "embedding", self.mapping,
                             self.config.endpoint_destination_id or "omniroute",
                             bool(self.config.omniroute_api_key), "embeddings"))

    def unchanged(
        self, config: AIExecutionConfig, mapping: ModelMapping | None, policy: RequestPolicy,
    ) -> bool:
        """Compare a fresh read with the original revision, gateway identity, mapping and policy."""
        return (config.configuration_revision == self.config.configuration_revision
                and config.gateway_identity == self.config.gateway_identity
                and config.endpoint_destination_id == self.config.endpoint_destination_id
                and mapping == self.mapping and policy == self.policy)


async def capture_authority(
    session: AsyncSession, settings: Settings, redis: Redis, *, scope: Scope,
) -> EmbeddingAuthority:
    """Admit the owner scope first, then read config; return the detached original snapshot."""
    fence = await _admit(
        session, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled,
    )
    config, mapping, policy = await configured_embedding(session, settings, redis, scope=scope)
    return EmbeddingAuthority(fence, config, mapping, policy)


def eligible_chunks(*, workspace_id: UUID) -> Select[UUID, str, UUID]:
    """Select one workspace's active-source chunks from ready current versions."""
    return (
        select(DocumentChunk.id, DocumentChunk.content, Source.id)
        .join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(
            Document.current_version == DocumentVersion.version_number,
            Document.workspace_id == workspace_id, Source.workspace_id == workspace_id,
            Document.extraction_status.in_(("ready", "succeeded")),
            Source.status == "active", Source.local_only.is_(False),
        )
    )


async def _dedupe_or_create(
    session: AsyncSession, mapping: ModelMapping, gateway_identity: str, *,
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence,
    honor_retry_delay: bool = False,
) -> IndexGeneration | None:
    """Reuse or create the workspace generation; the caller already holds the generation mutex.

    Returns None only when ``honor_retry_delay`` finds a recent matching failed run, so
    the retry decision is made under serialization rather than from a stale pre-lock read.
    """
    if honor_retry_delay:
        latest = await session.scalar(select(IndexGeneration).where(
            IndexGeneration.workspace_id == scope.workspace_id,
        ).order_by(IndexGeneration.created_at.desc(), IndexGeneration.id).limit(1))
        if (latest is not None and latest.status == "failed"
                and latest.model_id == mapping.model and latest.model_version == mapping.version
                and latest.gateway_identity == gateway_identity
                and datetime.now(UTC) - latest.updated_at < AUTO_INDEX_RETRY_DELAY):
            await session.rollback()
            return None
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


async def _take_generation_mutex(session: AsyncSession, scope: Scope) -> None:
    """Serialize generation creation for one workspace with a transaction advisory lock."""
    await session.execute(text(
        "SELECT pg_advisory_xact_lock(hashtextextended('search.generation:' || :workspace_id, 0))"
    ), {"workspace_id": str(scope.workspace_id)})


async def create_generation(
    session: AsyncSession, mapping: ModelMapping, gateway_identity: str, *,
    scope: Scope, multi_workspace_enabled: bool,
) -> IndexGeneration:
    """Create or reuse one admitted workspace generation under its durable advisory lock.

    Callers that observed configuration earlier use ``create_generation_for_authority``,
    which compares the original access fence and configuration under the same mutex.
    """
    access_fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True,
    )
    await _take_generation_mutex(session, scope)
    generation = await _dedupe_or_create(
        session, mapping, gateway_identity, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
    )
    if generation is None:  # unreachable: retry delay is only honored by the authority seam
        raise RuntimeError("Generation creation returned no row")
    return generation


async def create_generation_for_authority(
    session: AsyncSession, authority: EmbeddingAuthority, settings: Settings, redis: Redis, *,
    scope: Scope, honor_retry_delay: bool = False,
) -> IndexGeneration | None:
    """Publish a generation only if the originally observed authority still holds under the mutex.

    Order: locked access fence compared with the original, generation mutex, fresh module and
    configuration read compared with the original, failed-run retry decision, then dedupe or
    create. A change raises 409 (never rebases); None means the retry delay applies.
    """
    multi_workspace_enabled = settings.multi_workspace_enabled
    access_fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True,
        expected=authority.fence,
    )
    await _take_generation_mutex(session, scope)
    if not await ai_settings.module_is_enabled(
        session, "search", scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    ):
        raise HTTPException(status_code=409, detail="Search module is disabled")
    config, mapping, policy = await configured_embedding(session, settings, redis, scope=scope)
    if mapping is None or not authority.unchanged(config, mapping, policy) or not authority.permitted():
        raise HTTPException(status_code=409, detail="Search configuration changed")
    return await _dedupe_or_create(
        session, mapping, config.gateway_identity, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
        honor_retry_delay=honor_retry_delay,
    )


async def _reconcile_automatic_generations(
    factory: async_sessionmaker[AsyncSession], settings: Settings, redis: Redis,
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
        workspace_ids = await documents.list_indexable_workspace_ids(session, after=after, limit=100)
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
                    session, workspace_id, multi_workspace_enabled=settings.multi_workspace_enabled,
                )
                if owner is None:
                    await session.rollback()
                    continue
                scope = InternalJobScope(
                    workspace_id=workspace_id, actor_user_id=owner.user_id,
                    membership_revision=owner.membership_revision,
                )
                authority = await capture_authority(session, settings, redis, scope=scope)
                if not await ai_settings.module_is_enabled(
                    session, "search", scope=scope,
                    multi_workspace_enabled=settings.multi_workspace_enabled,
                ) or not authority.permitted():
                    await session.rollback()
                    continue
                has_chunks = await session.scalar(
                    eligible_chunks(workspace_id=workspace_id).with_only_columns(DocumentChunk.id).limit(1)
                )
                if has_chunks is None:
                    await session.rollback()
                    continue
                await create_generation_for_authority(
                    session, authority, settings, redis, scope=scope, honor_retry_delay=True,
                )
        except Exception as exc:  # one unavailable tenant must not starve later workspace IDs
            if isinstance(exc, ValueError) or (
                isinstance(exc, HTTPException) and exc.status_code in {401, 403, 404, 409}
            ):
                continue
            raise
        finally:
            await write_cursor(ctx, AUTO_INDEX_CURSOR_KEY, workspace_id, _CURSOR_KEYS)


async def _candidate_page(
    factory: async_sessionmaker[AsyncSession], statuses: tuple[str, ...], after: UUID | None,
) -> list[tuple[UUID, UUID]]:
    """Return one bounded keyset page of (generation, workspace) identities, wrapping at the end."""
    async with factory() as session:
        rows: list[tuple[UUID, UUID]] = []
        for lower in ((after, None) if after is not None else (None,)):
            query = select(IndexGeneration.id, IndexGeneration.workspace_id).where(
                IndexGeneration.status.in_(statuses),
            )
            if lower is not None:
                query = query.where(IndexGeneration.workspace_id > lower)
            result = await session.execute(
                query.order_by(IndexGeneration.workspace_id, IndexGeneration.id).limit(SCAN_PAGE)
            )
            rows = [(row[0], row[1]) for row in result.all()]
            if rows:
                break
        await session.rollback()
    return rows


@bounded_heavy_work
async def index_pending_chunks(ctx: dict[str, object]) -> int:
    """Reconcile, then index one eligible workspace generation chosen from a bounded fair page.

    Queued/running generations are scanned first by workspace keyset cursor, then active ones;
    a denied, disabled or unavailable subject is skipped without touching its rows, and the
    cursor advances past the scanned page so no single subject can monopolize indexing.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    redis = cast(Redis, ctx["redis"])
    settings = cast(Settings, ctx["settings"])
    # ARQ copies ctx per job: without the worker-installed dict this is a per-job throwaway.
    if STATE_KEY not in ctx:
        logger.warning("w2_cursor_state missing; search cursors are per-job")
    state = cast(dict[str, str], ctx.setdefault(STATE_KEY, {}))
    async with factory() as session:
        await documents.backfill_current_chunks(session, multi_workspace_enabled=settings.multi_workspace_enabled)
    await _reconcile_automatic_generations(factory, settings, redis, ctx)

    classes = [
        (QUEUED_INDEX_CURSOR_KEY, ("queued", "running")),
        (ACTIVE_INDEX_CURSOR_KEY, ("active",)),
    ]
    turn = 1 if state.get("search_class_turn") == "1" else 0
    state["search_class_turn"] = str(1 - turn)  # alternate which class goes first each invocation
    for key, statuses in classes[turn:] + classes[:turn]:
        after = await read_cursor(ctx, key, _CURSOR_KEYS)
        last: UUID | None = None
        done: int | None = None
        for generation_id, workspace_id in await _candidate_page(factory, statuses, after):
            last = workspace_id
            done = await _index_generation(factory, redis, settings, generation_id, workspace_id)
            if done is not None:
                break
        if last is not None:
            await write_cursor(ctx, key, last, _CURSOR_KEYS)
        if done is not None:
            return done
    return 0


class _SourceStale(Exception):  # control-flow signal: Source/chunk moved before the send
    """The Source or chunk is no longer eligible; the pending item should be dropped."""


class _AuthorityChanged(Exception):  # control-flow signal, not an error condition
    """The original fence, module or configuration no longer matches; discard the output."""


async def _recheck_prepared(
    session: AsyncSession, *, scope: Scope, settings: Settings, redis: Redis,
    original: EmbeddingAuthority, source_id: UUID, source_generation: int | None,
    chunk_id: UUID, content: str, workspace_id: UUID, lock: bool,
) -> tuple[AccessFence, bool, int | None]:
    """Re-admit against the original fence, then Source, then exact chunk, in lock order.

    Returns (fence, live, source_generation). ``live`` is False when the Source is no longer
    active/remote-eligible, its generation moved (when one was captured) or the chunk content
    changed. Raises _AuthorityChanged when the access fence, module or configuration changed.
    """
    try:
        fence = await _admit(
            session, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled,
            lock=lock, expected=original.fence,
        )
    except HTTPException as exc:
        if exc.status_code in {401, 403, 404, 409}:
            raise _AuthorityChanged from exc
        raise
    if fence != original.fence or not await ai_settings.module_is_enabled(
        session, "search", scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled,
    ):
        raise _AuthorityChanged
    source = await sources.lock_source(
        session, source_id, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled,
        expected_access_fence=fence,
    )
    if lock:  # publication: lock the Document (after Source) so a concurrent re-version serializes
        document_id = await session.scalar(
            select(DocumentVersion.document_id).join(
                DocumentChunk, DocumentChunk.document_version_id == DocumentVersion.id,
            ).where(DocumentChunk.id == chunk_id)
        )
        if document_id is not None:
            await documents.lock_document_ids(
                session, [document_id], scope=scope,
                multi_workspace_enabled=settings.multi_workspace_enabled,
            )
    current = await session.scalar(
        select(DocumentChunk.content)
        .join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(DocumentChunk.id == chunk_id, Document.workspace_id == workspace_id,
               Document.current_version == DocumentVersion.version_number,
               Document.extraction_status.in_(("ready", "succeeded")))
    )
    generation = source.generation if source is not None else None
    live = (source is not None and source.status == "active" and not source.local_only
            and current == content
            and (source_generation is None or generation == source_generation))
    config, mapping, policy = await configured_embedding(session, settings, redis, scope=scope)
    if not original.unchanged(config, mapping, policy):
        raise _AuthorityChanged
    return fence, live, generation


async def _index_generation(
    factory: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings,
    generation_id: UUID, workspace_id: UUID,
) -> int | None:
    """Index up to two chunks of one generation; return None when the subject is skipped.

    One original AccessFence/config snapshot is captured per invocation. No DB transaction or
    lock is open during ``embed``: preparation and the gateway ``before_send`` hook use short
    ordered sessions (access, Source, chunk), and publication reacquires the same order and
    compares the originals, discarding stale output instead of rebasing.
    """
    async with factory() as session:
        owner = await workspaces.resolve_workspace_owner_context(
            session, workspace_id, multi_workspace_enabled=settings.multi_workspace_enabled,
        )
        if owner is None:
            await session.rollback()
            return None
        scope = InternalJobScope(
            workspace_id=workspace_id, actor_user_id=owner.user_id,
            membership_revision=owner.membership_revision,
        )
        async def capture() -> EmbeddingAuthority | None:
            await session.rollback()
            captured = await capture_authority(session, settings, redis, scope=scope)
            if not await ai_settings.module_is_enabled(
                session, "search", scope=scope,
                multi_workspace_enabled=settings.multi_workspace_enabled,
            ):
                await session.rollback()
                return None
            return captured

        try:
            captured = await admit_retry_stale(capture)
        except HTTPException as exc:
            await session.rollback()
            code = denial_code(exc)
            if code is None:
                raise
            # Denial is terminal for a queued/running generation; the active one keeps serving.
            await terminalize(
                factory, IndexGeneration, generation_id, workspace_id, code,
                from_status=("queued", "running"),
            )
            return None
        if captured is None:
            return None
        original = captured
        generation = await session.scalar(select(IndexGeneration).where(
            IndexGeneration.id == generation_id, IndexGeneration.workspace_id == workspace_id,
        ))
        if generation is None or not original.permitted():
            await session.rollback()
            return None
        mapping, config = original.mapping, original.config
        if (mapping is None or mapping.model != generation.model_id
                or mapping.version != generation.model_version
                or config.gateway_identity != generation.gateway_identity):
            if generation.status != "active":
                before = await _index_projection(session, generation_id, scope=scope)
                generation.status, generation.error_code = "failed", "model_unavailable"
                await _commit_index_change(
                    session, before, generation_id=generation_id, scope=scope,
                    multi_workspace_enabled=settings.multi_workspace_enabled,
                    access_fence=original.fence,
                )
                return 0
            await session.rollback()
            return None
        await session.rollback()

    completed = 0
    idle = False
    for _ in range(2):
        async with factory() as session:
            try:
                access_fence = await _admit(
                    session, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled,
                    lock=True, expected=original.fence,
                )
            except HTTPException as exc:
                await session.rollback()
                if exc.status_code in {401, 403, 404, 409}:
                    break
                raise
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
            if row is None and generation.status != "running":
                await session.rollback()
                idle = True  # nothing pending: let the page scan move on
                break
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
                        "AND d.workspace_id = :workspace_id AND s.workspace_id = :workspace_id "
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
            assert row is not None
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
            # Short preparation transaction: access -> Source -> chunk, then release every lock.
            async with factory() as session:
                _fence, live, source_generation = await _recheck_prepared(
                    session, scope=scope, settings=settings, redis=redis, original=original,
                    source_id=source_id, source_generation=None, chunk_id=chunk_id,
                    content=content, workspace_id=workspace_id, lock=False,
                )
                await session.rollback()
            if not live or source_generation is None:
                await _drop_pending_item(
                    factory, scope=scope, settings=settings, original=original,
                    item_id=item_id, generation_id=generation_id, workspace_id=workspace_id,
                )
                continue

            async def recheck_send() -> None:
                """Fresh short access/Source/chunk/config check before each actual request."""
                async with factory() as check:
                    try:
                        _f, still_live, _g = await _recheck_prepared(
                            check, scope=scope, settings=settings, redis=redis, original=original,
                            source_id=source_id, source_generation=source_generation,  # noqa: B023  # awaited within this iteration
                            chunk_id=chunk_id, content=content, workspace_id=workspace_id,  # noqa: B023  # awaited within this iteration
                            lock=False,
                        )
                    except _AuthorityChanged as exc:
                        raise PrivacyPolicyDenied("Embedding send denied by current authority") from exc
                    finally:
                        await check.rollback()
                    if not still_live:
                        raise PrivacyPolicyDenied("Embedding source changed before send") from _SourceStale()

            response = await gateway(config, redis, recheck_send, scope=scope).embed(
                "embedding", mapping, original.policy, [content],
            )
            values, returned_model = embedding_values(response, dimensions)
            # Publication: same lock order (access -> Source -> chunk -> generation/item).
            async with factory() as session:
                fence, live, _g = await _recheck_prepared(
                    session, scope=scope, settings=settings, redis=redis, original=original,
                    source_id=source_id, source_generation=source_generation, chunk_id=chunk_id,
                    content=content, workspace_id=workspace_id, lock=True,
                )
                generation = await session.scalar(select(IndexGeneration).where(
                    IndexGeneration.id == generation_id, IndexGeneration.workspace_id == workspace_id,
                ).with_for_update())
                item = await session.scalar(select(SearchIndexItem).join(
                    IndexGeneration, IndexGeneration.id == SearchIndexItem.generation_id,
                ).where(
                    SearchIndexItem.id == item_id, IndexGeneration.workspace_id == workspace_id,
                ).with_for_update())
                if not live or generation is None or item is None or generation.status not in {"running", "active"}:
                    if item is not None:
                        before = await _index_projection(session, item.generation_id, scope=scope)
                        await session.delete(item)
                        await _commit_index_change(
                            session, before, generation_id=item.generation_id, scope=scope,
                            multi_workspace_enabled=settings.multi_workspace_enabled, access_fence=fence,
                        )
                    else:
                        await session.rollback()
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
                        multi_workspace_enabled=settings.multi_workspace_enabled, access_fence=fence,
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
                    multi_workspace_enabled=settings.multi_workspace_enabled, access_fence=fence,
                )
                completed += 1
        except _AuthorityChanged:
            break  # stale output discarded; the item stays pending for a fresh invocation
        except HTTPException as exc:
            if exc.status_code in {401, 403, 404, 409}:
                break
            raise
        except PrivacyPolicyDenied as exc:
            if isinstance(exc.__cause__, _AuthorityChanged):
                break  # leave pending for a fresh invocation
            if isinstance(exc.__cause__, _SourceStale):
                await _drop_pending_item(
                    factory, scope=scope, settings=settings, original=original,
                    item_id=item_id, generation_id=generation_id, workspace_id=workspace_id,
                )
                continue
            await _fail_item(
                factory, scope=scope, settings=settings, original=original,
                item_id=item_id, workspace_id=workspace_id,
            )
        except (ModelGatewayError, RedisError, ValueError):
            await _fail_item(
                factory, scope=scope, settings=settings, original=original,
                item_id=item_id, workspace_id=workspace_id,
            )
    return None if idle and completed == 0 else completed


async def _drop_pending_item(
    factory: async_sessionmaker[AsyncSession], *, scope: Scope, settings: Settings,
    original: EmbeddingAuthority, item_id: UUID, generation_id: UUID, workspace_id: UUID,
) -> None:
    """Delete a pending item whose Source/chunk is no longer eligible, under the original fence."""
    async with factory() as session:
        try:
            fence = await _admit(
                session, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled,
                lock=True, expected=original.fence,
            )
        except HTTPException:
            await session.rollback()
            return
        item = await session.scalar(select(SearchIndexItem).join(
            IndexGeneration, IndexGeneration.id == SearchIndexItem.generation_id,
        ).where(
            SearchIndexItem.id == item_id, IndexGeneration.workspace_id == workspace_id,
        ).with_for_update())
        if item is None:
            await session.rollback()
            return
        before = await _index_projection(session, generation_id, scope=scope)
        await session.delete(item)
        await _commit_index_change(
            session, before, generation_id=generation_id, scope=scope,
            multi_workspace_enabled=settings.multi_workspace_enabled, access_fence=fence,
        )


async def _fail_item(
    factory: async_sessionmaker[AsyncSession], *, scope: Scope, settings: Settings,
    original: EmbeddingAuthority, item_id: UUID, workspace_id: UUID,
) -> None:
    """Mark one item failed, but only while the original access fence still holds."""
    async with factory() as session:
        try:
            fence = await _admit(
                session, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled,
                lock=True, expected=original.fence,
            )
        except HTTPException:
            await session.rollback()
            return
        item = await session.scalar(select(SearchIndexItem).join(
            IndexGeneration, IndexGeneration.id == SearchIndexItem.generation_id,
        ).where(
            SearchIndexItem.id == item_id, IndexGeneration.workspace_id == workspace_id,
        ).with_for_update())
        if item is None:
            await session.rollback()
            return
        before = await _index_projection(session, item.generation_id, scope=scope)
        item.status = "failed"
        item.error_code = "embedding_failed"
        await _commit_index_change(
            session, before, generation_id=item.generation_id, scope=scope,
            multi_workspace_enabled=settings.multi_workspace_enabled, access_fence=fence,
        )


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
