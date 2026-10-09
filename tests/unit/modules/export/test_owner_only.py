"""Export is limited to the actor's own default workspace; members get 403."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from core.auth.dependencies import require_account
from core.workspaces.schemas import WorkspaceContext
from modules.export import routes


def _scope(user_id: int, role: str) -> WorkspaceContext:
    return WorkspaceContext(user_id=user_id, workspace_id=uuid4(), role=role, membership_revision=1)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize(("scope", "actor"), [(_scope(2, "member"), 2), (_scope(2, "owner"), 3)])
async def test_non_owner_or_mismatched_actor_is_403(scope: WorkspaceContext, actor: int) -> None:
    with pytest.raises(HTTPException) as exc:
        await routes._build_export_response(
            routes.ExportFormat.json, None, SimpleNamespace(owner_id=actor), scope, None, None,  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 403


def test_export_route_uses_account_auth_not_bootstrap_only() -> None:
    dep = routes.OwnerRead.__metadata__[0].dependency  # type: ignore[attr-defined]
    assert dep is require_account
