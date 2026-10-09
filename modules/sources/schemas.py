from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from core.workspaces.schemas import AccessFence

SourceType = Literal[
    "rss", "web", "file", "github", "calendar", "email", "api", "mcp", "manual", "other"
]
SourceStatus = Literal["active", "paused", "archived"]


class SourceCreate(BaseModel):
    """Validate a source type and keep registered provider identity immutable at creation."""
    model_config = ConfigDict(extra="forbid")

    type: SourceType
    name: str = Field(min_length=1, max_length=200)
    provider: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def validate_provider_type(self) -> "SourceCreate":
        """Require each registered provider to use its one supported source type."""
        expected = {
            "youtube": "rss",
            "arxiv": "rss",
            "huggingface": "api",
            "github_releases": "api",
            "github": "api",
            "telegram": "api",
            "alpha_vantage": "api",
            "open_meteo": "api",
            "bbc_world": "rss",
            "vnexpress_business": "rss",
            "hn_top": "api",
            "gdelt_economy": "api",
            "world_bank": "api",
            "frankfurter": "api",
            "ecb": "api",
            "binance": "api",
            "alternative_me": "api",
            "usgs": "api",
            "coinpaprika": "api",
            "coingecko": "api",
            "google_news": "rss",
        }.get(self.provider or "")
        if self.provider is not None and expected is None:
            raise ValueError("Provider is not registered")
        if expected is not None and self.type != expected:
            raise ValueError("Source type does not match the registered provider")
        return self


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
    # Filled by the route from connector schedule state; None for sources without a schedule.
    next_due_at: datetime | None = None
    retry_at: datetime | None = None


class SourceFence(BaseModel):
    """Workspace-bound lifecycle snapshot; only locking readers hold the Source row lock."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    workspace_id: UUID
    status: str
    generation: int
    local_only: bool


class SourceExportFence(BaseModel):
    """Bind an export's source identity and generation to the admitted workspace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: UUID
    workspace_id: UUID
    generation: int = Field(ge=0)


class SourceFenceSet(BaseModel):
    """Complete ordered Source set plus caller-held access fence, never a portable lock token.

    Every Source is admitted before construction. The owning transaction must keep its locks
    until publication/commit and release them before external I/O.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    fences: tuple[SourceFence, ...] = Field(max_length=500)
    access_fence: AccessFence


class SourceMetadataExportFence(BaseModel):
    """Bind exported metadata to its workspace, Source row revision and digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: UUID
    workspace_id: UUID
    created_at: datetime
    updated_at: datetime
    generation: int = Field(ge=1)
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class SourceMetadataExportPage(BaseModel):
    """Return credential-free metadata for one actual principal and admitted workspace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner_id: int = Field(ge=1)
    workspace_id: UUID
    record_kind: Literal["sources"]
    snapshot_at: datetime
    snapshot_count: int = Field(ge=0)
    items: list[SourceRead] = Field(max_length=100)
    fences: list[SourceMetadataExportFence] = Field(max_length=100)
    payload_bytes: int = Field(ge=0, le=16_777_216)
    max_payload_bytes: int = Field(default=16_777_216, ge=1, le=16_777_216)
    next_cursor: str | None = None
    available: bool = True
    omission_reason: None = None


class SourceMetadataExportValidation(BaseModel):
    """Report whether source metadata records remain eligible and unchanged before publication."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    valid: bool
    reason: Literal["valid", "owner_unavailable", "snapshot_count_changed", "record_changed"]
    observed_snapshot_count: int = Field(ge=0)

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
    """Workspace-bound defensive configuration snapshot, never standalone authorization."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    workspace_id: UUID
    type: str
    status: str
    generation: int
    configuration: dict[str, object]
    local_only: bool
    provider: str | None = None


class SourceList(BaseModel):
    """Return a source page and its optional continuation cursor."""
    items: list[SourceRead]
    next_cursor: str | None


class OperationRead(BaseModel):
    """Expose scope-bound purge progress after admission, even without a canonical Source."""
    operation_id: UUID
    workspace_id: UUID
    source_id: UUID
    status: Literal["queued", "running", "succeeded", "failed"]
    error_code: str | None
    documents_status: Literal["queued", "deleted", "failed", "unavailable"]
    pending_child_count: int | None
    failed_child_count: int | None
    pending_owner_codes: list[str]
    # Source-local Memory coverage stage; error codes are fixed allowlisted tokens, never content.
    memory_status: Literal["queued", "running", "succeeded", "failed"] = "queued"
    memory_error_code: str | None = None
    created_at: datetime
    updated_at: datetime


class SourcePurgeJobIdentity(BaseModel):
    """Exact retained purge authority tuple captured at operation creation; never raw URIs."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    operation_id: UUID
    workspace_id: UUID
    actor_user_id: StrictInt = Field(gt=0)
    membership_revision: StrictInt = Field(gt=0)
    configuration_revision: StrictInt = Field(gt=0)
    source_id: UUID
    source_generation: StrictInt = Field(gt=0)


class SourceImpactRead(BaseModel):
    """Counts only, no content or names; each value saturates at 1000."""

    document_count: int
    gadget_definition_count: int
    gadget_placement_count: int
    conversation_count: int
