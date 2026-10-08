"""Shared owner scope and admission stub for Temporal unit tests (no DB)."""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from core.workspaces.schemas import AccessFence, InternalJobScope, WorkspaceContext

WORKSPACE_ID = uuid4()
OWNER = WorkspaceContext(user_id=1, workspace_id=WORKSPACE_ID, role="owner", membership_revision=1)
MEMBER = WorkspaceContext(user_id=2, workspace_id=WORKSPACE_ID, role="member", membership_revision=1)
JOB = InternalJobScope(workspace_id=WORKSPACE_ID, actor_user_id=1, membership_revision=1)
FENCE = AccessFence(workspace_id=WORKSPACE_ID, user_id=1, membership_revision=1, configuration_revision=1)


@pytest.fixture
def admitted_owner():
    """Replace the workspace fence read with the matching owner fence."""
    with patch("modules.knowledge.temporal.public.workspaces.read_access_fence", AsyncMock(return_value=FENCE)):
        yield
