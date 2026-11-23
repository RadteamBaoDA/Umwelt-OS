"""Owner-only ``live_event`` translation input built from the client's inline snapshot.

Live-map events live only in the browser and per-process API memory, so the owner's client sends
title/summary with the request. This module recomputes the revision server-side, authorizes the
owner only, and later serves the stored snapshot (``content_translations.source_text``) to the
worker. It imports models/schemas/service only (``public`` lazily) to keep the import graph acyclic.
"""

import hashlib
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.schemas import WorkspaceContext
from modules.translations.models import ContentTranslation
from modules.translations.schemas import LiveEventSource, TranslationInput, TranslationItemRequest
from modules.translations.service import content_hash

if TYPE_CHECKING:
    from modules.translations.public import ResourceAuthorization

__all__ = [
    "live_event_input",
    "live_event_revision",
    "read_live_event_translation_input",
    "request_verdict",
]


def live_event_revision(title: str, summary: str | None) -> str:
    """Return the sha256 hex revision of the exact text translated (title, newline, summary)."""
    return hashlib.sha256(f"{title}\n{summary or ''}".encode()).hexdigest()


def live_event_input(
    scope: WorkspaceContext, resource_id: UUID, source: LiveEventSource, revision: str
) -> TranslationInput:
    """Build the immutable translation input for an owner's snapshot; never local-only, no sources.

    The visibility hash binds the workspace and the owner-only audience so a changed audience
    cannot reuse a cached row.
    """
    visibility = hashlib.sha256(f"live_event:{scope.workspace_id}:owner".encode()).hexdigest()
    return TranslationInput(
        workspace_id=scope.workspace_id,
        actor_user_id=scope.user_id,
        resource_type="live_event",
        resource_id=resource_id,
        resource_revision=revision,
        fields={"title": source.title, "excerpt": source.summary or ""},
        visibility_hash=visibility,
        source_ids=(),
        local_only=False,
    )


def request_verdict(
    scope: WorkspaceContext, item: TranslationItemRequest
) -> "ResourceAuthorization | str":
    """Decide one live_event request item without any database access.

    Returns ``"resource_unavailable"`` for non-owners or a missing source (indistinguishable by
    design), ``"stale_request"`` when the client revision differs from the one recomputed here from
    the supplied text, else a ``ResourceAuthorization`` for the server-recomputed revision.
    """
    from modules.translations.public import ResourceAuthorization  # lazy: public imports us

    if scope.role != "owner" or item.source is None:
        return "resource_unavailable"
    revision = live_event_revision(item.source.title, item.source.summary)
    if revision != item.resource_revision:
        return "stale_request"
    built = live_event_input(scope, item.resource_id, item.source, revision)
    return ResourceAuthorization(revision, content_hash(built), built.visibility_hash)


async def read_live_event_translation_input(
    session: AsyncSession,
    *,
    scope: WorkspaceContext,
    multi_workspace_enabled: bool,
    event_id: UUID,
) -> TranslationInput | None:
    """Return the owner's newest stored snapshot for ``event_id`` as an input, else None.

    None for non-owners (no query issued) and for events with no stored snapshot. Scoped by
    workspace and actor; the revision is recomputed from the stored text, never trusted.
    """
    if scope.role != "owner":
        return None
    snapshot = await session.scalar(
        select(ContentTranslation.source_text)
        .where(
            ContentTranslation.workspace_id == scope.workspace_id,
            ContentTranslation.actor_user_id == scope.user_id,
            ContentTranslation.resource_type == "live_event",
            ContentTranslation.resource_id == event_id,
            ContentTranslation.source_text.is_not(None),
        )
        .order_by(ContentTranslation.created_at.desc())
        .limit(1)
    )
    if not snapshot or not snapshot.get("title"):
        return None
    source = LiveEventSource(title=snapshot["title"], summary=snapshot.get("summary"))
    revision = live_event_revision(source.title, source.summary)
    return live_event_input(scope, event_id, source, revision)
