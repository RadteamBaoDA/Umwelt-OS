"""Data transfer objects and validation schemas for chat retrieval, context assembly, and citations."""

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from modules.knowledge.documents.schemas import GadgetDocumentSelectionFence

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
    source_id: UUID | None = None


class SelectedDocumentVersion(BaseModel):
    """Identify one exact current document version selected by an owner gadget."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    source_id: UUID = Field(alias="sourceId")
    document_id: UUID = Field(alias="documentId")
    document_version_id: UUID = Field(alias="documentVersionId")
    chunk_id: UUID | None = Field(default=None, alias="chunkId")


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
    selected_only: bool = False
    selection_fences: list[GadgetDocumentSelectionFence] = Field(default_factory=list, max_length=32)
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
    selection_fences: list[GadgetDocumentSelectionFence] = Field(default_factory=list, max_length=32)
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
    revision_of_message_id: UUID | None = None
    created_at: datetime


class ChatExportCitation(BaseModel):
    """Carry one citation only while its exact owner-visible evidence still exists."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_type: Literal["document"] = "document"
    source_id: UUID
    document_id: UUID
    document_version_id: UUID
    chunk_id: UUID
    title: str = Field(min_length=1, max_length=500)
    url: str | None = Field(default=None, max_length=2048)
    observed_at: datetime | None = None
    quote: str = Field(min_length=1, max_length=MAX_QUOTE_LENGTH)
    current_source_generation: int = Field(ge=0)


class ChatExportConversationRead(BaseModel):
    """Expose only owner-visible conversation fields needed for a portable transcript."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record_kind: Literal["conversation"] = "conversation"
    id: UUID
    title: str = Field(max_length=255)
    context_kind: str | None = Field(default=None, max_length=32)
    context_resource_id: UUID | None = None
    pinned: bool
    archived: bool
    created_at: datetime
    updated_at: datetime


class ChatExportMessageRead(BaseModel):
    """Expose one retained transcript revision without internal request or run payloads."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record_kind: Literal["message"] = "message"
    id: UUID
    conversation_id: UUID
    role: Literal["user", "assistant", "system"]
    content: str
    response_id: UUID | None = None
    revision_of_message_id: UUID | None = None
    citations: list[ChatExportCitation] = Field(default_factory=list, max_length=100)
    omitted_citation_count: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime

    @field_validator("content")
    @classmethod
    def bounded_export_content(cls, value: str) -> str:
        """Keep one transcript payload within the portable export record bound."""
        if len(value.encode("utf-8")) > 1_048_576:
            raise ValueError("Chat message exceeds the export content bound")
        return value


class ChatExportCitationFence(BaseModel):
    """Bind an exported citation to its retained exact evidence and live source generation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: UUID
    document_id: UUID
    document_version_id: UUID
    chunk_id: UUID
    current_source_generation: int = Field(ge=0)


class ChatExportFence(BaseModel):
    """Bind one conversation or message record to its current owner-visible revisions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    conversation_id: UUID
    conversation_created_at: datetime
    conversation_updated_at: datetime
    message_id: UUID | None = None
    message_created_at: datetime | None = None
    message_updated_at: datetime | None = None
    citations: list[ChatExportCitationFence] = Field(default_factory=list, max_length=100)


class ChatExportPage(BaseModel):
    """Return a bounded typed owner export page and its consistency fences."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner_id: int = Field(ge=1)
    record_kind: Literal["conversations", "messages"]
    snapshot_at: datetime
    snapshot_count: int = Field(ge=0)
    items: list[ChatExportConversationRead | ChatExportMessageRead] = Field(max_length=100)
    fences: list[ChatExportFence] = Field(max_length=100)
    payload_bytes: int = Field(ge=0, le=16_777_216)
    max_payload_bytes: int = Field(default=16_777_216, ge=1, le=16_777_216)
    next_cursor: str | None = None
    available: bool
    omission_reason: Literal["conversation_history_disabled"] | None = None
    privacy_persisted: bool
    privacy_updated_at: datetime | None = None
    history_enabled: bool

    @model_validator(mode="after")
    def validate_privacy_fence(self) -> "ChatExportPage":
        """Require a persisted privacy row to have an aware timestamp fence."""
        if self.privacy_persisted != (self.privacy_updated_at is not None):
            raise ValueError("Chat export privacy persistence marker is inconsistent")
        if (self.privacy_updated_at is not None
                and (self.privacy_updated_at.tzinfo is None or self.privacy_updated_at.utcoffset() is None)):
            raise ValueError("Chat export privacy timestamp must be timezone-aware")
        return self


class ChatExportFenceValidation(BaseModel):
    """Report whether captured chat export fences remain current at finalization."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    valid: bool
    reason: Literal[
        "valid", "owner_unavailable", "conversation_history_disabled",
        "privacy_changed", "snapshot_count_changed", "record_changed",
        "citation_unavailable", "source_generation_changed",
    ]
    observed_snapshot_count: int = Field(ge=0)
    privacy_persisted: bool
    privacy_updated_at: datetime | None = None

    @model_validator(mode="after")
    def validate_privacy_fence(self) -> "ChatExportFenceValidation":
        """Require a persisted privacy row to have an aware timestamp fence."""
        if self.privacy_persisted != (self.privacy_updated_at is not None):
            raise ValueError("Chat export privacy persistence marker is inconsistent")
        if (self.privacy_updated_at is not None
                and (self.privacy_updated_at.tzinfo is None or self.privacy_updated_at.utcoffset() is None)):
            raise ValueError("Chat export privacy timestamp must be timezone-aware")
        return self


class ConversationDetailRead(ConversationRead):
    """Full conversation projection including ordered messages and any active response handle."""

    messages: list[MessageRead] = Field(default_factory=list)
    active_response_id: UUID | None = None


class SendMessageRequest(BaseModel):
    """Payload for sending a user message within an existing conversation."""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=20000)
    client_request_id: str | None = Field(default=None, max_length=128)
    context: dict[str, Any] | None = None


class MessageMutationRequest(BaseModel):
    """Append an edited user prompt or regenerate an answer with its original context."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["edit", "regenerate"]
    base_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    client_request_id: str = Field(min_length=1, max_length=128)
    content: str | None = Field(default=None, min_length=1, max_length=20_000)


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

