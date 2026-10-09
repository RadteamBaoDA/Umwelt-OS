from uuid import uuid4

from core.workspaces.access import can_read_resource
from core.workspaces.schemas import WorkspaceContext


def test_membership_without_share_is_not_read_access():
    workspace_id = uuid4()
    member = WorkspaceContext(2, workspace_id, "member", 1)
    assert not can_read_resource(member, workspace_id, False)
    assert can_read_resource(member, workspace_id, True)
    assert not can_read_resource(member, uuid4(), True)
