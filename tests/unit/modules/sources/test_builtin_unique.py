"""POST /sources keeps at most one non-archived Source per built-in provider."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from core.workspaces.schemas import WorkspaceContext
from modules.sources import routes
from modules.sources.schemas import SourceCreate

SCOPE = WorkspaceContext(user_id=7, workspace_id=uuid4(), role="owner", membership_revision=3)
REQUEST = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(multi_workspace_enabled=False))))
BUILTIN = SourceCreate(type="rss", name="Lobsters", provider="lobsters")
PLAIN = SourceCreate(type="rss", name="My feed")


def _created() -> MagicMock:
    """Minimal Source-shaped object accepted by SourceRead.model_validate."""
    from datetime import UTC, datetime

    return SimpleNamespace(
        id=uuid4(), workspace_id=SCOPE.workspace_id, type="rss", name="x", provider="lobsters", status="active",
        generation=1, local_only=False, graph_enabled=True, rag_index_enabled=True, configuration={}, created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )


async def _call(payload: SourceCreate, existing: list, read: AsyncMock | None = None):
    """Invoke the route with patched sources.public; returns (result, list mock, create mock)."""
    lister = AsyncMock(return_value=existing)
    creator = AsyncMock(return_value=_created())
    with patch.object(routes.public, "list_provider_sources", lister), patch.object(routes.public, "create_source", creator), \
            patch.object(routes, "SourceRead", MagicMock()):
        result = await routes.create_source(payload, AsyncMock(), REQUEST, SCOPE)
    return result, lister, creator


@pytest.mark.asyncio
async def test_second_builtin_create_conflicts() -> None:
    with pytest.raises(HTTPException) as exc:
        await _call(BUILTIN, [object()])
    assert exc.value.status_code == 409
    assert exc.value.detail == "builtin_already_added"


@pytest.mark.asyncio
async def test_create_after_archive_allowed() -> None:
    # list_provider_sources excludes archived rows, so an archived one yields an empty list.
    _, lister, creator = await _call(BUILTIN, [])
    lister.assert_awaited_once()
    creator.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_builtin_skips_lookup() -> None:
    _, lister, creator = await _call(PLAIN, [object()])
    lister.assert_not_awaited()
    creator.assert_awaited_once()
