"""Run-scoped context/state types and the authority/source recheck shared by single and batched indexing."""

from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.errors import DomainError
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope
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
from modules.search.indexing._common import EmbeddingAuthority, _admit, configured_embedding
from modules.settings import public as ai_settings
from modules.sources import public as sources


class _SourceStale(Exception):  # control-flow signal: Source/chunk moved before the send
    """The Source or chunk is no longer eligible; the pending item should be dropped."""


class _AuthorityChanged(Exception):  # control-flow signal, not an error condition
    """The original fence, module or configuration no longer matches; discard the output."""


async def _recheck_prepared(
    session: AsyncSession,
    *,
    scope: Scope,
    settings: Settings,
    redis: Redis,
    original: EmbeddingAuthority,
    source_id: UUID,
    source_generation: int | None,
    chunk_id: UUID,
    content: str,
    workspace_id: UUID,
    lock: bool,
) -> tuple[AccessFence, bool, int | None]:
    """Re-admit against the original fence, then Source, then exact chunk, in lock order.

    Returns (fence, live, source_generation). ``live`` is False when the Source is no longer
    active/remote-eligible, its generation moved (when one was captured) or the chunk content
    changed. Raises _AuthorityChanged when the access fence, module or configuration changed.
    """
    try:
        fence = await _admit(
            session,
            scope=scope,
            multi_workspace_enabled=settings.multi_workspace_enabled,
            lock=lock,
            expected=original.fence,
        )
    except (DomainError, HTTPException) as exc:
        if exc.status_code in {401, 403, 404, 409}:
            raise _AuthorityChanged from exc
        raise
    if fence != original.fence or not await ai_settings.module_is_enabled(
        session,
        "search",
        scope=scope,
        multi_workspace_enabled=settings.multi_workspace_enabled,
    ):
        raise _AuthorityChanged
    source = await sources.lock_source(
        session,
        source_id,
        scope=scope,
        multi_workspace_enabled=settings.multi_workspace_enabled,
        expected_access_fence=fence,
    )
    if lock:  # publication: lock the Document (after Source) so a concurrent re-version serializes
        document_id = await session.scalar(
            select(DocumentVersion.document_id)
            .join(
                DocumentChunk,
                DocumentChunk.document_version_id == DocumentVersion.id,
            )
            .where(DocumentChunk.id == chunk_id)
        )
        if document_id is not None:
            await documents.lock_document_ids(
                session,
                [document_id],
                scope=scope,
                multi_workspace_enabled=settings.multi_workspace_enabled,
            )
    current = await session.scalar(
        select(DocumentChunk.content)
        .join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(
            DocumentChunk.id == chunk_id,
            Document.workspace_id == workspace_id,
            Document.current_version == DocumentVersion.version_number,
            Document.extraction_status.in_(("ready", "succeeded")),
        )
    )
    generation = source.generation if source is not None else None
    live = (
        source is not None
        and source.status == "active"
        and not source.local_only
        and source.rag_index_enabled
        and current == content
        and (source_generation is None or generation == source_generation)
    )
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
