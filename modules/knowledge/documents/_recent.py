"""Narrow owner read of a Source's newest Documents for scheduled-task posts (no content, no cursor)."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.schemas import Scope
from modules.knowledge.documents._scope import _admit_document_scope, _document_scope
from modules.knowledge.documents.models import Document

MAX_RECENT_DOCUMENTS = 50
_SITE_CHARS = 255
_SUMMARY_CHARS = 400


@dataclass(frozen=True)
class DocumentBrief:
    """Display-only view of one Document: id, title, URL, site and a short summary, never body text."""

    id: UUID
    title: str
    canonical_url: str | None
    site: str | None
    summary: str | None
    created_at: datetime


def _text(value: object, limit: int) -> str | None:
    """Return ``value`` as a bounded non-empty string, else None (metadata is untrusted JSON)."""
    return value[:limit] if isinstance(value, str) and value else None


async def list_recent_source_documents(
    session: AsyncSession,
    source_id: UUID,
    since: datetime,
    limit: int,
    *,
    until: datetime | None = None,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[DocumentBrief]:
    """Return up to ``limit`` (1..50) Documents of ``source_id`` created at or after ``since`` (and at or before ``until`` when given), newest first.

    Unlike ``list_documents`` this accepts the owner's ``InternalJobScope`` so background sweeps can
    read: it admits the scope as the owner (members and foreign sources see nothing), applies the
    same workspace/Source/data-purge predicate before the limit, and reads no locks. Only the
    ``summary`` and ``site`` metadata keys are projected, capped to 400 and 255 characters; content
    never leaves Documents. Raises ``ValueError`` for an out-of-range ``limit``.
    """
    if not 1 <= limit <= MAX_RECENT_DOCUMENTS:
        raise ValueError("Recent document limit must be between 1 and 50")
    await _admit_document_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    rows = await session.execute(
        select(
            Document.id,
            Document.title,
            Document.canonical_url,
            Document.metadata_json,
            Document.created_at,
        )
        .where(
            Document.source_id == source_id,
            Document.created_at >= since,
            *([Document.created_at <= until] if until is not None else []),
            *_document_scope(scope),
        )
        .order_by(desc(Document.created_at), desc(Document.id))
        .limit(limit)
    )
    return [
        DocumentBrief(
            id=row.id,
            title=row.title,
            canonical_url=row.canonical_url,
            site=_text((row.metadata_json or {}).get("site"), _SITE_CHARS),
            summary=_text((row.metadata_json or {}).get("summary"), _SUMMARY_CHARS),
            created_at=row.created_at,
        )
        for row in rows
    ]
