"""Request and response schemas for memory items, candidates, and privacy configuration."""

from datetime import datetime
from typing import Any, Literal
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
    kind_counts: dict[str, int] | None = None


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


class MemoryExportProvenance(BaseModel):
    """Allowlist owner memory provenance identifiers while dropping arbitrary JSON metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    conversation_id: UUID | None = None
    message_id: UUID | None = None
    source_id: UUID | None = None
    document_id: UUID | None = None
    document_version_id: UUID | None = None
    chunk_id: UUID | None = None
    origin: Literal["manual", "agent", "model"] | None = None


class MemoryExportRead(BaseModel):
    """Expose retained memory content and lifecycle through an immutable portable projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record_kind: Literal["memory"] = "memory"
    id: UUID
    content: str = Field(max_length=MAX_MEMORY_CONTENT_LENGTH)
    type: MemoryType
    provenance: MemoryExportProvenance | None = None
    confidence: float = Field(ge=0, le=1)
    reason: str | None = Field(default=None, max_length=MAX_REASON_LENGTH)
    status: MemoryStatus
    is_manual: bool
    superseded_by_id: UUID | None = None
    candidate_id: UUID | None = None
    created_at: datetime
    updated_at: datetime
    invalidated_at: datetime | None = None
    forgotten_at: datetime | None = None


class MemoryCandidateExportRead(BaseModel):
    """Expose retained candidate review outcomes without arbitrary provider metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record_kind: Literal["candidate"] = "candidate"
    id: UUID
    content: str = Field(max_length=MAX_MEMORY_CONTENT_LENGTH)
    type: MemoryType
    provenance: MemoryExportProvenance | None = None
    confidence: float = Field(ge=0, le=1)
    novelty_score: float = Field(ge=0, le=1)
    usefulness_score: float = Field(ge=0, le=1)
    reason: str | None = Field(default=None, max_length=MAX_REASON_LENGTH)
    status: CandidateStatus
    rejection_reason: str | None = Field(default=None, max_length=MAX_REASON_LENGTH)
    created_at: datetime
    updated_at: datetime
    evaluated_at: datetime | None = None


class MemoryExportFence(BaseModel):
    """Bind one selected memory record to its current timestamps and portable content digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record_kind: Literal["memory", "candidate"]
    id: UUID
    created_at: datetime
    updated_at: datetime
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_id: UUID | None = None
    source_generation: int | None = Field(default=None, ge=1)
    document_id: UUID | None = None
    document_version_id: UUID | None = None
    chunk_id: UUID | None = None
    conversation_id: UUID | None = None
    message_id: UUID | None = None
    chat_evidence_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    chat_privacy_persisted: bool | None = None
    chat_privacy_updated_at: datetime | None = None


class MemoryExportPage(BaseModel):
    """Return one bounded memory or candidate page and the final-validation fences."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner_id: int = Field(ge=1)
    record_kind: Literal["memories", "candidates"]
    snapshot_at: datetime
    snapshot_count: int = Field(ge=0)
    omitted_count: int = Field(default=0, ge=0, le=100)
    items: list[MemoryExportRead | MemoryCandidateExportRead] = Field(max_length=100)
    fences: list[MemoryExportFence] = Field(max_length=100)
    payload_bytes: int = Field(ge=0, le=16_777_216)
    max_payload_bytes: int = Field(default=16_777_216, ge=1, le=16_777_216)
    next_cursor: str | None = None
    available: bool = True
    omission_reason: Literal["unsupported_provenance"] | None = None


class MemoryExportFenceValidation(BaseModel):
    """Report whether selected memory records and the cutoff-bound inventory remain unchanged."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    valid: bool
    reason: Literal["valid", "owner_unavailable", "snapshot_count_changed", "record_changed"]
    observed_snapshot_count: int = Field(ge=0)


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
