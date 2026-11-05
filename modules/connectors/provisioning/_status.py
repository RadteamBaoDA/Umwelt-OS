"""Connector locking and read-only activation/managed-credential status queries."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import NotFoundError
from core.workspaces.schemas import AccessFence, Scope
from modules.connectors.models import (
    ConnectorManagedCredential,
    ConnectorProvisioning,
)
from modules.connectors.provisioning._common import _lock_connector_rows
from modules.connectors.public import (
    _connector_access,
    _connector_actor,
    _read_scoped_source,
)
from modules.sources import public as sources
from modules.sources.public import SourceFence


async def lock_connector(
    session: AsyncSession,
    source_id: UUID,
    slots: tuple[str, ...] = (),
    *, scope: Scope, multi_workspace_enabled: bool, expected_access_fence: AccessFence | None = None,
) -> tuple[SourceFence | None, ConnectorProvisioning | None, dict[str, ConnectorManagedCredential]]:
    """Lock source, provisioning, then credential slots in the one supported order."""
    _connector_actor(scope)
    if type(multi_workspace_enabled) is not bool:
        raise ValueError("The actual configured workspace flag is required")
    source = await sources.lock_source(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled, expected_access_fence=expected_access_fence)
    if source is None:
        return None, None, {}
    return await _lock_connector_rows(
        session, source_id, slots, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )


async def get_managed_credential(
    session: AsyncSession, source_id: UUID, slot: str,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> ConnectorManagedCredential | None:
    """Read one credential slot without acquiring the provisioning lock chain."""
    source = await _read_scoped_source(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source is None:
        raise NotFoundError("Source not found")
    return await session.scalar(
        select(ConnectorManagedCredential)
        .where(
            ConnectorManagedCredential.source_id == source_id,
            ConnectorManagedCredential.slot == slot,
        )
        .execution_options(populate_existing=True)
    )


async def activation_status(
    session: AsyncSession, source_id: UUID,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> ConnectorProvisioning | None:
    """Read a session-bound provisioning ORM row as a current-state hint.

    Returns None when no row exists. The row remains managed by the supplied
    session; callers must not treat it as a detached DTO or use it after that
    session closes. This lookup refreshes the identity-map row but does not
    acquire the connector provisioning lock.
    """
    source = await _read_scoped_source(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source is None:
        raise NotFoundError("Source not found")
    return await session.scalar(
        select(ConnectorProvisioning)
        .where(ConnectorProvisioning.source_id == source_id)
        .execution_options(populate_existing=True)
    )


async def activation_statuses(
    session: AsyncSession, source_ids: frozenset[UUID],
    *, scope: Scope, multi_workspace_enabled: bool,
) -> dict[UUID, ConnectorProvisioning | None]:
    """Batch ``activation_status``: provisioning rows (or None when no row) keyed by source id.

    One connector access check, one scoped Source batch read and one ``IN`` select, all without row
    locks. Like the single form, an id not visible in ``scope`` raises 404 and rows stay bound to
    ``session``; bounded to 200 ids.
    """
    await _connector_access(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    visible = await sources.get_connector_sources(
        session, source_ids, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if visible.keys() != source_ids:
        raise NotFoundError("Source not found")
    if not source_ids:
        return {}
    rows = await session.scalars(
        select(ConnectorProvisioning).where(ConnectorProvisioning.source_id.in_(list(source_ids)))
        .execution_options(populate_existing=True))
    found = {row.source_id: row for row in rows}
    return {source_id: found.get(source_id) for source_id in source_ids}
