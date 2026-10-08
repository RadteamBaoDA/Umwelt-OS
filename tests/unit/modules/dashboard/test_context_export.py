"""N-R1/N-R5: dashboard export scope predicate, export path, and context owner gate order."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from core.workspaces.schemas import WorkspaceContext
from modules.dashboard import context, public


def test_dashboard_export_scope_binds_workspace_owner_and_cutoff() -> None:
    ws, snap = uuid4(), datetime(2026, 1, 1, tzinfo=UTC)
    sql = " AND ".join(
        str(p.compile(dialect=postgresql.dialect())) for p in public._dashboard_export_scope(7, snap, ws)
    )
    assert "dashboards.workspace_id" in sql and "dashboards.owner_id" in sql
    assert "dashboards.created_at <=" in sql and "dashboards.updated_at <=" in sql


@pytest.mark.asyncio
async def test_export_validation_dashboards_reaches_scope_without_name_error() -> None:
    """The dashboards validation path builds its predicate (it raised NameError before)."""
    scope = WorkspaceContext(user_id=1, workspace_id=uuid4(), role="owner", membership_revision=1)
    session = AsyncMock()
    session.scalar.return_value = 3
    with patch.object(public, "_admit", AsyncMock(return_value=None)):
        result = await public.validate_export_fences(
            session, owner_id=1, record_kind="dashboards", snapshot_at=datetime.now(UTC),
            expected_snapshot_count=0, fences=[], scope=scope, multi_workspace_enabled=True,
        )
    assert result.valid is False and result.reason == "snapshot_count_changed"


@pytest.mark.asyncio
async def test_member_denied_before_any_context_read() -> None:
    """N-R5: the owner gate runs first, so a member triggers no widget read."""
    member = WorkspaceContext(user_id=2, workspace_id=uuid4(), role="member", membership_revision=1)
    reads = AsyncMock()
    with (
        patch.object(context, "_tasks_widget", reads),
        patch.object(context, "_goals_widget", reads),
        patch.object(context, "_stories_widget", reads),
        patch.object(context, "_events_widget", reads),
    ):
        for builder in (context.build_daily_context, context.build_daily_widgets):
            with pytest.raises(HTTPException) as exc:
                await builder(AsyncMock(), datetime.now(UTC).date(), "UTC", scope=member, multi_workspace_enabled=True)
            assert exc.value.status_code == 403
    reads.assert_not_awaited()
