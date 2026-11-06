"""Shared dashboard constants, error types, admission and row-lock/revision helpers (private to the dashboard package)."""

from __future__ import annotations

from datetime import UTC, tzinfo
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.admission import actor_id as _actor
from core.workspaces.admission import admit_scope
from core.workspaces.schemas import AccessFence, Scope
from modules.dashboard.models import (
    Dashboard,
    GadgetDefinition,
)
from modules.dashboard.schemas import (
    MAX_RULES_PER_DEFINITION,
)

MAX_REVISION = 9_007_199_254_740_991
DASHBOARD_QUOTA_LOCK_NAMESPACE = 4_603_202
MAX_HIGHLIGHT_NOTIFICATIONS_PER_TRANSACTION = 100
# A document can match every configured rule; keep a whole page below the insertion cap so advancing
# its cursor never drops an eligible rule/version notification.
HIGHLIGHT_SCAN_PAGE_LIMIT = max(
    1,
    MAX_HIGHLIGHT_NOTIFICATIONS_PER_TRANSACTION // MAX_RULES_PER_DEFINITION,
)
HIGHLIGHT_MATCHES_PER_PAGE_MAX = HIGHLIGHT_SCAN_PAGE_LIMIT * MAX_RULES_PER_DEFINITION
PREVIEW_PAGE_SIZE = 100
PREVIEW_MATCH_TIMEOUT_SECONDS = 5
PREVIEW_MAX_PAGES = 2  # 200 current versions at most
PREVIEW_MAX_MATCHES = 100


def _owner_tzinfo(name: str) -> tzinfo:
    """Resolve an IANA timezone name, falling back to UTC for unknown, malformed or unreadable names. Pure."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return UTC


async def _admit(
    session: AsyncSession,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    lock: bool = False,
    expected: AccessFence | None = None,
) -> AccessFence:
    """Admit owner scope before reading or locking dashboard-owned data.

    Thin wrapper over ``core.workspaces.admission.admit_scope`` (owner only, admin viewers read-only); keeps this module's
    private ``_admit`` name so call sites and tests are unchanged. No commit; the caller owns the
    transaction and lock ordering.
    """
    return await admit_scope(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        lock=lock,
        expected=expected,
        admin_read_only_on_lock=True,
    )


class DashboardConflict(Exception):
    """Represent a stale revision, exhausted counter, or invariant conflict for route mapping."""

    def __init__(self, code: str, message: str, current_revision: int | None = None) -> None:
        """Keep the stable machine code and optional current revision for HTTP 409 responses."""
        super().__init__(message)
        self.code = code
        self.current_revision = current_revision


class DashboardMissing(Exception):
    """Represent an absent or foreign-owned dashboard resource without disclosing its owner."""


async def _lock_dashboard(
    session: AsyncSession,
    dashboard_id: UUID,
    *,
    scope: Scope,
) -> Dashboard:
    """Lock one owner-scoped dashboard row and refresh it before revision checks."""
    row = await session.scalar(
        select(Dashboard)
        .where(
            Dashboard.id == dashboard_id,
            Dashboard.workspace_id == scope.workspace_id,
            Dashboard.owner_id == _actor(scope),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise DashboardMissing
    return row


async def _lock_definition(
    session: AsyncSession,
    definition_id: UUID,
    *,
    scope: Scope,
) -> GadgetDefinition:
    """Lock one owner-scoped definition before querying dashboard references."""
    row = await session.scalar(
        select(GadgetDefinition)
        .where(
            GadgetDefinition.id == definition_id,
            GadgetDefinition.workspace_id == scope.workspace_id,
            GadgetDefinition.owner_id == _actor(scope),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise DashboardMissing
    return row


def _check_revision(row: Dashboard | GadgetDefinition, expected: int) -> None:
    """Reject stale edits and exhausted JavaScript-safe revision counters before mutation."""
    if row.revision != expected:
        raise DashboardConflict("revision_conflict", "The resource changed", row.revision)
    if row.revision >= MAX_REVISION:
        raise DashboardConflict(
            "revision_exhausted", "The resource revision is exhausted", row.revision
        )


def _bump(row: Dashboard | GadgetDefinition) -> int:
    """Advance one already-locked revision exactly once and return its emitted value."""
    if row.revision >= MAX_REVISION:
        raise DashboardConflict(
            "revision_exhausted", "The resource revision is exhausted", row.revision
        )
    row.revision += 1
    return row.revision


async def _lock_owner_creation_quota(session: AsyncSession, scope: Scope) -> None:
    """Serialize dashboard/definition quota checks for one workspace.

    PostgreSQL transaction advisory locks use a namespace distinct from search
    indexing; all creation paths acquire this before source/entity locks and
    hold it until commit. This bounds quota oversubscription without a process lock.
    """
    await session.execute(
        text(
            "SELECT pg_advisory_xact_lock(hashtextextended('dashboard.quota:' || :workspace_id, 0))"
        ),
        {"workspace_id": str(scope.workspace_id)},
    )
