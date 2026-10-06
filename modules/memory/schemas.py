"""Request and response schemas for memory items, candidates, and privacy configuration."""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool

MemoryType = Literal["fact", "preference", "instruction", "decision", "procedural"]
MemoryStatus = Literal["active", "invalidated", "superseded", "forgotten"]
CandidateStatus = Literal["pending", "accepted", "rejected", "superseded", "expired"]

MAX_MEMORY_CONTENT_LENGTH = 5000
MAX_REASON_LENGTH = 1000


class MemoryProvenance(BaseModel):
    """Provenance tracking origin conversation, document, or manual creation."""

    model_config = ConfigDict(extra="allow")

    source: str | None = None
    conversation_id: UUID | None = None
    message_id: UUID | None = None
    source_id: UUID | None = None
    document_id: UUID | None = None
    document_version_id: UUID | None = None
    chunk_id: UUID | None = None
    origin: Literal["manual", "agent", "model"] = "manual"


class MemoryCreate(BaseModel):
    """Payload for explicit owner memory creation."""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=MAX_MEMORY_CONTENT_LENGTH)
    type: MemoryType = "fact"
    provenance: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    reason: str | None = Field(default=None, max_length=MAX_REASON_LENGTH)


class MemoryUpdate(BaseModel):
    """Payload for updating an existing active memory."""

    model_config = ConfigDict(extra="forbid")

    content: str | None = Field(default=None, min_length=1, max_length=MAX_MEMORY_CONTENT_LENGTH)
    type: MemoryType | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    reason: str | None = Field(default=None, max_length=MAX_REASON_LENGTH)


class MemoryInvalidateRequest(BaseModel):
    """Payload for marking an existing memory invalidated."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=MAX_REASON_LENGTH)


class MemorySupersedeRequest(BaseModel):
    """Payload for superseding an existing memory with newer knowledge."""

    model_config = ConfigDict(extra="forbid")

    new_content: str = Field(min_length=1, max_length=MAX_MEMORY_CONTENT_LENGTH)
    type: MemoryType | None = None
    reason: str = Field(min_length=1, max_length=MAX_REASON_LENGTH)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class MemoryForgetRequest(BaseModel):
    """Payload for immediately forgetting and purges a memory item."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=MAX_REASON_LENGTH)


class MemoryRead(BaseModel):
    """Safe projection of a persistent memory item."""

    model_config = ConfigDict(extra="ignore", from_attributes=True)

    id: UUID
    content: str
    type: str = Field(alias="memory_type")
    provenance: dict[str, Any]
    confidence: float
    reason: str | None = None
    status: str
    is_manual: bool
    superseded_by_id: UUID | None = None
    candidate_id: UUID | None = None
    created_at: datetime
    updated_at: datetime
    invalidated_at: datetime | None = None
    forgotten_at: datetime | None = None


class MemoryPage(BaseModel):
    """Cursor-paginated page of memory items."""

    model_config = ConfigDict(extra="forbid")

    items: list[MemoryRead]
    next_cursor: str | None = None
    total_count: int | None = None


class MemoryCandidateCreate(BaseModel):
    """Payload for proposing a memory candidate for review or auto-acceptance."""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=MAX_MEMORY_CONTENT_LENGTH)
    type: MemoryType = "fact"
    provenance: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    reason: str | None = Field(default=None, max_length=MAX_REASON_LENGTH)


class MemoryCandidateRead(BaseModel):
    """Safe projection of an evaluated memory candidate."""

    model_config = ConfigDict(extra="ignore", from_attributes=True)

    id: UUID
    content: str
    type: str = Field(alias="memory_type")
    provenance: dict[str, Any]
    confidence: float
    novelty_score: float
    usefulness_score: float
    reason: str | None = None
    status: str
    rejection_reason: str | None = None
    created_at: datetime
    updated_at: datetime
    evaluated_at: datetime | None = None


class MemoryCandidatePage(BaseModel):
    """Cursor-paginated list of memory candidates."""

    model_config = ConfigDict(extra="forbid")

    items: list[MemoryCandidateRead]
    next_cursor: str | None = None


class MemoryCandidateRejectRequest(BaseModel):
    """Payload for rejecting a proposed candidate."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=MAX_REASON_LENGTH)


class MemoryPrivacyConfig(BaseModel):
    """Owner memory and conversation history privacy settings."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    store_conversation_history: bool = True
    store_agent_memory: bool = False
    auto_accept_memory: bool = False


class MemoryExportPrivacy(BaseModel):
    """Immutable, minimal history-retention setting and missing-row fence for exports."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    store_conversation_history: StrictBool
    persisted: StrictBool
    updated_at: datetime | None


class MemoryPrivacyUpdate(BaseModel):
    """Payload for updating owner memory privacy settings."""

    model_config = ConfigDict(extra="forbid")

    store_conversation_history: bool | None = None
    store_agent_memory: bool | None = None
    auto_accept_memory: bool | None = None


class MemoryPurgeRequest(BaseModel):
    """Owner request to purge retained memories or conversation history."""

    model_config = ConfigDict(extra="forbid")

    purge_forgotten_memories: bool = False
    purge_rejected_candidates: bool = False
    purge_conversation_history: bool = False


class MemoryPurgeResponse(BaseModel):
    """Summary of purged records across memory and conversation categories."""

    model_config = ConfigDict(extra="forbid")

    purged_memories_count: int = 0
    purged_candidates_count: int = 0
    purged_conversations_count: int = 0
