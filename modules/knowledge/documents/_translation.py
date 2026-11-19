"""Authorized translation input reader for knowledge documents (current ready version)."""

import hashlib
import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces import public as workspaces
from core.workspaces.schemas import WorkspaceContext
from modules.knowledge.documents.models import Document, DocumentVersion
from modules.sources.public import SourceReadProjection as Source
from modules.translations.public import TranslationInput

MAX_DOCUMENT_EXCERPT_CHARS = 2000


async def read_document_translation_input(
    session: AsyncSession,
    *,
    scope: WorkspaceContext,
    document_id: UUID,
    multi_workspace_enabled: bool,
) -> TranslationInput | None:
    """Return the authorized title and bounded excerpt of a ready document, or None when unreadable.

    Enforces workspace scoping, ready extraction status, and member share grants. The visibility hash
    binds document, version, source generation, share revision, membership revision, and ``local_only``
    so any permission or source-policy change invalidates cached translations.
    """
    del multi_workspace_enabled
    row = (
        await session.execute(
            select(Document, DocumentVersion, Source)
            .join(
                DocumentVersion,
                (DocumentVersion.document_id == Document.id)
                & (DocumentVersion.version_number == Document.current_version),
            )
            .join(
                Source,
                (Source.id == Document.source_id) & (Source.workspace_id == Document.workspace_id),
            )
            .where(
                Document.id == document_id,
                Document.workspace_id == scope.workspace_id,
                Document.extraction_status == "ready",
            )
        )
    ).first()
    if row is None:
        return None
    doc, version, source = row
    grant_revision = 0
    if scope.role == "member":
        grants = await workspaces.read_resource_grants(
            session,
            scope=scope,
            kind="document",
            resource_ids=(doc.id,),
        )
        if not grants:
            return None
        grant_revision = grants[0].share_revision

    visibility_tuple = (
        str(doc.id),
        str(version.id),
        str(source.id),
        source.generation,
        grant_revision,
        scope.membership_revision,
        bool(source.local_only),
    )
    excerpt = (version.content or "").strip()[:MAX_DOCUMENT_EXCERPT_CHARS]
    return TranslationInput(
        workspace_id=scope.workspace_id,
        actor_user_id=scope.user_id,
        resource_type="document",
        resource_id=doc.id,
        resource_revision=str(version.id),
        fields={"title": doc.title, "excerpt": excerpt},
        visibility_hash=hashlib.sha256(
            json.dumps(visibility_tuple, separators=(",", ":")).encode()
        ).hexdigest(),
        source_ids=(source.id,),
        local_only=bool(source.local_only),
    )
