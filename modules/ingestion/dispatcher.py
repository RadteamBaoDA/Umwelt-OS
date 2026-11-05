"""Compatibility facade for the outbox dispatcher, which now lives in ``core.events.dispatch``.

``dispatch_pending_work`` and the envelope validator are re-exported unchanged for the ARQ cron registration and
existing callers. ``WORKER_BY_EVENT`` / ``PRODUCERS_BY_EVENT`` are derived on access from the descriptor-driven
event registry instead of being hard-coded here.
"""

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.events.dispatch import (
    DISPATCH_SCAN_STATE,
    DISPATCH_STALE_AFTER,
    dispatch_pending_work,
    valid_event_envelope,
)
from core.events.registry import get_event_registry
from core.workspaces.schemas import Scope
from modules.ingestion import public

__all__ = [
    "DISPATCH_SCAN_STATE", "DISPATCH_STALE_AFTER", "dispatch_pending_work", "mark_event_delivered",
    "valid_event_envelope",
]


def __getattr__(name: str) -> Any:
    """Serve the historical routing tables from the event registry (lazy, so import order is irrelevant)."""
    if name == "WORKER_BY_EVENT":
        return get_event_registry().job_names
    if name == "PRODUCERS_BY_EVENT":
        return get_event_registry().producers
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


async def mark_event_delivered(
    session: AsyncSession, event_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Forward the owner flush-only acknowledgement; caller holds ordered locks and commits.

    Entities' old dispatcher import remains an explicit required-scope caller migration.
    This compatibility module path introduces no commit or earlier-lock acquisition.
    """
    return await public.mark_event_delivered(session, event_id, scope=scope,
                                             multi_workspace_enabled=multi_workspace_enabled)
