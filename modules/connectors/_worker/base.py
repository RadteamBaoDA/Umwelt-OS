"""Shared fairness cursors, admission helpers and constants for the connector worker jobs."""

import logging
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from core.errors import DomainError
from core.worker_cursors import STATE_KEY, read_cursor, write_cursor
from core.workspaces.public import read_access_fence
from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.sources import public as sources

logger = logging.getLogger("modules.connectors.worker")

# Admission outcomes that mean "this subject may not act now": skip it, never rebase onto
# another actor or epoch. Anything else (database, programming, 5xx) propagates.
_DENIED = frozenset({401, 403, 404, 409})
_PASS_LIMIT = 40


_CURSOR_KEYS = frozenset(
    {
        "connectors_deleted_revoke",
        "connectors_workflows",
        "connectors_unknown_create",
        "connectors_activation",
        "connectors_transitions",
    }
)


def _cursor_ctx(ctx: dict[str, object]) -> dict[str, object] | None:
    """Return ``ctx`` when the worker-wide fairness dict (``core.worker_cursors.STATE_KEY``) is installed.

    ARQ hands each job a shallow copy of ``ctx``, so only that shared ``dict[str, str]`` survives
    between passes. Without it every pass starts at the beginning, which is correct but not fair.
    """
    return ctx if isinstance(ctx.get(STATE_KEY), dict) else None


async def _read_cursor(ctx: dict[str, object] | None, key: str) -> UUID | None:
    """Read one Source-id fairness cursor (Redis first); missing or corrupt restarts the sweep."""
    return await read_cursor(ctx, key, _CURSOR_KEYS) if ctx is not None else None


async def _write_cursor(ctx: dict[str, object] | None, key: str, last: UUID | None) -> None:
    """Remember the last discovered Source id of a full page, or clear the key to wrap around."""
    if ctx is not None:
        await write_cursor(ctx, key, last, _CURSOR_KEYS)


def _slot_state(ctx: dict[str, object] | None) -> dict[str, str] | None:
    """Return the worker's shared per-pass cursor state mapping, or ``None`` without a worker context."""
    return cast(dict[str, str], ctx[STATE_KEY]) if ctx is not None else None


def _read_slot_cursor(ctx: dict[str, object] | None, key: str) -> tuple[UUID, str] | None:
    """Parse a composite ``source_id|slot`` cursor (encoded string, not a core UUID cursor)."""
    state = _slot_state(ctx)
    try:
        source_id, slot = (state[key] if state is not None else "").split("|", 1)
        return UUID(source_id), slot
    except (KeyError, ValueError):
        return None


def _write_slot_cursor(
    ctx: dict[str, object] | None, key: str, last: tuple[UUID, str] | None
) -> None:
    """Remember the last (Source id, slot) of a full page, or clear the key to wrap around."""
    state = _slot_state(ctx)
    if state is None:
        return
    if last is None:
        state.pop(key, None)
    else:
        state[key] = f"{last[0]}|{last[1]}"


def _page(
    statement: Any,
    id_column: Any,
    after: UUID | None,
    *where: ColumnElement[bool],
    order: tuple[Any, ...] = (),
    slot_column: Any = None,
    after_slot: str | None = None,
) -> Any:
    """Order a Source-id discovery by id after the cursor; denied subjects cannot starve later ones.

    With ``slot_column`` and ``after_slot`` the keyset is the composite (id, slot), so a page that
    ended mid-Source resumes at that Source's next slot instead of skipping its remaining slots.
    """
    statement = statement.where(*where)
    if after is not None:
        if slot_column is not None and after_slot is not None:
            statement = statement.where(
                or_(id_column > after, and_(id_column == after, slot_column > after_slot))
            )
        else:
            statement = statement.where(id_column > after)
    return statement.order_by(id_column, *order).limit(_PASS_LIMIT)


def _lineage(source_id: UUID, envelope: object) -> tuple[InternalJobScope, AccessFence] | None:
    """Rebuild the original principal and access fence recorded in a durable operation JSON.

    Prepared credential/workflow/activation envelopes record the admitted workspace, actor,
    membership revision, workspace configuration revision and Source generation. Legacy rows
    without them are quarantined (None): a job is never rebased onto the current epoch.
    """
    if not isinstance(envelope, dict):
        return None
    try:
        scope = InternalJobScope(
            workspace_id=UUID(str(envelope["workspace_id"])),
            actor_user_id=envelope["actor_user_id"],
            membership_revision=envelope["membership_revision"],
            source_id=source_id,
            source_generation=envelope["source_generation"],
        )
        return scope, AccessFence(
            scope.workspace_id,
            scope.actor_user_id,
            scope.membership_revision,
            envelope["workspace_configuration_revision"],
        )
    except (KeyError, TypeError, ValueError):
        return None


async def _admit_lineage(
    session: AsyncSession,
    source_id: UUID,
    envelope: object,
    *,
    multi_workspace_enabled: bool,
) -> tuple[InternalJobScope, AccessFence] | None:
    """Admit a durable operation's original principal; None releases the transaction and skips it.

    Reads the access fence for the recorded lineage (non-locking) and requires it to equal the
    recorded fence exactly. Denial, a changed epoch or a legacy envelope never fall back to the
    current owner. Callers that continue into locks take them in access, Source, provisioning order.
    """
    lineage = _lineage(source_id, envelope)
    if lineage is None:
        await session.rollback()
        return None
    scope, original = lineage
    try:
        current = await read_access_fence(
            session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
    except (DomainError, HTTPException) as exc:
        await session.rollback()
        if exc.status_code not in _DENIED:
            raise
        return None
    if current != original:
        await session.rollback()
        return None
    return scope, original


async def _admit_source_job(
    session: AsyncSession,
    source_id: UUID,
    *,
    multi_workspace_enabled: bool,
) -> tuple[InternalJobScope, AccessFence] | None:
    """Recipe S: resolve a bare Source's job scope, then read its fence; None means skip this Source.

    ``resolve_source_job_scope`` admits (locks) auth/workspace/membership before it rereads the
    Source row, so this is safe before any Source lock. Archived anchors resolve too, which is what
    deleted-source grant revocation needs. The caller continues into ``lock_*`` helpers passing the
    returned fence as the expected fence.
    """
    try:
        scope = await sources.resolve_source_job_scope(
            session,
            source_id,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if scope is None:
            await session.rollback()
            return None
        fence = await read_access_fence(
            session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
    except (DomainError, HTTPException) as exc:
        await session.rollback()
        if exc.status_code not in _DENIED:
            raise
        return None
    return scope, fence
