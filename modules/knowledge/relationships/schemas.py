from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from modules.knowledge.entities.schemas import EvidenceRef, validate_metadata


class EntityGraphRead(BaseModel):
    """Serialize the minimal entity identity embedded in a graph result."""
    id: UUID
    type: str
    name: str | None
    revision: int


class RelationshipCreate(BaseModel):
    """Validate an owner-authorized relationship request and its origin/evidence fields.

    ``origin`` may be ``owner`` or ``derived``; request authorization does not
    determine the stored fact's origin. Derived relationships require evidence
    with both endpoint memberships, while owner relationships may omit evidence.
    """
    model_config = ConfigDict(extra="forbid")
    source_entity_id: UUID
    target_entity_id: UUID
    type: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")
    origin: Literal["owner", "derived"] = "owner"
    confidence: float | None = Field(default=None, ge=0, le=1)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=100)
    reason: str = Field(default="owner_relationship", min_length=1, max_length=300)

    @field_validator("metadata")
    @classmethod
    def bounded_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Apply the entity metadata JSON and size constraints to relationship metadata."""
        return validate_metadata(value)


class EvidenceRead(BaseModel):
    """Serialize relationship evidence with exact version, chunk, and source provenance."""
    id: UUID
    relationship_id: UUID
    document_id: UUID
    document_version_id: UUID
    version_number: int
    chunk_id: UUID
    observed_at: datetime
    extracted_at: datetime
    confidence: float
    source_entity_membership_id: UUID | None
    target_entity_membership_id: UUID | None
    title: str
    canonical_url: str | None
    source_id: UUID
    excerpt: str
    metadata_is_version_snapshot: bool


class RelationshipRead(BaseModel):
    """Serialize canonical state; any nullable validity boundary is explicitly unknown."""
    id: UUID
    source_entity_id: UUID
    target_entity_id: UUID
    type: str
    origin: Literal["owner", "derived"]
    confidence: float | None
    valid_from: datetime | None
    valid_to: datetime | None
    metadata: dict[str, Any]
    created_at: datetime
    evidence: list[EvidenceRead] = Field(default_factory=list)
    validity_precision: Literal["bounded", "unknown"] = "unknown"


class RelationshipPage(BaseModel):
    """Page canonical rows with separate historical coverage and observed-time controls.

    Unavailable identifiers are bounded hints, not fabricated historical values;
    current evidence observations remain separately pageable through evidence API.
    """
    items: list[RelationshipRead]
    next_cursor: str | None
    canonical_history_available: bool = True
    knowledge_as_of: datetime | None = None
    observation_history_only: bool = False
    unavailable_relationship_ids: list[UUID] = Field(default_factory=list)


class EvidencePage(BaseModel):
    """Return relationship evidence with its optional continuation cursor."""
    items: list[EvidenceRead]
    next_cursor: str | None


class NeighborRead(BaseModel):
    """Pair one adjacent entity with the relationship connecting it to the focus."""
    entity: EntityGraphRead
    relationship: RelationshipRead


class NeighborPage(BaseModel):
    """Return bounded neighbors, truncation state, and any continuation cursor."""
    items: list[NeighborRead]
    truncated: bool
    next_cursor: str | None


class CorrectionSupportRef(BaseModel):
    """Identify support evidence and endpoint memberships used by a correction."""
    id: UUID
    document_id: UUID | None
    source_id: UUID | None
    document_version_id: UUID
    chunk_id: UUID
    source_membership_id: UUID | None
    target_membership_id: UUID | None
    confidence: float


class CorrectionRelationshipRef(BaseModel):
    """Describe a relationship and its supports for entity merge or split planning."""
    id: UUID
    source_entity_id: UUID
    target_entity_id: UUID
    type: str
    origin: Literal["owner", "derived"]
    valid_from: datetime | None
    valid_to: datetime | None
    metadata: dict[str, Any]
    supports: list[CorrectionSupportRef]


class RelationshipSnapshot(BaseModel):
    """Complete detached current owner state and exact support digest; no fake revision."""
    relationship: RelationshipRead
    endpoints: list[dict[str, Any]]
    memberships: list[dict[str, Any]]
    supports: list[dict[str, Any]]
    source_generations: dict[str, int]
    digest: str
