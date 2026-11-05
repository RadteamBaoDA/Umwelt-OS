import json
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from fastapi import HTTPException
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import Select, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from core.config import Settings
from core.database import direct_engine
from core.errors import ConflictError, DomainError, ForbiddenError
from core.heavy_work import bounded_heavy_work
from core.job_denial import admit_retry_stale, denial_code, terminalize
from core.model_gateway.client import ModelGateway, ModelGatewayError, PrivacyPolicyDenied
from core.model_gateway.policy import may_send
from core.model_gateway.schemas import AIExecutionConfig, ModelMapping, RequestPolicy
from core.realtime import commit_with_replay, make_index_change
from core.worker_context import worker_session_factory
from core.worker_cursors import STATE_KEY, read_cursor, write_cursor
from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope, WorkspaceContext
from modules.knowledge.documents import public as documents
from modules.knowledge.documents.public import (
    DocumentChunkReadProjection as DocumentChunk,
)
from modules.knowledge.documents.public import (
    DocumentReadProjection as Document,
)
from modules.knowledge.documents.public import (
    DocumentVersionReadProjection as DocumentVersion,
)
from modules.search.models import IndexGeneration, SearchIndexItem
from modules.settings import public as ai_settings
from modules.sources import public as sources
from modules.sources.public import SourceReadProjection as Source

MAX_VECTOR_DIMENSIONS = 2000  # pgvector HNSW vector index limit.
AUTO_INDEX_CURSOR_KEY = "search:auto:generation-cursor"
ACTIVE_INDEX_CURSOR_KEY = "search:index:generation-cursor"
QUEUED_INDEX_CURSOR_KEY = "search:index:queued-cursor"
_CURSOR_KEYS = frozenset({AUTO_INDEX_CURSOR_KEY, ACTIVE_INDEX_CURSOR_KEY, QUEUED_INDEX_CURSOR_KEY})
# Per-invocation work bound: the body runs inside the heavy_job_slot (120s deadline, job_timeout
# 180s); the time limit only gates *starting* another item, so one in-flight embed still fits.
INDEX_RUN_ITEMS = 50
MAX_EMBED_BATCH = 128  # upper bound for SEARCH_EMBED_BATCH_SIZE
INDEX_RUN_BATCH_ITEMS = 2048  # chunk cap per invocation on the batched path (the time budget usually binds first)
INDEX_RUN_SECONDS = 45
# Per-chunk progress events are coalesced: clients only invalidate queries / poll, and status
# changes (claim of a queued generation, finalize, failure) are always published.
INDEX_EVENT_ITEMS = 100
INDEX_EVENT_SECONDS = 5.0
SCAN_PAGE = 20  # identities scanned per status class per invocation; bounds skip cost
AUTO_INDEX_RETRY_DELAY = timedelta(minutes=15)
logger = logging.getLogger(__name__)


async def _admit(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool, lock: bool = False,
    expected: AccessFence | None = None,
) -> AccessFence:
    """Admit Search access before its own roots, with optional ordered publication locking."""
    if isinstance(scope, WorkspaceContext) and scope.role != "owner":
        raise ForbiddenError(detail="Workspace owner required")
    if lock:
        return await workspaces.lock_access_fence(
            session, scope=scope, expected=expected, multi_workspace_enabled=multi_workspace_enabled,
        )
    fence = await workspaces.read_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if expected is not None and fence != expected:
        # read_access_fence has no expected keyword; compare the full detached value here.
        raise ConflictError(detail="Workspace access changed")
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
    publish: bool = True,
) -> None:
    """Commit index state and publish an event only when its projection changed.

    The transaction commits even when no event draft is needed; callers must not
    assume an unchanged projection leaves their session uncommitted. ``publish=False``
    skips the projection read and event entirely (coalesced progress commits).
    """
    await session.flush()
    after = await _index_projection(session, generation_id, scope=scope) if publish else None
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


def _vector_literal(values: list[float]) -> str:
    """Serialize a validated float vector into a pgvector literal outside locked transactions."""
    return json.dumps(values)


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
    # Validate finiteness, reject booleans/NaNs, verify non-zero norm, and coerce to float in a single pass.
    parsed: list[float] = []
    nonzero = False
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Embedding contains invalid values")
        fval = float(value)
        if not math.isfinite(fval):
            raise ValueError("Embedding contains invalid values")
        if fval != 0.0:
            nonzero = True
        parsed.append(fval)
    if not nonzero:
        raise ValueError("Embedding cannot be a zero vector")
    return parsed, returned_model


def gateway(
    config: AIExecutionConfig, redis: Redis, before_send: Callable[[], Awaitable[None]], *, scope: Scope,
) -> ModelGateway:
    """Construct the model gateway bound to one scope, config revision, identity and fresh send check."""
    config = ai_settings.derive_for_role(config, "embedding")   # embedding is this module's only role
    return ModelGateway(redis, config.gateway_base_url, config.gateway_api_key,
        config.endpoint_destination_id or "remote", config.request_timeout_seconds,
        scope=scope, gateway_identity=config.gateway_identity,
        model_identity=config.model_identity,
        configuration_revision=config.configuration_revision, before_send=before_send,
        approved_endpoint_cidrs=config.endpoint_allowed_cidrs, allow_public_endpoints=config.allow_public_providers, kind=config.gateway_kind)


async def configured_embedding(
    session: AsyncSession, settings: Settings, redis: Redis, *, scope: Scope,
) -> tuple[AIExecutionConfig, ModelMapping | None, RequestPolicy]:
    """Return scoped gateway config, embedding role and privacy-constrained policy."""
    config = ai_settings.derive_for_role(
        await ai_settings.get_ai_execution_config(session, settings, redis, scope=scope), "embedding")
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
    return config, config.defaults.resolve("embedding"), policy


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
                             self.config.endpoint_destination_id or "remote",
                             bool(self.config.gateway_api_key), "embeddings"))

    def unchanged(
        self, config: AIExecutionConfig, mapping: ModelMapping | None, policy: RequestPolicy,
    ) -> bool:
        """Compare a fresh read with the original revision, gateway identity, mapping and policy."""
        return (
            (
                config is self.config
                or (
                    config.configuration_revision == self.config.configuration_revision
                    and config.gateway_identity == self.config.gateway_identity
                    and config.endpoint_destination_id == self.config.endpoint_destination_id
                )
            )
            and (mapping is self.mapping or mapping == self.mapping)
            and (policy is self.policy or policy == self.policy)
        )


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
    """Select one workspace's active-source chunks from ready current versions, skipping Sources whose owner turned RAG indexing off or configured skip_embedding."""
    return (
        select(DocumentChunk.id, DocumentChunk.content, Source.id)
        .join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(
            Document.current_version == DocumentVersion.version_number,
            Document.workspace_id == workspace_id, Source.workspace_id == workspace_id,
            Document.extraction_status.in_(("ready", "succeeded")),
            Source.status == "active", Source.local_only.is_(False), Source.rag_index_enabled.is_(True),
            or_(
                Source.configuration["skip_embedding"].as_boolean().is_(None),
                Source.configuration["skip_embedding"].as_boolean().is_(False),
            ),
        )
    )


async def _dedupe_or_create(
    session: AsyncSession, mapping: ModelMapping, model_identity: str, *,
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence,
    honor_retry_delay: bool = False,
) -> IndexGeneration | None:
    """Reuse or create the workspace generation; the caller already holds the generation mutex.

    Returns None only when ``honor_retry_delay`` finds a recent matching failed run, so
    the retry decision is made under serialization rather than from a stale pre-lock read.
    ``model_identity`` (stored in the legacy ``gateway_identity`` column) identifies the embedding model/provider
    space only, so web search, prompt or preference saves never force a rebuild.
    """
    if honor_retry_delay:
        latest = await session.scalar(select(IndexGeneration).where(
            IndexGeneration.workspace_id == scope.workspace_id,
        ).order_by(IndexGeneration.created_at.desc(), IndexGeneration.id).limit(1))
        if (latest is not None and latest.status == "failed"
                and latest.model_id == mapping.model and latest.model_version == mapping.version
                and latest.gateway_identity == model_identity
                and datetime.now(UTC) - latest.updated_at < AUTO_INDEX_RETRY_DELAY):
            await session.rollback()
            return None
    existing = await session.scalar(select(IndexGeneration).where(
        IndexGeneration.workspace_id == scope.workspace_id,
        IndexGeneration.status.in_(("queued", "running", "active")),
    ).order_by(IndexGeneration.created_at.desc(), IndexGeneration.id).limit(1).with_for_update())
    if existing is not None and existing.status in {"queued", "running"}:
        if (existing.gateway_identity != model_identity or existing.model_id != mapping.model
                or existing.model_version != mapping.version):
            raise ValueError("An index generation for another gateway is still running")
        await commit_with_replay(
            session, [], scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
        )
        return existing
    if (existing is not None and existing.status == "active"
            and existing.gateway_identity == model_identity and existing.model_id == mapping.model
            and existing.model_version == mapping.version):
        await commit_with_replay(
            session, [], scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
        )
        return existing
    generation = IndexGeneration(
        workspace_id=scope.workspace_id, model_id=mapping.model,
        model_version=mapping.version, gateway_identity=model_identity,
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
    session: AsyncSession, mapping: ModelMapping, model_identity: str, *,
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
        session, mapping, model_identity, scope=scope,
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
        raise ConflictError(detail="Search module is disabled")
    config, mapping, policy = await configured_embedding(session, settings, redis, scope=scope)
    if mapping is None or not authority.unchanged(config, mapping, policy) or not authority.permitted():
        raise ConflictError(detail="Search configuration changed")
    return await _dedupe_or_create(
        session, mapping, config.model_identity, scope=scope,
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
                isinstance(exc, (DomainError, HTTPException)) and exc.status_code in {401, 403, 404, 409}
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
    factory = worker_session_factory(ctx)
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
    except (DomainError, HTTPException) as exc:
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
            and source.rag_index_enabled
            and current == content
            and (source_generation is None or generation == source_generation))
    config, mapping, policy = await configured_embedding(session, settings, redis, scope=scope)
    if not original.unchanged(config, mapping, policy):
        raise _AuthorityChanged
    return fence, live, generation


@dataclass(frozen=True)
class _RunContext:
    """Immutable per-invocation inputs shared by the indexing helpers.

    ``original`` is the one AccessFence/config snapshot captured at the start of the run; every
    short transaction compares against it and discards stale output instead of rebasing.
    """

    factory: async_sessionmaker[AsyncSession]
    redis: Redis
    settings: Settings
    scope: InternalJobScope
    original: EmbeddingAuthority
    generation_id: UUID
    workspace_id: UUID

    @property
    def multi_workspace_enabled(self) -> bool:
        """Return the deployment's multi-workspace flag used by every admission call."""
        return self.settings.multi_workspace_enabled


@dataclass
class _RunState:
    """Mutable loop bookkeeping: completed count, idle flag, claim keyset and event coalescing."""

    completed: int = 0
    idle: bool = False
    last_chunk: UUID | None = None
    last_emit: float = float("-inf")
    unemitted: int = 0


@dataclass(frozen=True)
class _ClaimedChunk:
    """One chunk claimed for embedding together with its item row id and target dimensions."""

    chunk_id: UUID
    content: str
    source_id: UUID
    item_id: UUID
    dimensions: int | None


async def _prepare_index_run(
    factory: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings,
    generation_id: UUID, workspace_id: UUID,
) -> tuple[_RunContext | None, int | None]:
    """Capture the original authority for one run and validate the generation against it.

    Returns ``(context, None)`` when the batch loop should run, otherwise ``(None, result)``
    with the value ``_index_generation`` must return (``None`` skipped, ``0`` after marking a
    non-active generation failed). Uses one session: rollback on every skip path; a
    ``model_unavailable`` failure is committed through ``_commit_index_change``. A typed
    admission denial terminalizes the queued/running generation (separate session) and skips.
    """
    async with factory() as session:
        owner = await workspaces.resolve_workspace_owner_context(
            session, workspace_id, multi_workspace_enabled=settings.multi_workspace_enabled,
        )
        if owner is None:
            await session.rollback()
            return None, None
        scope = InternalJobScope(
            workspace_id=workspace_id, actor_user_id=owner.user_id,
            membership_revision=owner.membership_revision,
        )

        async def capture() -> EmbeddingAuthority | None:
            """Re-capture the embedding authority from a clean transaction; None if Search is off."""
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
        except (DomainError, HTTPException) as exc:
            await session.rollback()
            code = denial_code(exc)
            if code is None:
                raise
            # Denial is terminal for a queued/running generation; the active one keeps serving.
            await terminalize(
                factory, IndexGeneration, generation_id, workspace_id, code,
                from_status=("queued", "running"),
            )
            return None, None
        if captured is None:
            return None, None
        original = captured
        generation = await session.scalar(select(IndexGeneration).where(
            IndexGeneration.id == generation_id, IndexGeneration.workspace_id == workspace_id,
        ))
        if generation is None or not original.permitted():
            await session.rollback()
            return None, None
        mapping, config = original.mapping, original.config
        if (mapping is None or mapping.model != generation.model_id
                or mapping.version != generation.model_version
                or config.model_identity != generation.gateway_identity):  # column stores model_identity
            if generation.status != "active":
                before = await _index_projection(session, generation_id, scope=scope)
                generation.status, generation.error_code = "failed", "model_unavailable"
                await _commit_index_change(
                    session, before, generation_id=generation_id, scope=scope,
                    multi_workspace_enabled=settings.multi_workspace_enabled,
                    access_fence=original.fence,
                )
                return None, 0
            await session.rollback()
            return None, None
        await session.rollback()
    return _RunContext(factory, redis, settings, scope, original, generation_id, workspace_id), None


async def _finalize_exhausted_generation(
    session: AsyncSession, ctx: _RunContext, generation: IndexGeneration,
    before: IndexProjection | None, access_fence: AccessFence,
) -> None:
    """Finalize a running generation whose first (unfiltered) claim probe found nothing pending.

    Runs in the caller's locked session: prunes items whose chunk is no longer eligible, then marks
    the generation failed (``item_failed`` / ``no_indexable_chunks``) or activates it, and commits
    through ``_commit_index_change`` (which publishes the status change). A non-running generation
    is only committed (no state change). Activation first commits this transaction and builds the HNSW
    index with ``CREATE INDEX CONCURRENTLY`` on a separate autocommit connection (see
    ``_build_index_outside_transaction``); if the generation changed meanwhile the session is rolled back and
    nothing is published.
    """
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
            "AND s.status = 'active' AND s.local_only = false AND s.rag_index_enabled = true)"
        ), {"generation_id": ctx.generation_id, "workspace_id": str(ctx.workspace_id)})
        failed = await session.scalar(select(func.count()).select_from(SearchIndexItem).where(
            SearchIndexItem.generation_id == ctx.generation_id, SearchIndexItem.status != "succeeded",
        ))
        if failed:
            generation.status = "failed"
            generation.error_code = "item_failed"
        elif generation.dimensions is None:
            generation.status = "failed"
            generation.error_code = "no_indexable_chunks"
        else:
            relocked = await _build_index_outside_transaction(session, ctx, generation)
            if relocked is None:
                return  # state changed during the build; the generation stays running and is re-finalized later
            generation, access_fence = relocked
            await activate_generation(session, generation, scope=ctx.scope)
    await _commit_index_change(
        session, before, generation_id=ctx.generation_id, scope=ctx.scope,
        multi_workspace_enabled=ctx.multi_workspace_enabled, access_fence=access_fence,
    )


async def _claim_next_chunk(ctx: _RunContext, state: _RunState) -> _ClaimedChunk | None:
    """Claim the next pending chunk (keyset after ``state.last_chunk``) in one locked transaction.

    Lock order: access fence -> generation row. Returns ``None`` when the loop must stop: denial
    (401/403/404/409), Search disabled, generation gone/not claimable, nothing above the keyset,
    or nothing pending (``state.idle`` set when the generation is not running; a running one is
    finalized). Other admission errors propagate. Advances ``state.last_chunk`` and creates the
    ``SearchIndexItem`` row; the queued->running claim is published, per-chunk progress is not.
    """
    async with ctx.factory() as session:
        try:
            access_fence = await _admit(
                session, scope=ctx.scope, multi_workspace_enabled=ctx.multi_workspace_enabled,
                lock=True, expected=ctx.original.fence,
            )
        except (DomainError, HTTPException) as exc:
            await session.rollback()
            if exc.status_code in {401, 403, 404, 409}:
                return None
            raise
        if not await ai_settings.module_is_enabled(
            session, "search", scope=ctx.scope,
            multi_workspace_enabled=ctx.multi_workspace_enabled,
        ):
            await session.rollback()
            return None
        generation = await session.scalar(select(IndexGeneration).where(
            IndexGeneration.id == ctx.generation_id, IndexGeneration.workspace_id == ctx.workspace_id,
        ).with_for_update())
        if generation is None or generation.status not in {"queued", "running", "active"}:
            await session.rollback()
            return None
        claim_was_queued = generation.status == "queued"
        # Projection is needed to publish the queued->running claim or a first-probe finalize.
        before = (await _index_projection(session, ctx.generation_id, scope=ctx.scope)
                  if claim_was_queued or state.last_chunk is None else None)
        if claim_was_queued:
            generation.status = "running"
        claim = eligible_chunks(workspace_id=ctx.workspace_id).outerjoin(
            SearchIndexItem,
            (SearchIndexItem.chunk_id == DocumentChunk.id) & (SearchIndexItem.generation_id == ctx.generation_id),
        ).where((SearchIndexItem.id.is_(None)) | (SearchIndexItem.status == "pending"))
        if state.last_chunk is not None:
            claim = claim.where(DocumentChunk.id > state.last_chunk)
        row = (await session.execute(claim.order_by(DocumentChunk.id).limit(1))).first()
        if row is None and state.last_chunk is not None:
            # Chunk ids are random UUIDs and items may remain pending, so "nothing above the
            # keyset" does not mean "nothing pending": never finalize here; the next
            # invocation's first (unfiltered) probe decides finalize/idle.
            await session.rollback()
            return None
        if row is None and generation.status != "running":
            await session.rollback()
            state.idle = True  # nothing pending: let the page scan move on
            return None
        if row is None:
            await _finalize_exhausted_generation(session, ctx, generation, before, access_fence)
            return None
        chunk_id, content, source_id = row
        state.last_chunk = chunk_id
        item = await session.scalar(select(SearchIndexItem).where(
            SearchIndexItem.generation_id == ctx.generation_id, SearchIndexItem.chunk_id == chunk_id,
        ))
        if item is None:
            item = SearchIndexItem(generation_id=ctx.generation_id, chunk_id=chunk_id)
            session.add(item)
            await session.flush()
        claimed = _ClaimedChunk(chunk_id, content, source_id, item.id, generation.dimensions)
        await _commit_index_change(
            session, before, generation_id=ctx.generation_id, scope=ctx.scope,
            multi_workspace_enabled=ctx.multi_workspace_enabled, access_fence=access_fence,
            publish=claim_was_queued,
        )
    return claimed


async def _embed_claimed_chunk(
    ctx: _RunContext, claimed: _ClaimedChunk,
) -> tuple[list[float], str | None, int | None] | None:
    """Embed one claimed chunk with no DB transaction open during the gateway call.

    Returns ``(values, returned_model, source_generation)``, or ``None`` after dropping a pending
    item whose Source/chunk is no longer eligible. A fresh short access/Source/chunk/config check
    runs before the actual request via ``before_send``; authority loss raises
    ``PrivacyPolicyDenied`` chained to ``_AuthorityChanged`` / ``_SourceStale``.
    """
    # Short preparation transaction: access -> Source -> chunk, then release every lock.
    async with ctx.factory() as session:
        _fence, live, source_generation = await _recheck_prepared(
            session, scope=ctx.scope, settings=ctx.settings, redis=ctx.redis, original=ctx.original,
            source_id=claimed.source_id, source_generation=None, chunk_id=claimed.chunk_id,
            content=claimed.content, workspace_id=ctx.workspace_id, lock=False,
        )
        await session.rollback()
    if not live or source_generation is None:
        await _drop_pending_item(
            ctx.factory, scope=ctx.scope, settings=ctx.settings, original=ctx.original,
            item_id=claimed.item_id, generation_id=ctx.generation_id, workspace_id=ctx.workspace_id,
        )
        return None

    async def recheck_send() -> None:
        """Fresh short access/Source/chunk/config check before each actual request."""
        async with ctx.factory() as check:
            try:
                _f, still_live, _g = await _recheck_prepared(
                    check, scope=ctx.scope, settings=ctx.settings, redis=ctx.redis, original=ctx.original,
                    source_id=claimed.source_id, source_generation=source_generation,
                    chunk_id=claimed.chunk_id, content=claimed.content, workspace_id=ctx.workspace_id,
                    lock=False,
                )
            except _AuthorityChanged as exc:
                raise PrivacyPolicyDenied("Embedding send denied by current authority") from exc
            finally:
                await check.rollback()
            if not still_live:
                raise PrivacyPolicyDenied("Embedding source changed before send") from _SourceStale()

    mapping = ctx.original.mapping
    response = await gateway(ctx.original.config, ctx.redis, recheck_send, scope=ctx.scope).embed(
        "embedding", mapping, ctx.original.policy, [claimed.content],
    )
    values, returned_model = embedding_values(response, claimed.dimensions)
    return values, returned_model, source_generation


async def _publish_embedding(
    ctx: _RunContext, state: _RunState, claimed: _ClaimedChunk, source_generation: int | None,
    values: list[float], returned_model: str | None,
) -> bool:
    """Publish one embedding under the ordered locks; return False when the loop must stop.

    Lock order: access -> Source -> chunk -> generation -> item (re-verified against the original
    fence). Stale output (source gone, generation/item missing or no longer running) is discarded
    by deleting the item. A returned-model identity change fails the generation (always
    published) and stops the loop. Progress events are coalesced via ``state``; raises
    ``ValueError`` if the vector dimensions changed.
    """
    # Pre-serialize the pgvector literal before acquiring publication locks so JSON
    # encoding of up to 2,000 floats does not extend FOR UPDATE lock hold time.
    embedding_literal = _vector_literal(values)
    # Publication: same lock order (access -> Source -> chunk -> generation/item).
    async with ctx.factory() as session:
        fence, live, _g = await _recheck_prepared(
            session, scope=ctx.scope, settings=ctx.settings, redis=ctx.redis, original=ctx.original,
            source_id=claimed.source_id, source_generation=source_generation, chunk_id=claimed.chunk_id,
            content=claimed.content, workspace_id=ctx.workspace_id, lock=True,
        )
        generation = await session.scalar(select(IndexGeneration).where(
            IndexGeneration.id == ctx.generation_id, IndexGeneration.workspace_id == ctx.workspace_id,
        ).with_for_update())
        item = await session.scalar(select(SearchIndexItem).join(
            IndexGeneration, IndexGeneration.id == SearchIndexItem.generation_id,
        ).where(
            SearchIndexItem.id == claimed.item_id, IndexGeneration.workspace_id == ctx.workspace_id,
        ).with_for_update())
        if not live or generation is None or item is None or generation.status not in {"running", "active"}:
            if item is not None:
                before = await _index_projection(session, item.generation_id, scope=ctx.scope)
                await session.delete(item)
                await _commit_index_change(
                    session, before, generation_id=item.generation_id, scope=ctx.scope,
                    multi_workspace_enabled=ctx.multi_workspace_enabled, access_fence=fence,
                )
            else:
                await session.rollback()
            return True
        now = time.monotonic()
        publish = state.unemitted + 1 >= INDEX_EVENT_ITEMS or now - state.last_emit >= INDEX_EVENT_SECONDS
        before = (await _index_projection(session, ctx.generation_id, scope=ctx.scope)
                  if publish else None)
        if generation.dimensions is None:
            generation.response_model_id = returned_model
            generation.dimensions = len(values)
        elif returned_model != generation.response_model_id:
            generation.status = "failed"
            generation.error_code = "model_identity_changed"
            item.status = "failed"
            item.error_code = "model_identity_changed"
            # Status change: always published (before=None differs from any projection).
            await _commit_index_change(
                session, before, generation_id=ctx.generation_id, scope=ctx.scope,
                multi_workspace_enabled=ctx.multi_workspace_enabled, access_fence=fence,
            )
            return False
        elif generation.dimensions != len(values):
            raise ValueError("Embedding dimensions changed during indexing")
        await session.execute(text("UPDATE search_index_items SET embedding = CAST(:embedding AS vector) WHERE id = :item_id"),
                              {"embedding": embedding_literal, "item_id": claimed.item_id})
        item.status = "succeeded"
        item.error_code = None
        await _commit_index_change(
            session, before, generation_id=ctx.generation_id, scope=ctx.scope,
            multi_workspace_enabled=ctx.multi_workspace_enabled, access_fence=fence,
            publish=publish,
        )
        state.completed += 1
        if publish:
            state.unemitted, state.last_emit = 0, now
        else:
            state.unemitted += 1
    return True


async def _process_claimed_chunk(ctx: _RunContext, state: _RunState, claimed: _ClaimedChunk) -> bool:
    """Embed and publish one claimed chunk, mapping failures to the loop's continue/stop decision.

    Returns True to continue with the next chunk, False to stop the batch. Authority changes and
    401/403/404/409 admission denials stop (the item stays pending for a fresh invocation); a
    stale Source drops the item; gateway/Redis/value errors fail the item; other errors propagate.
    """
    try:
        embedded = await _embed_claimed_chunk(ctx, claimed)
        if embedded is None:
            return True
        values, returned_model, source_generation = embedded
        return await _publish_embedding(ctx, state, claimed, source_generation, values, returned_model)
    except _AuthorityChanged:
        return False  # stale output discarded; the item stays pending for a fresh invocation
    except (DomainError, HTTPException) as exc:
        if exc.status_code in {401, 403, 404, 409}:
            return False
        raise
    except PrivacyPolicyDenied as exc:
        if isinstance(exc.__cause__, _AuthorityChanged):
            return False  # leave pending for a fresh invocation
        if isinstance(exc.__cause__, _SourceStale):
            await _drop_pending_item(
                ctx.factory, scope=ctx.scope, settings=ctx.settings, original=ctx.original,
                item_id=claimed.item_id, generation_id=ctx.generation_id, workspace_id=ctx.workspace_id,
            )
            return True
        await _fail_item(
            ctx.factory, scope=ctx.scope, settings=ctx.settings, original=ctx.original,
            item_id=claimed.item_id, workspace_id=ctx.workspace_id,
        )
    except (ModelGatewayError, RedisError, ValueError):
        await _fail_item(
            ctx.factory, scope=ctx.scope, settings=ctx.settings, original=ctx.original,
            item_id=claimed.item_id, workspace_id=ctx.workspace_id,
        )
    return True


def embedding_batch_values(
    response: object, count: int, expected_dimensions: int | None = None,
) -> tuple[list[list[float]], str | None]:
    """Validate a ``count``-input embedding response and return the vectors in input order plus the model.

    Every row passes the same finite/nonzero/dimension checks as ``embedding_values``. Rows are ordered by
    their ``index`` when all rows carry an integer one, else they must already be in input order. Raises
    ``ValueError`` for a wrong row count, duplicate/missing indexes or any invalid row, so a malformed
    batch never publishes a partial result.
    """
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, list) or len(data) != count or count < 1:
        raise ValueError("Invalid embedding response")
    indexes = [row.get("index") if isinstance(row, dict) else None for row in data]
    if all(isinstance(i, int) and not isinstance(i, bool) for i in indexes):
        if sorted(cast(list[int], indexes)) != list(range(count)):
            raise ValueError("Invalid embedding response")
        data = [row for _, row in sorted(zip(cast(list[int], indexes), data, strict=True), key=lambda p: p[0])]
    model = cast(dict[str, object], response).get("model")
    vectors: list[list[float]] = []
    for row in data:
        values, _ = embedding_values({"data": [row], "model": model}, expected_dimensions)
        vectors.append(values)
        expected_dimensions = len(values)  # all rows of one response share one dimensionality
    return vectors, cast(str | None, model)


@dataclass(frozen=True)
class _EmbeddedBatch:
    """Vectors for the live chunks of one batch (parallel lists) and the response's model identity."""

    chunks: list[_ClaimedChunk]
    generations: list[int | None]
    vectors: list[list[float]]
    returned_model: str | None


def embed_batch_size(settings: object) -> int:
    """Configured chunks per embedding request, clamped to 1..128 (1 selects the single-chunk path)."""
    value = getattr(settings, "search_embed_batch_size", 1)
    return max(1, min(value, MAX_EMBED_BATCH)) if isinstance(value, int) else 1


async def _claim_chunk_batch(ctx: _RunContext, state: _RunState, limit: int) -> list[_ClaimedChunk]:
    """Claim up to ``limit`` pending chunks (one keyset query, ascending id) in one locked transaction.

    Lock order and stop conditions match ``_claim_next_chunk``: access fence -> generation row; an empty
    list means the loop must stop (denial, Search disabled, generation gone, nothing above the keyset, or
    nothing pending, in which case ``state.idle``/finalize apply exactly as for a single claim). Creates
    the missing ``SearchIndexItem`` rows in one flush, advances ``state.last_chunk`` to the batch's last
    id and publishes only the queued->running transition.
    """
    async with ctx.factory() as session:
        try:
            access_fence = await _admit(
                session, scope=ctx.scope, multi_workspace_enabled=ctx.multi_workspace_enabled,
                lock=True, expected=ctx.original.fence,
            )
        except (DomainError, HTTPException) as exc:
            await session.rollback()
            if exc.status_code in {401, 403, 404, 409}:
                return []
            raise
        if not await ai_settings.module_is_enabled(
            session, "search", scope=ctx.scope, multi_workspace_enabled=ctx.multi_workspace_enabled,
        ):
            await session.rollback()
            return []
        generation = await session.scalar(select(IndexGeneration).where(
            IndexGeneration.id == ctx.generation_id, IndexGeneration.workspace_id == ctx.workspace_id,
        ).with_for_update())
        if generation is None or generation.status not in {"queued", "running", "active"}:
            await session.rollback()
            return []
        claim_was_queued = generation.status == "queued"
        before = (await _index_projection(session, ctx.generation_id, scope=ctx.scope)
                  if claim_was_queued or state.last_chunk is None else None)
        if claim_was_queued:
            generation.status = "running"
        claim = eligible_chunks(workspace_id=ctx.workspace_id).outerjoin(
            SearchIndexItem,
            (SearchIndexItem.chunk_id == DocumentChunk.id) & (SearchIndexItem.generation_id == ctx.generation_id),
        ).where((SearchIndexItem.id.is_(None)) | (SearchIndexItem.status == "pending"))
        if state.last_chunk is not None:
            claim = claim.where(DocumentChunk.id > state.last_chunk)
        rows = list((await session.execute(claim.order_by(DocumentChunk.id).limit(limit))).all())
        if not rows:
            if state.last_chunk is not None:
                await session.rollback()  # keyset exhausted, not necessarily nothing pending (see single claim)
            elif generation.status != "running":
                await session.rollback()
                state.idle = True
            else:
                await _finalize_exhausted_generation(session, ctx, generation, before, access_fence)
            return []
        state.last_chunk = rows[-1][0]
        known = {item.chunk_id: item for item in await session.scalars(select(SearchIndexItem).where(
            SearchIndexItem.generation_id == ctx.generation_id,
            SearchIndexItem.chunk_id.in_([row[0] for row in rows]),
        ))}
        for chunk_id, _content, _source in rows:
            if chunk_id not in known:
                known[chunk_id] = SearchIndexItem(generation_id=ctx.generation_id, chunk_id=chunk_id)
                session.add(known[chunk_id])
        await session.flush()
        claimed = [_ClaimedChunk(cid, content, sid, known[cid].id, generation.dimensions) for cid, content, sid in rows]
        await _commit_index_change(
            session, before, generation_id=ctx.generation_id, scope=ctx.scope,
            multi_workspace_enabled=ctx.multi_workspace_enabled, access_fence=access_fence,
            publish=claim_was_queued,
        )
    return claimed


async def _recheck_batch(
    ctx: _RunContext, chunks: list[_ClaimedChunk], generations: list[int | None],
) -> tuple[list[bool], list[int | None]]:
    """Revalidate every chunk of a batch in ONE short non-locking session (access -> Source -> chunk order).

    Returns per-chunk liveness and the Source generation observed; raises ``_AuthorityChanged`` when the
    access fence, module or configuration moved. Rolls back before returning, so no lock outlives the call.
    """
    live: list[bool] = []
    seen: list[int | None] = []
    async with ctx.factory() as session:
        try:
            for chunk, generation in zip(chunks, generations, strict=True):
                _fence, ok, source_generation = await _recheck_prepared(
                    session, scope=ctx.scope, settings=ctx.settings, redis=ctx.redis, original=ctx.original,
                    source_id=chunk.source_id, source_generation=generation, chunk_id=chunk.chunk_id,
                    content=chunk.content, workspace_id=ctx.workspace_id, lock=False,
                )
                live.append(ok and source_generation is not None)
                seen.append(source_generation)
        finally:
            await session.rollback()
    return live, seen


async def _embed_chunk_batch(
    ctx: _RunContext, claimed: list[_ClaimedChunk], survivors: list[_ClaimedChunk] | None = None,
) -> _EmbeddedBatch | None:
    """Embed the live chunks of a claim with one gateway request and no DB transaction open during it.

    Chunks whose Source/chunk is no longer eligible have their pending item dropped. Returns ``None`` when
    nothing is live. A fresh batch-wide recheck runs as ``before_send``; any authority loss raises
    ``PrivacyPolicyDenied`` chained to ``_AuthorityChanged`` and a stale chunk to ``_SourceStale`` (the
    caller falls back to per-chunk processing). ``ValueError`` signals a malformed response. When given,
    ``survivors`` is replaced in place by the chunks that were still live after the first recheck, so a
    fallback never re-sends chunks whose pending items were just dropped.
    """
    live, generations = await _recheck_batch(ctx, claimed, [None] * len(claimed))
    chunks: list[_ClaimedChunk] = []
    kept: list[int | None] = []
    for chunk, ok, generation in zip(claimed, live, generations, strict=True):
        if ok:
            chunks.append(chunk)
            kept.append(generation)
        else:
            await _drop_pending_item(
                ctx.factory, scope=ctx.scope, settings=ctx.settings, original=ctx.original,
                item_id=chunk.item_id, generation_id=ctx.generation_id, workspace_id=ctx.workspace_id,
            )
    if survivors is not None:
        survivors[:] = chunks
    if not chunks:
        return None

    async def recheck_send() -> None:
        """Fresh short access/Source/chunk/config check for the whole batch before the actual request."""
        try:
            still_live, _g = await _recheck_batch(ctx, chunks, kept)
        except _AuthorityChanged as exc:
            raise PrivacyPolicyDenied("Embedding send denied by current authority") from exc
        if not all(still_live):
            raise PrivacyPolicyDenied("Embedding source changed before send") from _SourceStale()

    response = await gateway(ctx.original.config, ctx.redis, recheck_send, scope=ctx.scope).embed(
        "embedding", ctx.original.mapping, ctx.original.policy, [chunk.content for chunk in chunks],
    )
    vectors, returned_model = embedding_batch_values(response, len(chunks), chunks[0].dimensions)
    return _EmbeddedBatch(chunks, kept, vectors, returned_model)


async def _lock_batch_sources(
    session: AsyncSession, ctx: _RunContext, batch: _EmbeddedBatch,
) -> tuple[AccessFence, dict[UUID, bool]]:
    """Re-admit under lock and revalidate each chunk in canonical (Source id, chunk id) order.

    Sorting by Source id before locking keeps the access -> Source -> Document order identical for every
    concurrent publisher, so two multi-Source batches cannot deadlock. Returns the fence and
    ``{chunk id: still live}``; raises ``_AuthorityChanged`` from ``_recheck_prepared``.
    """
    order = sorted(
        range(len(batch.chunks)),
        key=lambda i: (str(batch.chunks[i].source_id), str(batch.chunks[i].chunk_id)),
    )
    fence: AccessFence | None = None
    live: dict[UUID, bool] = {}
    for i in order:
        chunk = batch.chunks[i]
        fence, ok, _g = await _recheck_prepared(
            session, scope=ctx.scope, settings=ctx.settings, redis=ctx.redis, original=ctx.original,
            source_id=chunk.source_id, source_generation=batch.generations[i], chunk_id=chunk.chunk_id,
            content=chunk.content, workspace_id=ctx.workspace_id, lock=True,
        )
        live[chunk.chunk_id] = ok
    if fence is None:
        raise _AuthorityChanged
    return fence, live


async def _publish_embedding_batch(ctx: _RunContext, state: _RunState, batch: _EmbeddedBatch) -> bool:
    """Publish a whole batch in ONE transaction; return False when the loop must stop.

    Lock order: access -> Sources (sorted) -> Documents -> generation -> items (sorted by id), all
    re-verified against the original fence. Stale output (chunk no longer live, generation/item gone or
    not running/active) deletes its item. A returned-model identity change fails the generation and every
    item of the batch (published) and stops; a dimension change raises ``ValueError``. Live vectors are
    written with a single executemany UPDATE, and progress events are coalesced via ``state``.
    """
    literals = [_vector_literal(values) for values in batch.vectors]  # serialize before taking locks
    async with ctx.factory() as session:
        fence, live = await _lock_batch_sources(session, ctx, batch)
        generation = await session.scalar(select(IndexGeneration).where(
            IndexGeneration.id == ctx.generation_id, IndexGeneration.workspace_id == ctx.workspace_id,
        ).with_for_update())
        items = {item.id: item for item in await session.scalars(select(SearchIndexItem).join(
            IndexGeneration, IndexGeneration.id == SearchIndexItem.generation_id,
        ).where(
            SearchIndexItem.id.in_([chunk.item_id for chunk in batch.chunks]),
            IndexGeneration.workspace_id == ctx.workspace_id,
        ).order_by(SearchIndexItem.id).with_for_update())}
        running = generation is not None and generation.status in {"running", "active"}
        writes: list[dict[str, object]] = []
        stale: list[SearchIndexItem] = []
        for chunk, literal in zip(batch.chunks, literals, strict=True):
            item = items.get(chunk.item_id)
            if item is None:
                continue
            if live[chunk.chunk_id] and running:
                writes.append({"embedding": literal, "item_id": chunk.item_id})
            else:
                stale.append(item)
        now = time.monotonic()
        publish = state.unemitted + len(writes) >= INDEX_EVENT_ITEMS or now - state.last_emit >= INDEX_EVENT_SECONDS
        before = (await _index_projection(session, ctx.generation_id, scope=ctx.scope)
                  if publish or stale else None)
        for item in stale:
            await session.delete(item)
        if writes and generation is not None:
            dimensions = len(batch.vectors[0])
            if generation.dimensions is None:
                generation.response_model_id, generation.dimensions = batch.returned_model, dimensions
            elif batch.returned_model != generation.response_model_id:
                generation.status, generation.error_code = "failed", "model_identity_changed"
                for write in writes:
                    failed_item = items[cast(UUID, write["item_id"])]
                    failed_item.status, failed_item.error_code = "failed", "model_identity_changed"
                await _commit_index_change(
                    session, before, generation_id=ctx.generation_id, scope=ctx.scope,
                    multi_workspace_enabled=ctx.multi_workspace_enabled, access_fence=fence,
                )
                return False
            elif generation.dimensions != dimensions:
                raise ValueError("Embedding dimensions changed during indexing")
            await session.execute(
                text("UPDATE search_index_items SET embedding = CAST(:embedding AS vector) WHERE id = :item_id"),
                writes,
            )
            for write in writes:
                done = items[cast(UUID, write["item_id"])]
                done.status, done.error_code = "succeeded", None
        await _commit_index_change(
            session, before, generation_id=ctx.generation_id, scope=ctx.scope,
            multi_workspace_enabled=ctx.multi_workspace_enabled, access_fence=fence,
            publish=publish or bool(stale),
        )
        state.completed += len(writes)
        if publish:
            state.unemitted, state.last_emit = 0, now
        else:
            state.unemitted += len(writes)
    return True


async def _fail_batch(ctx: _RunContext, claimed: list[_ClaimedChunk]) -> None:
    """Mark every item of a batch failed (each under the original fence, own short transaction)."""
    for chunk in claimed:
        await _fail_item(
            ctx.factory, scope=ctx.scope, settings=ctx.settings, original=ctx.original,
            item_id=chunk.item_id, workspace_id=ctx.workspace_id,
        )


async def _process_claimed_batch(ctx: _RunContext, state: _RunState, claimed: list[_ClaimedChunk]) -> bool:
    """Embed and publish one claimed batch; return True to continue, False to stop the run.

    Authority changes and 401/403/404/409 admission denials stop (items stay pending). A stale Source or a
    malformed response degrades to the per-chunk path for this batch only, so one bad chunk cannot fail
    its neighbours. Gateway/Redis errors fail every item of the batch (as the per-chunk path would, one
    request at a time); other errors propagate.
    """
    survivors = list(claimed)  # narrowed to the live chunks once the batch recheck has dropped the stale ones
    try:
        embedded = await _embed_chunk_batch(ctx, claimed, survivors)
        if embedded is None:
            return True
        return await _publish_embedding_batch(ctx, state, embedded)
    except _AuthorityChanged:
        return False
    except (DomainError, HTTPException) as exc:
        if exc.status_code in {401, 403, 404, 409}:
            return False
        raise
    except PrivacyPolicyDenied as exc:
        if isinstance(exc.__cause__, _AuthorityChanged):
            return False
        if not isinstance(exc.__cause__, _SourceStale):
            await _fail_batch(ctx, claimed)
            return True
    except ValueError:
        pass  # malformed response or dimension drift: isolate the offender chunk by chunk below
    except (ModelGatewayError, RedisError):
        await _fail_batch(ctx, claimed)
        return True
    for chunk in survivors:  # fallback: identical to the single-chunk pipeline, one request per chunk
        if not await _process_claimed_chunk(ctx, state, chunk):
            return False
    return True


async def _index_generation(
    factory: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings,
    generation_id: UUID, workspace_id: UUID,
) -> int | None:
    """Index a bounded batch of chunks (items and time) of one generation; None when skipped.

    Chunks are claimed in ascending id order via a keyset (``id > last_chunk``) so an item that
    stays pending (e.g. dropped under admission failure) is not re-claimed within the run.
    Progress events are coalesced; claim of a queued generation, finalize and failures publish.

    One original AccessFence/config snapshot is captured per invocation. No DB transaction or
    lock is open during ``embed``: preparation and the gateway ``before_send`` hook use short
    ordered sessions (access, Source, chunk), and publication reacquires the same order and
    compares the originals, discarding stale output instead of rebasing.
    """
    ctx, early = await _prepare_index_run(factory, redis, settings, generation_id, workspace_id)
    if ctx is None:
        return early
    state = _RunState()
    deadline = time.monotonic() + INDEX_RUN_SECONDS
    batch_size = embed_batch_size(settings)
    if batch_size > 1:
        # Batched path: loop while work remains, bounded by the time budget (it gates *starting* a batch).
        while state.completed < INDEX_RUN_BATCH_ITEMS and time.monotonic() < deadline:
            claimed_batch = await _claim_chunk_batch(ctx, state, batch_size)
            if not claimed_batch or not await _process_claimed_batch(ctx, state, claimed_batch):
                break
        return None if state.idle and state.completed == 0 else state.completed
    for _ in range(INDEX_RUN_ITEMS):
        if time.monotonic() >= deadline:
            break
        claimed = await _claim_next_chunk(ctx, state)
        if claimed is None:
            break
        if not await _process_claimed_chunk(ctx, state, claimed):
            break
    return None if state.idle and state.completed == 0 else state.completed


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
        except (DomainError, HTTPException):
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
        except (DomainError, HTTPException):
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


def vector_index_name(generation_id: UUID) -> str:
    """Return the per-generation partial HNSW index name (derived from a UUID, never request text)."""
    return f"ix_search_vector_{generation_id.hex}"


def vector_index_statements(generation_id: UUID, dimensions: int) -> tuple[str, str]:
    """Return ``(drop_invalid_leftover, create_concurrently)`` SQL for one generation's HNSW index.

    Identifier and dimensions originate from a UUID and a bounded integer (validated here), never from
    request text, so f-string interpolation is safe. The partial predicate matches ``_vector_ids`` so the
    planner can prove it.
    """
    if not 1 <= dimensions <= MAX_VECTOR_DIMENSIONS:
        raise ValueError("Invalid generation dimensions")
    name = vector_index_name(generation_id)
    drop = f"DROP INDEX CONCURRENTLY IF EXISTS {name}"
    create = (
        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON search_index_items "
        f"USING hnsw ((embedding::vector({dimensions})) vector_cosine_ops) "
        f"WHERE generation_id = '{generation_id}' AND status = 'succeeded'"
    )
    return drop, create


# Upper bound for one concurrent HNSW build, including its wait for older transactions to finish.
HNSW_BUILD_STATEMENT_TIMEOUT_MS = 2 * 60 * 60 * 1000


async def build_vector_index_concurrently(engine: AsyncEngine, generation_id: UUID, dimensions: int) -> None:
    """Build one generation's HNSW index with ``CREATE INDEX CONCURRENTLY`` on its own autocommit connection.

    Takes no lock that blocks writes to ``search_index_items`` (the previous in-transaction build did) but must
    not be called while the caller's own transaction holds writes on that table, because the concurrent build
    waits for them. The statement timeout is raised to ``HNSW_BUILD_STATEMENT_TIMEOUT_MS`` (finite, 2 h) and the lock timeout lifted for the build and reset afterwards. An *invalid*
    index left by an earlier interrupted build is dropped first (``IF NOT EXISTS`` would silently keep it),
    and a build that fails here drops its own invalid leftover before re-raising so a retry starts clean.

    The connection comes from ``core.database.direct_engine(engine)``: the build holds a session-level advisory
    lock and session ``SET``/``RESET`` across several autocommit statements, which a transaction pooler would
    hand to other clients between statements. In PgBouncer mode that is the attached direct (unpooled) engine;
    in direct mode it is ``engine`` itself.
    """
    drop, create = vector_index_statements(generation_id, dimensions)
    name = vector_index_name(generation_id)
    async with direct_engine(engine).connect() as raw:
        connection = await raw.execution_options(isolation_level="AUTOCOMMIT")
        locked = False
        try:
            # Finite on purpose (was 0 = unbounded): the build waits for older open transactions, so an
            # idle-in-transaction session would otherwise hang it forever. On timeout the invalid leftover is
            # dropped below and the next indexing pass retries. See docs/operations.md "Search HNSW indexes".
            await connection.execute(text(f"SET statement_timeout = {HNSW_BUILD_STATEMENT_TIMEOUT_MS}"))
            await connection.execute(text("SET lock_timeout = 0"))
            # Session-level advisory lock keyed by the index name serializes builders of the same generation
            # (the generation row lock is released before the build). Without it a second finalize would see the
            # first builder's in-progress (indisvalid = false) index as a leftover and drop it. Blocking is
            # intended: after the first builder finishes, the probe below finds a valid index and the create is a no-op.
            await connection.execute(text("SELECT pg_advisory_lock(hashtext(:name))"), {"name": name})
            locked = True
            invalid = (await connection.execute(text(
                "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = :name AND NOT i.indisvalid"
            ), {"name": name})).first()
            if invalid is not None:
                await connection.execute(text(drop))
            try:
                await connection.execute(text(create))
            except BaseException:
                # A failed/cancelled concurrent build leaves an INVALID index behind; remove it best-effort
                # (we still hold the advisory lock, so no other builder's index can be dropped here).
                try:
                    await connection.execute(text(drop))
                except Exception:  # cleanup must not mask the build failure
                    logger.warning("Could not drop invalid HNSW index %s after a failed build", name, exc_info=True)
                raise
        finally:
            # Pooled connection: release the session lock and timeouts, but never let a broken/cancelled
            # connection mask the original error; discard it instead so the pool cannot reuse dirty state.
            try:
                if locked:
                    await connection.execute(text("SELECT pg_advisory_unlock(hashtext(:name))"), {"name": name})
                await connection.execute(text("RESET statement_timeout"))
                await connection.execute(text("RESET lock_timeout"))
            except BaseException:
                logger.warning("Could not reset HNSW build connection state; invalidating it", exc_info=True)
                try:
                    await connection.invalidate()
                except Exception:
                    logger.debug("Invalidate after failed reset also failed", exc_info=True)


def async_engine_for_session(session: AsyncSession) -> AsyncEngine:
    """Return the ``AsyncEngine`` behind ``session`` (bound directly or through an ``AsyncConnection``).

    ``AsyncEngine.engine`` is the *sync* engine, so it must never be read off an engine bind.

    Raises:
        TypeError: the session is not bound to an async engine/connection.
    """
    bind = session.bind
    if isinstance(bind, AsyncEngine):
        return bind
    if isinstance(bind, AsyncConnection):
        return bind.engine
    raise TypeError("Concurrent index build requires an AsyncEngine-bound session")


async def _build_index_outside_transaction(
    session: AsyncSession, ctx: _RunContext, generation: IndexGeneration,
) -> tuple[IndexGeneration, AccessFence] | None:
    """Commit, build the generation's HNSW index concurrently, then re-lock and re-validate for activation.

    Order: (1) commit the caller's transaction (releases the access-fence and generation locks and the
    write locks the concurrent build would otherwise wait on forever), (2) ``CREATE INDEX CONCURRENTLY`` on a
    separate autocommit connection, (3) new transaction: access fence (compared with the run's original
    fence) then generation row ``FOR UPDATE`` -- the same lock order as a claim. Returns the re-locked
    generation and fence, or ``None`` after a rollback when admission is denied, the generation is no longer
    running with the same dimensions, or unfinished items appeared meanwhile (a later pass claims them and
    finalizes again; the finished index is reused by ``IF NOT EXISTS``).
    """
    dimensions = generation.dimensions
    if dimensions is None:
        return None
    engine = async_engine_for_session(session)  # resolve before commit so a misconfiguration fails without side effects
    await session.commit()
    await build_vector_index_concurrently(engine, ctx.generation_id, dimensions)
    try:
        access_fence = await _admit(
            session, scope=ctx.scope, multi_workspace_enabled=ctx.multi_workspace_enabled,
            lock=True, expected=ctx.original.fence,
        )
    except (DomainError, HTTPException) as exc:
        await session.rollback()
        if exc.status_code in {401, 403, 404, 409}:
            return None
        raise
    # populate_existing: the commit above kept the old instance (expire_on_commit=False), so refresh it
    # from the row we now hold the lock on instead of trusting pre-build attribute values.
    current = await session.scalar(select(IndexGeneration).where(
        IndexGeneration.id == ctx.generation_id, IndexGeneration.workspace_id == ctx.workspace_id,
    ).with_for_update().execution_options(populate_existing=True))
    unfinished = await session.scalar(select(func.count()).select_from(SearchIndexItem).where(
        SearchIndexItem.generation_id == ctx.generation_id, SearchIndexItem.status != "succeeded",
    )) if current is not None else 0
    if current is None or current.status != "running" or current.dimensions != dimensions or unfinished:
        await session.rollback()
        return None
    return current, access_fence


async def activate_generation(
    session: AsyncSession, generation: IndexGeneration, *, scope: Scope,
) -> None:
    """Retire only the admitted workspace's active predecessor and mark ``generation`` active.

    The generation's HNSW index must already exist: it is built before this call by
    ``build_vector_index_concurrently`` (``CREATE INDEX CONCURRENTLY`` cannot run inside this transaction
    and a transactional build would block writes to ``search_index_items``). Locks the predecessor row
    ``FOR UPDATE`` inside the caller's transaction; the caller commits.
    """
    if generation.dimensions is None or not 1 <= generation.dimensions <= MAX_VECTOR_DIMENSIONS:
        raise ValueError("Invalid generation dimensions")
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


async def purge_version_embeddings(session: AsyncSession, document_version_id: UUID) -> None:
    """Delete every generation's index items for the chunks of one (superseded) document version.

    Runs in the caller's transaction and takes no locks beyond the deletes. Scope is exactly the
    chunks of ``document_version_id`` (served by the ``search_index_items(chunk_id)`` index), so
    other versions, documents and workspaces are untouched; the version id is workspace-owned.
    """
    await session.execute(text(
        "DELETE FROM search_index_items WHERE chunk_id IN "
        "(SELECT id FROM document_chunks WHERE document_version_id = :v)"
    ), {"v": document_version_id})


documents.register_version_superseded_hook(purge_version_embeddings)
