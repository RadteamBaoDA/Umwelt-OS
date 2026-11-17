"""Shared admission fence and change-scheduling helpers for the entities owner implementation.

Private implementation of ``modules.knowledge.entities.public``; import from that facade.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.admission import ALL_SCOPE_TYPES, admit_scope
from core.workspaces.schemas import (
    AccessFence,
    Scope,
)
from modules.knowledge.entities.models import (
    Entity,
    EntityEvidenceMembership,
)


async def _admit(
    session: AsyncSession,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    lock: bool = False,
    expected: AccessFence | None = None,
) -> AccessFence:
    """Require owner scope and capture or lock authorization before entity locks.

    Delegates to the canonical ``admit_scope``: typed workspace/admin-read/job scope (``TypeError``), owner-only
    members (403), admin viewers denied when ``lock`` is true (403), boolean multi-workspace flag, then a locked
    (with ``expected``) or read access fence. No commit.
    """
    return await admit_scope(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        lock=lock,
        expected=expected,
        allowed_scopes=ALL_SCOPE_TYPES,
        scope_label="entity workspace",
        admin_read_only_on_lock=True,
    )


async def _schedule_entity_change(
    session: AsyncSession,
    entity: Entity,
    fields: list[str],
    origin: str,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Flush workspace-local support and revision scheduling without committing."""
    from modules.knowledge.temporal import public as temporal

    rows = list(
        (
            await session.execute(
                select(
                    EntityEvidenceMembership.document_version_id,
                    EntityEvidenceMembership.chunk_id,
                )
                .where(
                    EntityEvidenceMembership.workspace_id == scope.workspace_id,
                    EntityEvidenceMembership.entity_id == entity.id,
                )
                .limit(10_001)
            )
        ).all()
    )
    if len(rows) > 10_000:
        raise ValueError("Entity change exceeds complete support bound")
    await temporal.schedule_canonical_change(
        session,
        kind="entity",
        canonical_id=entity.id,
        revision=entity.revision,
        fields=fields,
        support=[(version, chunk) for version, chunk in rows],
        origin=origin,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
