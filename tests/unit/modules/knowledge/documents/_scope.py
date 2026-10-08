"""Shared owner scope constants for Documents unit tests (plain module; one import identity)."""

from uuid import uuid4

from core.workspaces.schemas import AccessFence, WorkspaceContext

WORKSPACE_ID = uuid4()
SCOPE = WorkspaceContext(user_id=1, workspace_id=WORKSPACE_ID, role="owner", membership_revision=1)
FENCE = AccessFence(workspace_id=WORKSPACE_ID, user_id=1, membership_revision=1, configuration_revision=1)
SCOPE_KW = {"scope": SCOPE, "multi_workspace_enabled": False}
