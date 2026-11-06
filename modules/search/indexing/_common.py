"""Shared constants, embedding-authority capture and index-change publication used by every indexing stage."""

import json
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from core.errors import ConflictError, ForbiddenError
from core.model_gateway.client import ModelGateway
from core.model_gateway.policy import may_send
from core.model_gateway.schemas import AIExecutionConfig, ModelMapping, RequestPolicy
from core.realtime import commit_with_replay, make_index_change
from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, Scope, WorkspaceContext
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


INDEX_RUN_BATCH_ITEMS = (
    2048  # chunk cap per invocation on the batched path (the time budget usually binds first)
)


INDEX_RUN_SECONDS = 45


# Per-chunk progress events are coalesced: clients only invalidate queries / poll, and status
# changes (claim of a queued generation, finalize, failure) are always published.
INDEX_EVENT_ITEMS = 100


INDEX_EVENT_SECONDS = 5.0


SCAN_PAGE = 20  # identities scanned per status class per invocation; bounds skip cost


AUTO_INDEX_RETRY_DELAY = timedelta(minutes=15)


logger = logging.getLogger("modules.search.indexing")


async def _admit(
    session: AsyncSession,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    lock: bool = False,
    expected: AccessFence | None = None,
) -> AccessFence:
    """Admit Search access before its own roots, with optional ordered publication locking."""
    if isinstance(scope, WorkspaceContext) and scope.role != "owner":
        raise ForbiddenError(detail="Workspace owner required")
    if lock:
        return await workspaces.lock_access_fence(
            session,
            scope=scope,
            expected=expected,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    fence = await workspaces.read_access_fence(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
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
    session: AsyncSession,
    generation_id: UUID,
    *,
    scope: Scope,
) -> IndexProjection | None:
    """Read a generation's lifecycle and succeeded/failed item counts."""
    row = await session.execute(
        select(IndexGeneration.id, IndexGeneration.status).where(
            IndexGeneration.id == generation_id, IndexGeneration.workspace_id == scope.workspace_id
        )
    )
    generation = row.one_or_none()
    if generation is None:
        return None
    counts = dict(
        (
            await session.execute(
                select(SearchIndexItem.status, func.count())
                .where(SearchIndexItem.generation_id == generation_id)
                .group_by(SearchIndexItem.status)
            )
        ).all()
    )
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
        drafts.append(
            make_index_change(
                after.generation_id,
                after.status,
                after.indexed_items,
                after.failed_items,
                scope=scope,
            )
        )
    await commit_with_replay(
        session,
        drafts,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )


def _vector_literal(values: list[float]) -> str:
    """Serialize a validated float vector into a pgvector literal outside locked transactions."""
    return json.dumps(values)


def embedding_values(
    response: object, expected_dimensions: int | None = None
) -> tuple[list[float], str | None]:
    """Validate one finite, nonzero, bounded embedding and return its model identity."""
    if (
        not isinstance(response, dict)
        or not isinstance(response.get("data"), list)
        or len(response["data"]) != 1
    ):
        raise ValueError("Invalid embedding response")
    returned_model = response.get("model")
    if returned_model is not None and (
        not isinstance(returned_model, str) or not returned_model.strip()
    ):
        raise ValueError("Embedding response model identity is invalid")
    row = response["data"][0]
    if not isinstance(row, dict) or not isinstance(row.get("embedding"), list):
        raise ValueError("Invalid embedding response")
    values = row["embedding"]
    if not 1 <= len(values) <= MAX_VECTOR_DIMENSIONS or expected_dimensions not in (
        None,
        len(values),
    ):
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
    config: AIExecutionConfig,
    redis: Redis,
    before_send: Callable[[], Awaitable[None]],
    *,
    scope: Scope,
) -> ModelGateway:
    """Construct the model gateway bound to one scope, config revision, identity and fresh send check."""
    config = ai_settings.derive_for_role(
        config, "embedding"
    )  # embedding is this module's only role
    return ModelGateway(
        redis,
        config.gateway_base_url,
        config.gateway_api_key,
        config.endpoint_destination_id or "remote",
        config.request_timeout_seconds,
        scope=scope,
        gateway_identity=config.gateway_identity,
        model_identity=config.model_identity,
        configuration_revision=config.configuration_revision,
        before_send=before_send,
        approved_endpoint_cidrs=config.endpoint_allowed_cidrs,
        allow_public_endpoints=config.allow_public_providers,
        kind=config.gateway_kind,
    )


async def configured_embedding(
    session: AsyncSession,
    settings: Settings,
    redis: Redis,
    *,
    scope: Scope,
) -> tuple[AIExecutionConfig, ModelMapping | None, RequestPolicy]:
    """Return scoped gateway config, embedding role and privacy-constrained policy."""
    config = ai_settings.derive_for_role(
        await ai_settings.get_ai_execution_config(session, settings, redis, scope=scope),
        "embedding",
    )
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
        return (
            self.mapping is not None
            and not self.config.endpoint_policy_denied
            and may_send(
                self.policy,
                "embedding",
                self.mapping,
                self.config.endpoint_destination_id or "remote",
                bool(self.config.gateway_api_key),
                "embeddings",
            )
        )

    def unchanged(
        self,
        config: AIExecutionConfig,
        mapping: ModelMapping | None,
        policy: RequestPolicy,
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
    session: AsyncSession,
    settings: Settings,
    redis: Redis,
    *,
    scope: Scope,
) -> EmbeddingAuthority:
    """Admit the owner scope first, then read config; return the detached original snapshot."""
    fence = await _admit(
        session,
        scope=scope,
        multi_workspace_enabled=settings.multi_workspace_enabled,
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
            Document.workspace_id == workspace_id,
            Source.workspace_id == workspace_id,
            Document.extraction_status.in_(("ready", "succeeded")),
            Source.status == "active",
            Source.local_only.is_(False),
            Source.rag_index_enabled.is_(True),
            or_(
                Source.configuration["skip_embedding"].as_boolean().is_(None),
                Source.configuration["skip_embedding"].as_boolean().is_(False),
            ),
        )
    )
