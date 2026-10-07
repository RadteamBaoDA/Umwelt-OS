import json
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

EntityType = Literal[
    "person", "organization", "company", "project", "repository", "place", "country",
    "product", "topic", "technology", "asset", "device", "website", "event_subject", "other",
]


def canonicalize_name(value: str) -> str:
    """Normalize whitespace and case for entity-name identity comparisons."""
    return " ".join(value.split()).casefold()


def validate_metadata(value: dict[str, Any]) -> dict[str, Any]:
    """Reject non-JSON values and metadata whose compact UTF-8 form exceeds 64 KiB."""
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > 65_536:
        raise ValueError("metadata exceeds 64 KiB")
    return value


class EntityCreate(BaseModel):
    """Validate owner-created entity fields, aliases, metadata, and audit reason."""
    model_config = ConfigDict(extra="forbid")
    type: EntityType
    name: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=20_000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    aliases: list[str] = Field(default_factory=list, max_length=100)
    reason: str = Field(default="owner_create", min_length=1, max_length=300)

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        """Collapse name whitespace and reject names that become blank."""
        value = " ".join(value.split())
        if not value:
            raise ValueError("name cannot be blank")
        return value

    @field_validator("aliases")
    @classmethod
    def clean_aliases(cls, values: list[str]) -> list[str]:
        """Normalize aliases and reject blanks, overlong values, or duplicate names."""
        aliases = [" ".join(value.split()) for value in values]
        if any(not value or len(value) > 300 for value in aliases):
            raise ValueError("aliases must contain 1 to 300 characters")
        if len({canonicalize_name(value) for value in aliases}) != len(aliases):
            raise ValueError("aliases must be unique")
        return aliases

    @field_validator("metadata")
    @classmethod
    def bounded_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Apply the shared JSON size and finiteness bound to entity metadata."""
        return validate_metadata(value)


class EntityPatch(BaseModel):
    """Validate an optimistic entity update with its expected revision and reason."""
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    reason: str = Field(default="owner_update", min_length=1, max_length=300)
    name: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=20_000)
    metadata: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str | None) -> str | None:
        """Normalize a supplied name while preserving an omitted/null value."""
        if value is None:
            return value
        value = " ".join(value.split())
        if not value:
            raise ValueError("name cannot be blank")
        return value

    @field_validator("metadata")
    @classmethod
    def bounded_metadata(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        """Bound replacement metadata while preserving an omitted/null value."""
        return validate_metadata(value) if value is not None else value


class AliasCreate(BaseModel):
    """Validate one owner-confirmed alias and its correction reason."""
    model_config = ConfigDict(extra="forbid")
    alias: str = Field(min_length=1, max_length=300)
    confirmed: bool = True
    reason: str = Field(default="owner_alias", min_length=1, max_length=300)

    @field_validator("alias")
    @classmethod
    def trim_alias(cls, value: str) -> str:
        """Collapse alias whitespace and reject a blank normalized alias."""
        value = " ".join(value.split())
        if not value:
            raise ValueError("alias cannot be blank")
        return value


class EntityAliasRead(BaseModel):
    """Serialize an alias with confirmation, provenance, and confidence fields."""
    id: UUID
    entity_id: UUID
    alias: str
    source_id: UUID | None
    confirmed: bool
    origin: Literal["owner", "derived"] | None
    confidence: float | None
    created_at: datetime


class EntityRead(BaseModel):
    """Serialize canonical entity state, revision, field origins, and aliases."""
    id: UUID
    type: EntityType
    name: str | None
    canonical_name: str | None
    description: str | None
    metadata: dict[str, Any]
    revision: int
    name_origin: Literal["owner", "derived"] | None = None
    description_origin: Literal["owner", "derived"] | None = None
    first_seen_at: datetime | None
    last_seen_at: datetime | None
    created_at: datetime
    updated_at: datetime
    aliases: list[EntityAliasRead] = Field(default_factory=list)


class EntityPage(BaseModel):
    """Return a bounded entity page and its optional continuation cursor."""
    items: list[EntityRead]
    next_cursor: str | None


class EntityExportAliasEvidence(BaseModel):
    """Identify one exact alias support membership and its eligible source generation."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    membership_id: UUID
    source_id: UUID
    source_generation: int = Field(ge=1)
    confidence: float = Field(ge=0, le=1)


class EntityExportAlias(BaseModel):
    """Expose one canonical alias and exact eligible support, without creator-source metadata."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    alias: str
    confirmed: bool
    origin: Literal["owner", "derived"] | None
    confidence: float | None
    created_at: datetime
    supports: list[EntityExportAliasEvidence] = Field(max_length=100)


class EntityExportEvidence(BaseModel):
    """Expose stable citation identity for one retained entity support membership."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    source_id: UUID
    source_generation: int = Field(ge=1)
    document_id: UUID
    document_version_id: UUID
    chunk_id: UUID
    observed_at: datetime
    extracted_at: datetime
    confidence: float


class EntitySourceExportFence(BaseModel):
    """Capture one public source generation used by the entity's citation references."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: UUID
    generation: int = Field(ge=1)


class EntityExportRead(BaseModel):
    """Serialize allowlisted canonical entity facts and exact retained provenance."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    record_kind: Literal["entity"] = "entity"
    id: UUID
    type: EntityType
    name: str | None
    canonical_name: str | None
    description: str | None
    revision: int = Field(ge=1)
    name_origin: Literal["owner", "derived"] | None
    description_origin: Literal["owner", "derived"] | None
    first_seen_at: datetime | None
    last_seen_at: datetime | None
    created_at: datetime
    updated_at: datetime
    aliases: list[EntityExportAlias] = Field(max_length=100)
    evidence: list[EntityExportEvidence] = Field(max_length=100)


class EntityExportFence(BaseModel):
    """Bind a canonical entity page item to its live revision and support identities."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    created_at: datetime
    updated_at: datetime
    revision: int = Field(ge=1)
    alias_ids: list[UUID] = Field(max_length=100)
    evidence_ids: list[UUID] = Field(max_length=100)
    source_fences: list[EntitySourceExportFence] = Field(max_length=100)
    alias_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class EntityExportPage(BaseModel):
    """Return one bounded canonical entity export page and its validation fences."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    owner_id: int = Field(ge=1)
    record_kind: Literal["entities"]
    snapshot_at: datetime
    snapshot_count: int = Field(ge=0)
    items: list[EntityExportRead] = Field(max_length=100)
    fences: list[EntityExportFence] = Field(max_length=100)
    payload_bytes: int = Field(ge=0, le=16_777_216)
    max_payload_bytes: int = Field(default=16_777_216, ge=1, le=16_777_216)
    next_cursor: str | None = None
    available: bool = True
    omission_reason: None = None


class EntityExportFenceValidation(BaseModel):
    """Report whether a captured entity count, revision, and citation set remain current."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    valid: bool
    reason: Literal["valid", "owner_unavailable", "snapshot_count_changed", "record_changed"]
    observed_snapshot_count: int = Field(ge=0)


class EntityExtractionStatus(BaseModel):
    """Expose extraction progress and any facts or candidates awaiting review."""
    document_version_id: UUID
    status: Literal["pending", "running", "succeeded", "blocked", "failed"]
    attempt: int
    error_code: str | None
    model: str | None = None
    facts: list[dict[str, Any]] = Field(default_factory=list)
    review_candidates: list[dict[str, Any]] = Field(default_factory=list)
    completed_at: datetime | None = None


class EntityReferenceRead(BaseModel):
    """Represent a requested entity ID resolved to its current canonical record."""
    requested_id: UUID
    canonical_id: UUID
    revision: int
    type: EntityType
    name: str | None


class EntityMembershipReferenceRead(BaseModel):
    """Identify an evidence membership and its document chunk and timestamps."""
    id: UUID
    entity_id: UUID
    document_version_id: UUID
    chunk_id: UUID
    observed_at: datetime
    extracted_at: datetime
    confidence: float


class VersionMembershipReference(BaseModel):
    """Expose a bounded current-version membership key, canonical entity revision, chunk, and observation."""
    membership_id: UUID
    entity_id: UUID
    entity_type: EntityType
    entity_revision: int
    chunk_id: UUID
    observed_at: datetime
    name: str | None


class EntityEvidenceRead(BaseModel):
    """Serialize evidence with version snapshot metadata and its source details."""
    id: UUID
    entity_id: UUID
    document_id: UUID
    document_version_id: UUID
    version_number: int
    chunk_id: UUID
    observed_at: datetime
    extracted_at: datetime
    confidence: float
    source_id: UUID
    title: str
    canonical_url: str | None
    metadata_is_version_snapshot: bool
    excerpt: str


class EntityEvidencePage(BaseModel):
    """Return a bounded entity-evidence page and optional continuation cursor."""
    items: list[EntityEvidenceRead]
    next_cursor: str | None


class EntityReviewEvidence(BaseModel):
    """Provide provenance and excerpt fields used to review extracted evidence."""
    document_id: UUID
    document_version_id: UUID
    version_number: int
    chunk_id: UUID
    source_id: UUID
    source_name: str
    title: str
    canonical_url: str | None
    metadata_is_version_snapshot: bool
    observed_at: datetime
    excerpt: str


class EntityReviewEndpoint(BaseModel):
    """Describe whether a review candidate endpoint is assigned, absent, or ambiguous."""
    state: Literal["assigned", "unassigned", "ambiguous"]
    entity_id: UUID | None = None
    entity_name: str | None = None
    entity_type: EntityType | None = None
    membership_id: UUID | None = None


class EntityReviewCandidate(BaseModel):
    """Serialize an entity or relationship candidate with snapshot and evidence context."""
    kind: Literal["entity", "relationship"] = "entity"
    candidate_id: UUID | None = None
    work_id: UUID
    result_id: UUID
    snapshot_digest: str | None = None
    document_version_id: UUID
    source_generation: int
    owner_generation: int | None = None
    document_id: UUID | None = None
    version_number: int | None = None
    source_id: UUID | None = None
    source_name: str | None = None
    evidence: list[EntityReviewEvidence] = Field(default_factory=list)
    relationship_type: str | None = None
    source_endpoint: EntityReviewEndpoint | None = None
    target_endpoint: EntityReviewEndpoint | None = None
    actionable: bool = False
    status: Literal["pending", "running", "succeeded", "blocked", "failed"]
    candidate_name: str
    candidate_type: str | None = None
    reason: str
    possible_entity_ids: list[UUID] = Field(default_factory=list)


class EntityReviewAssignmentRequest(BaseModel):
    """Fence an owner assignment against result, source, owner, and target revisions."""
    model_config = ConfigDict(extra="forbid")
    result_id: UUID
    snapshot_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_source_generation: int = Field(ge=1)
    expected_owner_generation: int = Field(ge=1)
    target_entity_id: UUID
    expected_target_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=300)
    future_document_id: UUID | None = None


class EntityReviewAssignmentResult(BaseModel):
    """Report the assigned candidate, created memberships, and resulting revision."""
    candidate_id: UUID
    target_entity_id: UUID
    membership_ids: list[UUID]
    revision: int


class EntityRelationshipReviewRequest(BaseModel):
    """Fence relationship review against the extraction snapshot and generations."""
    model_config = ConfigDict(extra="forbid")
    result_id: UUID
    snapshot_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_source_generation: int = Field(ge=1)
    expected_owner_generation: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=300)


class EntityRelationshipReviewResult(BaseModel):
    """Report the candidate and relationship created by owner review."""
    candidate_id: UUID
    relationship_id: UUID


class EntityReviewPage(BaseModel):
    """Return review candidates with an optional continuation cursor."""
    items: list[EntityReviewCandidate]
    next_cursor: str | None


class EntityMergeRequest(BaseModel):
    """Validate merge target and both entity revisions before correction."""
    model_config = ConfigDict(extra="forbid")
    into_id: UUID
    expected_revision: int = Field(ge=1)
    expected_into_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=300)
    future_document_id: UUID | None = None


class EntitySplitRequest(BaseModel):
    """Validate evidence memberships to move and the replacement entity payload."""
    model_config = ConfigDict(extra="forbid")
    evidence_ids: list[UUID] = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=1)
    new_entity: EntityCreate
    reason: str = Field(min_length=1, max_length=300)
    future_document_id: UUID | None = None

    @field_validator("evidence_ids")
    @classmethod
    def unique_evidence(cls, value: list[UUID]) -> list[UUID]:
        """Reject repeated membership IDs so one split applies each item once."""
        if len(set(value)) != len(value):
            raise ValueError("Evidence membership IDs must be unique")
        return value


class EntitySuppressionRequest(BaseModel):
    """Validate evidence memberships to suppress and the expected entity revision."""
    model_config = ConfigDict(extra="forbid")
    evidence_ids: list[UUID] = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=300)
    future_document_id: UUID | None = None

    @field_validator("evidence_ids")
    @classmethod
    def unique_evidence(cls, value: list[UUID]) -> list[UUID]:
        """Reject repeated membership IDs so one suppression applies each once."""
        if len(set(value)) != len(value):
            raise ValueError("Evidence membership IDs must be unique")
        return value


class EntityCorrectionResult(BaseModel):
    """Report correction outcome, canonical target, replacements, and conflicts."""
    operation: Literal["merge", "split", "suppress"]
    entity_id: UUID
    canonical_entity_id: UUID
    replacement_entity_ids: list[UUID]
    revision: int
    conflicts: list[dict[str, Any]] = Field(default_factory=list)


class EntityCorrectionConflict(BaseModel):
    """Identify a correction conflict and the involved entity, evidence, and links."""
    code: str
    message: str
    entity_ids: list[UUID] = Field(default_factory=list)
    membership_ids: list[UUID] = Field(default_factory=list)
    relationship_ids: list[UUID] = Field(default_factory=list)


class EntityCorrectionPreview(BaseModel):
    """Summarize merge or split scope and conflicts without applying the change."""
    operation: Literal["merge", "split"]
    entity_ids: list[UUID]
    membership_ids: list[UUID]
    relationship_ids: list[UUID]
    evidence_ref_count: int
    conflicts: list[EntityCorrectionConflict] = Field(default_factory=list)


class EvidenceRef(BaseModel):
    """Reference a versioned chunk and optional endpoint memberships as evidence."""
    model_config = ConfigDict(extra="forbid")
    document_version_id: UUID
    chunk_id: UUID
    confidence: float = Field(ge=0, le=1)
    source_membership_id: UUID | None = None
    target_membership_id: UUID | None = None


class EntityTemporalNodeSeed(BaseModel):
    """Detached current field proof over exactly selected source-local memberships."""
    entity_id: UUID
    revision: int
    type: str
    name: str
    summary: str | None
    source_id: UUID
    source_generation: int
    memberships: list[EntityMembershipReferenceRead]
    name_support_membership_ids: list[UUID]
    summary_support_membership_ids: list[UUID]
    name_hash: str
    summary_hash: str | None


class EntityHistoryItem(BaseModel):
    """Expose identifier-only correction audit without retaining reasons or deleted text."""
    id: UUID
    recorded_at: datetime
    operation: str
    affected_ids: list[UUID]
    revisions: dict[str, int | None]


class EntityHistoryPage(BaseModel):
    """Page owner edits separately from currently retained membership observations."""
    items: list[EntityHistoryItem]
    next_cursor: str | None
    historical_values_available: bool = False
    memberships: list[EntityEvidenceRead] = Field(default_factory=list)
    membership_next_cursor: str | None = None
