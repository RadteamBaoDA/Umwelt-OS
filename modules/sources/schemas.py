from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

SourceType = Literal[
    "rss", "web", "file", "github", "calendar", "email", "api", "mcp", "manual", "other"
]
SourceStatus = Literal["active", "paused", "archived"]


class SourceCreate(BaseModel):
    """Validate the type, name, and optional provider for a new source."""
    model_config = ConfigDict(extra="forbid")

    type: SourceType
    name: str = Field(min_length=1, max_length=200)
    provider: str | None = Field(default=None, max_length=120)


class SourcePatch(BaseModel):
    """Validate supported source name and lifecycle status updates."""
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    status: SourceStatus | None = None


class SourceRead(BaseModel):
    """Serialize source lifecycle, collection, processing, and generation state."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    type: str
    name: str
    provider: str | None
    status: str
    local_only: bool
    last_sync_at: datetime | None
    last_success_at: datetime | None
    last_error_at: datetime | None
    last_error_code: str | None
    collected_at: datetime | None
    indexed_at: datetime | None
    collection_error_code: str | None
    processing_error_code: str | None
    generation: int
    retired_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SourceFence(BaseModel):
    """Detached source eligibility snapshot; caller holds the DB row lock."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    status: str
    generation: int
    local_only: bool


class GadgetSourceSelection(BaseModel):
    """Expose source identity and lifecycle for an already-authorized dashboard caller.

    This immutable projection deliberately excludes configuration, credentials,
    scopes, and content. It does not establish provider item-level permissions.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    name: str
    type: str
    provider: str | None
    status: str
    generation: int
    local_only: bool


class GadgetSourceSelectionPage(BaseModel):
    """Return an immutable bounded page of dashboard-selectable source metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[GadgetSourceSelection, ...]
    next_cursor: str | None


class ConnectorSource(BaseModel):
    """Detached configuration snapshot for connector validation and dispatch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    type: str
    status: str
    generation: int
    configuration: dict[str, object]


class SourceList(BaseModel):
    """Return a source page and its optional continuation cursor."""
    items: list[SourceRead]
    next_cursor: str | None


class OperationRead(BaseModel):
    """Expose the status and timestamps of a source operation."""
    operation_id: UUID
    source_id: UUID
    status: Literal["queued", "running", "succeeded", "failed"]
    error_code: str | None
    created_at: datetime
    updated_at: datetime
