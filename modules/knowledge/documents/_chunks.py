"""Chunk persistence, superseded-version hooks and the current-version chunk backfill."""

import logging
from collections.abc import Awaitable, Callable
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import (
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession

from core.chunking import chunk_text
from core.errors import DomainError
from core.heavy_work import to_thread_joined
from core.workspaces import public as workspaces
from modules.knowledge.documents._hashing import content_hash
from modules.knowledge.documents._ready import _publish_document_ready
from modules.knowledge.documents.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
)
from modules.sources import public as sources
from modules.sources.public import SourceReadProjection as Source

_log = logging.getLogger(__name__)


async def add_content_chunks(session: AsyncSession, version: DocumentVersion) -> int:
    """Chunk a version's content, add its searchable rows, and return their count."""
    await session.flush()
    drafts = await to_thread_joined(chunk_text, version.content)
    for index, draft in enumerate(drafts):
        session.add(
            DocumentChunk(
                document_version_id=version.id,
                chunk_index=index,
                content=draft.content,
                content_hash=content_hash(draft.content),
                token_count=draft.token_count,
                metadata_json=draft.metadata,
            )
        )
    return len(drafts)


_superseded_version_hooks: list[Callable[[AsyncSession, UUID], Awaitable[None]]] = []


def register_version_superseded_hook(hook: Callable[[AsyncSession, UUID], Awaitable[None]]) -> None:
    """Register a callback run in the caller's transaction when a document version stops being current.

    Search depends on Documents, so Documents cannot import it; derived-data owners register here
    (idempotently) at import time instead. Hooks must only delete derived rows for that version.
    """
    if hook not in _superseded_version_hooks:
        _superseded_version_hooks.append(hook)


async def _backfill_candidates(session: AsyncSession, limit: int) -> list[tuple[UUID, UUID, int]]:
    """Read a bounded window of current versions that still lack chunks.

    Read-only; returns ``(version_id, document_id, version_number)`` for active-source documents
    whose current version has content but no chunk, ordered by version id and capped at ``limit * 10``.

    """
    # Only the three identity columns are needed; selecting entities would load every version body.
    candidates = [
        (row.id, row.document_id, row.version_number)
        for row in (
            await session.execute(
                select(
                    DocumentVersion.id, DocumentVersion.document_id, DocumentVersion.version_number
                )
                .join(Document, Document.id == DocumentVersion.document_id)
                .join(Source, Source.id == Document.source_id)
                .where(
                    Document.current_version == DocumentVersion.version_number,
                    Document.extraction_status.in_(("ready", "succeeded")),
                    Source.status == "active",
                    DocumentVersion.content != "",
                    ~select(DocumentChunk.id)
                    .where(DocumentChunk.document_version_id == DocumentVersion.id)
                    .exists(),
                )
                .order_by(DocumentVersion.id)
                .limit(limit * 10)  # bounded window so denied lineages cannot starve others
            )
        ).all()
    ]
    return candidates


async def backfill_current_chunks(
    session: AsyncSession, limit: int = 2, *, multi_workspace_enabled: bool
) -> int:
    """Fill legacy manual revisions created before chunking was enabled.

    System path: each version's Source resolves its own internal job scope, then admission,
    Source and Document locks are taken in order and held through publish and commit. One
    transaction per version, so no two workspaces' locks are ever held together. A version
    whose lineage is denied or stale is skipped; nothing is rebased onto another actor.
    """
    candidates = await _backfill_candidates(session, limit)
    await session.rollback()
    done = 0
    for version_id, document_id, version_number in candidates:
        if done >= limit:
            break
        try:
            hint = await session.get(Document, document_id)
            scope = (
                (
                    await sources.resolve_source_job_scope(
                        session,
                        hint.source_id,
                        multi_workspace_enabled=multi_workspace_enabled,
                    )
                )
                if hint
                else None
            )
            if hint is None or scope is None:
                await session.rollback()
                _log.warning("chunk backfill skipped version %s: no admissible scope", version_id)
                continue
            access_fence = await workspaces.read_access_fence(
                session,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
            source = await sources.lock_source(
                session,
                hint.source_id,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
                expected_access_fence=access_fence,
            )
            document = await session.scalar(
                select(Document)
                .where(Document.id == document_id, Document.workspace_id == scope.workspace_id)
                .with_for_update()
            )
            version = await session.get(DocumentVersion, version_id)
            if (
                source is None
                or source.status != "active"
                or document is None
                or version is None
                or document.current_version != version_number
                or version.version_number != version_number
            ):
                await session.rollback()
                _log.warning("chunk backfill skipped version %s: stale lineage", version_id)
                continue
            if await add_content_chunks(session, version):
                await _publish_document_ready(
                    session,
                    document,
                    version,
                    scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled,
                    access_fence=access_fence,
                    source_fence=source,
                )
            await session.commit()
            done += 1
        except (HTTPException, DomainError) as exc:
            await session.rollback()
            if exc.status_code not in {401, 403, 404, 409}:
                raise
            _log.warning("chunk backfill skipped version %s: HTTP %s", version_id, exc.status_code)
    return done
