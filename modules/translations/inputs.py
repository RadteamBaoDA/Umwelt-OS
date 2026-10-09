"""Authorized translation inputs, loaded through owner public modules (lazy imports, no ORM).

Importing this module registers the News and Brief adapters with the translation authorizer registry.
"""

from importlib import import_module
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.schemas import WorkspaceContext
from modules.translations.public import ResourceAuthorization, register_resource_authorizer
from modules.translations.schemas import TranslationInput, TranslationItemRequest
from modules.translations.service import content_hash

__all__ = ["TranslationInput", "load_translation_input"]

# resource type -> (owner public module, reader, id keyword)
_OWNERS = {
    "news_story": ("modules.news.public", "read_story_translation_input", "story_id"),
    "daily_brief": ("modules.dashboard.public", "read_brief_translation_input", "brief_id"),
}


async def _load(
    session: AsyncSession, scope: WorkspaceContext, resource_type: str, resource_id: UUID,
    *, multi_workspace_enabled: bool,
) -> TranslationInput | None:
    module, reader, id_kw = _OWNERS[resource_type]
    read: Any = getattr(import_module(module), reader)  # lazy: avoids import cycles with owner modules
    found: TranslationInput | None = await read(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, **{id_kw: resource_id},
    )
    if found is not None and (found.workspace_id != scope.workspace_id or found.resource_type != resource_type):
        return None
    return found


async def load_translation_input(
    session: AsyncSession, scope: WorkspaceContext, item: TranslationItemRequest,
    *, multi_workspace_enabled: bool,
) -> TranslationInput | None:
    """Return the currently authorized input, or None (callers map None to a uniform 404)."""
    return await _load(
        session, scope, item.resource_type, item.resource_id, multi_workspace_enabled=multi_workspace_enabled,
    )


def _adapter(resource_type: str) -> Any:
    async def authorize(
        session: AsyncSession, *, scope: WorkspaceContext, resource_id: UUID, multi_workspace_enabled: bool,
    ) -> ResourceAuthorization | None:
        source = await _load(
            session, scope, resource_type, resource_id, multi_workspace_enabled=multi_workspace_enabled,
        )
        if source is None:
            return None
        return ResourceAuthorization(source.resource_revision, content_hash(source), source.visibility_hash)

    return authorize


for _type in _OWNERS:
    register_resource_authorizer(_type, _adapter(_type))
