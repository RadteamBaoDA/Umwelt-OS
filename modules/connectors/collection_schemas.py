"""Public DTOs for durable collection requests and fenced managed-n8n admission."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

CollectionTrigger = Literal["manual", "scheduled", "retry"]
CollectionStatus = Literal["queued", "running", "succeeded", "no_changes", "failed", "cancelled"]


class CollectionRequestRead(BaseModel):
    """Expose a request's durable state; ``succeeded`` means data was accepted, not yet indexed."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    source_id: UUID
    status: CollectionStatus
    ingestion_run_id: UUID | None = None
    error_code: str | None = None


class CollectionAdmissionRequest(BaseModel):
    """Bind a managed n8n admission call to one source, revision and backend revision."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_generation: int = Field(ge=1)
    connector_revision: int = Field(ge=1)
    backend_revision: int = Field(ge=1)


class CollectionAdmissionRead(BaseModel):
    """Return the request and fenced admission token that later managed calls must carry."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    source_id: UUID
    admission_token: UUID
    attempt: int = Field(ge=1, le=5)
