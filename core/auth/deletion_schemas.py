"""Safe account deletion API contracts; no identifiers or credentials in responses."""
from typing import Literal

from pydantic import BaseModel, Field, SecretStr


class DeletionPreview(BaseModel):
    """Report only lifecycle blockers and owned workspace count."""
    can_delete: bool
    blockers: list[Literal["workspace_has_members", "workspace_has_live_invitations"]]
    workspace_count: int
    instance_operator: bool


class DeletionRequest(BaseModel):
    """Require current password and exact destructive-action confirmation."""
    current_password: SecretStr = Field(min_length=1, max_length=1024)
    confirmation: Literal["DELETE"]


class DeletionStatus(BaseModel):
    """Receipt-only progress with stable errors, never identity or content."""
    status: Literal["queued", "running", "failed", "succeeded"]
    phase: str
    error_code: str | None
