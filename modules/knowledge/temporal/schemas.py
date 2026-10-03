"""Strict owner-facing temporal status and finite reconciliation contracts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GraphStatus(BaseModel):
    """Separate canonical availability from desired/applied derived graph progress."""
    mapping_id: UUID
    document_version_id: UUID
    episode_id: UUID
    partition_id: UUID
    status: str
    desired_revision: int
    applied_revision: int
    error_code: str | None
    graph_enabled: bool = False
    applied_at: datetime | None = None


class ReconcileRequest(BaseModel):
    """Select one finite owner scope; empty/global requests and ambiguous scopes are rejected."""
    model_config = ConfigDict(extra="forbid")
    source_id: UUID | None = None
    document_version_ids: list[UUID] = Field(default_factory=list, max_length=100)
    entity_id: UUID | None = None

    @model_validator(mode="after")
    def bounded_scope(self) -> "ReconcileRequest":
        """Require exactly one scope and unique version identities before any work is queued."""
        if sum((self.source_id is not None, bool(self.document_version_ids), self.entity_id is not None)) != 1:
            raise ValueError("Choose exactly one bounded reconciliation scope")
        if len(set(self.document_version_ids)) != len(self.document_version_ids):
            raise ValueError("Version identities must be unique")
        return self


class ReconcileStatus(BaseModel):
    """Expose resumable coverage counts without claiming pending/blocked cleanup is successful."""
    run_id: UUID
    status: str
    scanned: int
    queued: int
    converged: int
    blocked: int
    failed: int
    continuation: str | None


class ChangeRead(BaseModel):
    """Identify an observed canonical mutation without fabricated earlier field values."""
    id: int
    kind: str
    canonical_id: UUID
    revision: int | None
    changed_fields: list[str]
    origin: str
    deleted: bool
    observed_at: datetime
    evidence: list[dict[str, object]]


class ChangePage(BaseModel):
    """Bound canonical change history, explicitly starting when durable tracking was installed."""
    items: list[ChangeRead]
    next_cursor: str | None
    history_before_tracking_available: Literal[False] = False
