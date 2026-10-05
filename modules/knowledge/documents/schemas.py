import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_CONTENT_BYTES = 1_048_576
MAX_METADATA_BYTES = 65_536


def validate_content(value: str) -> str:
    """Reject content whose UTF-8 representation exceeds the 1 MiB request bound."""
    if len(value.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise ValueError("content exceeds 1 MiB")
    return value


def validate_metadata(value: dict[str, Any]) -> dict[str, Any]:
    """Reject non-finite or non-JSON metadata and enforce the 64 KiB encoded limit."""
    encoded = json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > MAX_METADATA_BYTES:
        raise ValueError("metadata exceeds 64 KiB")
    return value


class DocumentCreate(BaseModel):
    """Validate a source-backed document and its initial content and metadata."""
    model_config = ConfigDict(extra="forbid")

    source_id: UUID
    title: str = Field(min_length=1, max_length=500)
    content: str
    external_id: str | None = Field(default=None, max_length=512)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("content")
    @classmethod
    def bounded_content(cls, value: str) -> str:
        """Apply the document content byte limit before persistence."""
        return validate_content(value)

    @field_validator("metadata")
    @classmethod
    def bounded_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Apply the shared JSON metadata size and finiteness checks."""
        return validate_metadata(value)


class NormalizedDocumentInput(BaseModel):
    """Validate collector-normalized content with source generation and provenance."""
    model_config = ConfigDict(extra="forbid")

    source_id: UUID
    expected_source_generation: int = Field(ge=1)
    observation_id: UUID
    provider_id: str = Field(min_length=1, max_length=512)
    provider_version: str | None = Field(default=None, max_length=255)
    accepted_record_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalization_version: int = Field(ge=1)
    observed_at: datetime
    received_at: datetime | None = None
    collected_at: datetime | None = None
    title: str = Field(min_length=1, max_length=500)
    canonical_url: str | None = None
    published_at: datetime | None = None
    content_type: str | None = Field(default=None, max_length=64)
    content: str
    provenance: dict[str, Any] = Field(default_factory=dict)

    @field_validator("content")
    @classmethod
    def bounded_content(cls, value: str) -> str:
        """Apply the content byte limit to normalized collector content."""
        return validate_content(value)

    @field_validator("observed_at", "received_at", "collected_at", "published_at")
    @classmethod
    def require_aware_times(cls, value: datetime | None) -> datetime | None:
        """Require timezone-aware timestamps and normalize supplied values to UTC."""
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("normalized timestamps must include a timezone")
            return value.astimezone(UTC)
        return value

    @field_validator("provenance")
    @classmethod
    def bounded_provenance(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Allow only declared provenance fields and enforce the metadata bound."""
        allowed = {"title", "canonical_url", "published_at", "content_type", "author", "language", "metadata", "provider_scope_discriminator"}
        if value.keys() - allowed:
            raise ValueError("provenance contains unsupported fields")
        return validate_metadata(value)


class NormalizedDocumentResult(BaseModel):
    """Report normalization disposition, selected revision, and generated chunks."""
    disposition: Literal["normalized", "duplicate", "tombstoned"]
    document_id: UUID | None
    document_version_id: UUID | None
    version_number: int | None
    created_version: bool
    selected_current: bool
    chunk_count: int


class DocumentPatch(BaseModel):
    """Validate mutable document metadata fields while leaving versions immutable."""
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=500)
    metadata: dict[str, Any] | None = None

    @field_validator("metadata")
    @classmethod
    def bounded_metadata(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        """Bound replacement metadata and preserve a missing or null update."""
        return validate_metadata(value) if value is not None else value


class ContentUpdate(BaseModel):
    """Request a new immutable content version using its expected current number."""
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    content: str

    @field_validator("content")
    @classmethod
    def bounded_content(cls, value: str) -> str:
        """Apply the document content byte limit to the new version."""
        return validate_content(value)


class DocumentRead(BaseModel):
    """Serialize a document's current metadata, provenance, and version state."""
    id: UUID
    source_id: UUID
    external_id: str | None
    title: str
    content_type: str | None
    mime_type: str | None
    raw_uri: str | None
    canonical_url: str | None
    author: str | None
    metadata: dict[str, Any]
    current_version: int
    content_hash: str
    extraction_status: str
    published_at: datetime | None
    observed_at: datetime | None
    language: str | None
    created_at: datetime
    updated_at: datetime


class VersionRead(BaseModel):
    """Serialize immutable content and identity fields for one document version."""
    id: UUID
    document_id: UUID
    version_number: int
    content: str
    content_hash: str
    observed_at: datetime
    created_at: datetime


class VersionList(BaseModel):
    """Return document versions with an optional continuation cursor."""
    items: list[VersionRead]
    next_cursor: str | None


class EvidenceReferenceRead(BaseModel):
    """Expose exact versioned chunk provenance and excerpt for evidence review."""
    document_id: UUID
    document_version_id: UUID
    version_number: int
    chunk_id: UUID
    source_id: UUID
    title: str
    canonical_url: str | None
    metadata_is_version_snapshot: bool
    observed_at: datetime
    excerpt: str


class DocumentList(BaseModel):
    """Return a bounded document page and its optional continuation cursor."""
    items: list[DocumentRead]
    next_cursor: str | None
