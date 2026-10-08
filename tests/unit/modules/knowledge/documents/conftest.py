"""Shared owner scope and admission stub for Documents unit tests (no DB)."""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from core.workspaces.schemas import AccessFence, WorkspaceContext

WORKSPACE_ID = uuid4()
SCOPE = WorkspaceContext(user_id=1, workspace_id=WORKSPACE_ID, role="owner", membership_revision=1)
FENCE = AccessFence(workspace_id=WORKSPACE_ID, user_id=1, membership_revision=1, configuration_revision=1)
SCOPE_KW = {"scope": SCOPE, "multi_workspace_enabled": False}


@pytest.fixture(autouse=True)
def _admitted_owner():
    """Replace the workspace owner admission read with the matching owner fence."""
    with patch("modules.knowledge.documents.public.read_access_fence", AsyncMock(return_value=FENCE)):
        yield
