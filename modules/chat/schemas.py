"""Data transfer objects and validation schemas for chat retrieval, context assembly, and citations."""

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

MAX_RETRIEVAL_LIMIT = 50
MAX_SOURCE_SCOPE = 100
MAX_ENTITY_SCOPE = 100
MAX_SELECTED_REFS = 100
MAX_QUERY_LENGTH = 1000
MAX_QUOTE_LENGTH = 1000
DEFAULT_CONTEXT_BUDGET_BYTES = 32_000
MAX_CONTEXT_BUDGET_BYTES = 64_000


class SelectedEvidenceRef(BaseModel):
    """Reference to a specific versioned chunk explicitly selected by client context."""

    model_config = ConfigDict(extra="forbid")

    document_version_id: UUID
    chunk_id: UUID
    document_id: UUID | None = None


class Citation(BaseModel):
    """Exact evidence citation reference grounded in retrieved revision chunks.

    Adheres to the canonical JSON schema shape:
    {"sourceType":"document","sourceId":"uuid","documentId":"uuid","documentVersionId":"uuid","chunkId":"uuid","title":"Note","url":null,"observedAt":"2026-09-25T03:00:00Z","quote":"Evidence"}
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    sourceType: Literal["document"] = Field(default="document", alias="source_type")
    sourceId: UUID = Field(alias="source_id")
    documentId: UUID = Field(alias="document_id")
    documentVersionId: UUID = Field(alias="document_version_id")
    chunkId: UUID = Field(alias="chunk_id")
    title: str = Field(min_length=1, max_length=500)
    url: str | None = None
    observedAt: datetime | None = Field(default=None, alias="observed_at")
    quote: str = Field(min_length=1, max_length=MAX_QUOTE_LENGTH)


class EvidenceItem(BaseModel):
    """Detached evidence representation with full chunk content and source policy snapshot."""

    model_config = ConfigDict(extra="forbid")

    source_id: UUID
    source_type: str = "document"
    source_generation: int
    local_only: bool
    document_id: UUID
    document_version_id: UUID
    version_number: int
    chunk_id: UUID
    chunk_index: int = 0
    content: str
    title: str
    canonical_url: str | None = None
    observed_at: datetime | None = None
    published_at: datetime | None = None
    metadata_is_version_snapshot: bool = False
    score: float = 0.0


class EntityContextItem(BaseModel):
    """Safe projection of an entity and its supporting references for grounded context."""

    model_config = ConfigDict(extra="forbid")

    entity_id: UUID
    name: str | None = None
    canonical_name: str | None = None
    entity_type: str
    description: str | None = None
    backing_refs: list[Citation] = Field(default_factory=list)
    neighbors: list[dict[str, Any]] = Field(default_factory=list)


class TemporalContextItem(BaseModel):
    """Safe projection of a timeline event and its supporting references for grounded context."""

    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    title: str
    event_type: str | None = None
    timestamp: datetime | None = None
    summary: str | None = None
    backing_refs: list[Citation] = Field(default_factory=list)


class AnswerContextRequest(BaseModel):
    """Input parameters for bounded grounded context retrieval."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH)
    source_scope: list[UUID] = Field(default_factory=list, max_length=MAX_SOURCE_SCOPE)
    entity_ids: list[UUID] = Field(default_factory=list, max_length=MAX_ENTITY_SCOPE)
    date_context: datetime | date | str | None = None
    timezone: str | None = None
    selected_refs: list[SelectedEvidenceRef] = Field(default_factory=list, max_length=MAX_SELECTED_REFS)
    limit: int = Field(default=20, ge=1, le=MAX_RETRIEVAL_LIMIT)
    context_budget_bytes: int = Field(
        default=DEFAULT_CONTEXT_BUDGET_BYTES, ge=1000, le=MAX_CONTEXT_BUDGET_BYTES
    )
    mode: Literal["lexical", "hybrid"] = "hybrid"
    allow_hybrid: bool = True


class AnswerContext(BaseModel):
    """Assembled grounded retrieval context bounded by privacy fences and context budgets."""

    model_config = ConfigDict(extra="forbid")

    query: str
    source_scope: list[UUID] = Field(default_factory=list)
    entity_ids: list[UUID] = Field(default_factory=list)
    date_context: str | None = None
    timezone: str | None = None
    evidence: list[EvidenceItem] = Field(default_factory=list)
    entity_summaries: list[EntityContextItem] = Field(default_factory=list)
    temporal_summaries: list[TemporalContextItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    rerank_status: Literal["applied", "unavailable", "skipped"] = "skipped"
    has_sufficient_evidence: bool = False
    total_evidence_bytes: int = 0
    fence_snapshot: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, Any] | None = None


class CitationValidationResult(BaseModel):
    """Result of pure citation validation against the retrieved evidence set."""

    model_config = ConfigDict(extra="forbid")

    is_valid: bool
    valid_citations: list[Citation] = Field(default_factory=list)
    rejected_citations: list[dict[str, Any]] = Field(default_factory=list)
    rejection_reasons: list[str] = Field(default_factory=list)


class ValidatedAnswer(BaseModel):
    """Grounded answer with verified citations and sufficiency indicator."""

    model_config = ConfigDict(extra="forbid")

    answer: str
    citations: list[Citation] = Field(default_factory=list)
    has_sufficient_evidence: bool = True
    warnings: list[str] = Field(default_factory=list)


class ConversationCreate(BaseModel):
    """Payload for creating a new chat conversation."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=255)
    context_kind: str | None = Field(default=None, max_length=32)
    context_resource_id: UUID | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConversationPatch(BaseModel):
    """Payload for updating conversation metadata or pinned/archived state."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=255)
    pinned: bool | None = None
    archived: bool | None = None
    metadata: dict[str, Any] | None = None


class ConversationRead(BaseModel):
    """Public representation of a chat conversation header."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    title: str
    context_kind: str | None = None
    context_resource_id: UUID | None = None
    pinned: bool = False
    archived: bool = False
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class MessageRead(BaseModel):
    """Public representation of a chat message within a conversation."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    conversation_id: UUID
    role: str
    content: str
    client_request_id: str | None = None
    model_identity: str | None = None
    citations: list[dict[str, Any]] = Field(default_factory=list)
    response_id: UUID | None = None
    created_at: datetime


class ConversationDetailRead(ConversationRead):
    """Full conversation projection including ordered messages."""

    messages: list[MessageRead] = Field(default_factory=list)


class SendMessageRequest(BaseModel):
    """Payload for sending a user message within an existing conversation."""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=20000)
    client_request_id: str | None = Field(default=None, max_length=128)
    context: dict[str, Any] | None = None


class SendMessageResponse(BaseModel):
    """Response acknowledging message reception and dispatching response run."""

    model_config = ConfigDict(extra="forbid")

    message_id: UUID
    response_id: UUID
    status: str


class ResponseRunRead(BaseModel):
    """Status and metadata of an active or completed response run."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    conversation_id: UUID
    user_message_id: UUID
    assistant_message_id: UUID | None = None
    client_request_id: str | None = None
    status: str
    model_alias: str | None = None
    model_name: str | None = None
    provider: str | None = None
    error_code: str | None = None
    token_usage: dict[str, Any] = Field(default_factory=dict)
    citations: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class AgentActivityRead(BaseModel):
    """Expose only bounded identifiers and status/tool names linked to an owned conversation."""

    model_config = ConfigDict(extra="forbid")
    conversation_id: UUID
    agent_run_id: UUID
    activities: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    updated_at: datetime


class CancelResponse(BaseModel):
    """Outcome of a response run cancellation request."""

    model_config = ConfigDict(extra="forbid")

    response_id: UUID
    status: str

