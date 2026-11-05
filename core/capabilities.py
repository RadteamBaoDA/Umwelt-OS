"""Late-bound module capabilities consumed by core (dependency inversion for ``core`` -> ``modules``).

Core must not import modules. Where core needs a module-owned behaviour (profile naming at setup, workspace-share
projections, translation purge, module availability, checkpoint erasure), the owning descriptor declares a
``CapabilitySpec(name, "package.module:function")`` and core calls the typed wrappers below. The wrapper resolves
the descriptor target at call time and looks the attribute up on the owner's public module each time, so there is no
import edge from core to the module and no stale cached callable (tests may still patch the owner's public function).

Resolution failures raise ``CapabilityUnavailable`` (a ``RuntimeError``): a missing registration is a wiring bug, not a
runtime condition callers should handle.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from core.auth.erasure_contracts import AccountErasureScope
    from core.workspaces.schemas import ResourceAccessProjection, Scope, WorkspaceContext

SET_INITIAL_OWNER_DISPLAY_NAME = "settings.set_initial_owner_display_name"
NORMALIZE_DISPLAY_NAME = "settings.normalize_display_name"
READ_MODULE_AVAILABILITY = "settings.read_module_availability"
ERASE_ACCOUNT_CHECKPOINTS_PAGE = "agents.erase_account_checkpoints_page"
LOCK_DOCUMENT_IDS = "documents.lock_document_ids"
READ_DOCUMENT_ACCESS_PROJECTION = "documents.read_document_access_projection"
CHECK_BRIEF_SHAREABLE = "dashboard.check_brief_shareable"
READ_BRIEF_ACCESS_PROJECTION = "dashboard.read_brief_access_projection"
PURGE_RESOURCE_TRANSLATIONS = "translations.purge_resource_translations"


class CapabilityUnavailable(RuntimeError):
    """Raised when no composed descriptor registers the requested capability."""


def resolve_capability(name: str) -> Any:
    """Return the callable registered under ``name`` by a composed module descriptor.

    The descriptor set is read from ``core.modules.composition_descriptors`` (lifecycle plus platform) lazily so this
    module imports nothing from ``modules``. The owner's public attribute is fetched on each call.

    Raises:
        CapabilityUnavailable: If no descriptor declares ``name``.
    """
    from core.modules import composition_descriptors

    for descriptor in composition_descriptors():
        for spec in descriptor.capabilities:
            if spec.name == name:
                module_name, _, attribute = spec.target.partition(":")
                return getattr(import_module(module_name), attribute)
    raise CapabilityUnavailable(f"No module registers capability {name!r}")


async def set_initial_owner_display_name(
    session: AsyncSession, *, owner_id: int, display_name: str | None
) -> None:
    """Store the first-run display name in the caller's transaction (no commit); blank/None is a no-op."""
    await resolve_capability(SET_INITIAL_OWNER_DISPLAY_NAME)(
        session, owner_id=owner_id, display_name=display_name
    )


def normalize_display_name(value: str | None) -> str | None:
    """Apply the Settings display-name rules (trim, blank -> None, bounded length, no control characters)."""
    result: str | None = resolve_capability(NORMALIZE_DISPLAY_NAME)(value)
    return result


async def read_module_availability(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool
) -> Any:
    """Return the owner-only scoped module lifecycle DTO (``.modules`` items expose ``id``/``explicitly_disabled``)."""
    return await resolve_capability(READ_MODULE_AVAILABILITY)(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )


async def erase_account_checkpoints_page(
    session: AsyncSession,
    scope: AccountErasureScope,
    *,
    limit: int = 25,
) -> bool:
    """Run one bounded page of agent checkpoint erasure; True when no work remains for the account."""
    done: bool = await resolve_capability(ERASE_ACCOUNT_CHECKPOINTS_PAGE)(
        session, scope, limit=limit
    )
    return done


async def lock_document_ids(
    session: AsyncSession,
    document_ids: list[UUID],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[UUID]:
    """Lock a bounded, sorted set of retained document rows for owner transactions (Documents owns the rule)."""
    locked: list[UUID] = await resolve_capability(LOCK_DOCUMENT_IDS)(
        session,
        document_ids,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    return locked


async def read_document_access_projection(
    session: AsyncSession,
    resource_id: UUID,
    *,
    scope: Scope,
) -> ResourceAccessProjection | None:
    """Return the share-binding projection of one in-scope document, or None when it is not visible."""
    projection: ResourceAccessProjection | None = await resolve_capability(
        READ_DOCUMENT_ACCESS_PROJECTION
    )(
        session,
        resource_id,
        scope=scope,
    )
    return projection


async def check_brief_shareable(
    session: AsyncSession,
    brief_id: UUID,
    *,
    scope: WorkspaceContext,
    member_user_id: int,
    multi_workspace_enabled: bool = False,
) -> None:
    """Raise the Dashboard 404/409 shareability errors for one brief; return when it may be shared."""
    await resolve_capability(CHECK_BRIEF_SHAREABLE)(
        session,
        brief_id,
        scope=scope,
        member_user_id=member_user_id,
        multi_workspace_enabled=multi_workspace_enabled,
    )


async def read_brief_access_projection(
    session: AsyncSession,
    resource_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool = False,
) -> ResourceAccessProjection | None:
    """Return the brief's revision and shareability projection, or None when it is not visible."""
    projection: ResourceAccessProjection | None = await resolve_capability(
        READ_BRIEF_ACCESS_PROJECTION
    )(
        session,
        resource_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    return projection


async def purge_resource_translations(
    session: AsyncSession,
    workspace_id: UUID,
    resource_type: str,
    resource_id: UUID,
) -> int:
    """Delete every actor's derived translations of one resource (flush-only); returns the removed row count."""
    removed: int = await resolve_capability(PURGE_RESOURCE_TRANSLATIONS)(
        session,
        workspace_id,
        resource_type,
        resource_id,
    )
    return removed
