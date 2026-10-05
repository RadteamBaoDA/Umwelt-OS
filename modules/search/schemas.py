from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from modules.goals.schemas import GoalRead
from modules.tasks.schemas import TaskRead


class SearchFilters(BaseModel):
    """Bound source, date, and content-type filters for a search request."""
    model_config = ConfigDict(extra="forbid")

    source_ids: list[UUID] = Field(default_factory=list, max_length=100)
    date_from: datetime | None = None
    date_to: datetime | None = None
    content_types: list[str] = Field(default_factory=list, max_length=20)


class SearchRequest(BaseModel):
    """Validate query text, retrieval mode, page size, filters, and cursor."""
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=1000)
    filters: SearchFilters = Field(default_factory=SearchFilters)
    mode: Literal["lexical", "hybrid"] = "hybrid"
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=256)


class Citation(BaseModel):
    """Carry source and exact document-chunk details for a search citation."""
    sourceType: Literal["document"] = "document"
    sourceId: UUID
    documentId: UUID
    chunkId: UUID
    title: str
    url: str | None
    observedAt: datetime | None
    quote: str


class SearchSource(BaseModel):
    """Expose the source identity associated with a search hit."""
    id: UUID
    name: str
    type: str


class SearchHit(BaseModel):
    """Serialize a ranked versioned chunk with provenance and citation data."""
    document_id: UUID
    document_version_id: UUID
    version_number: int
    chunk_id: UUID
    title: str
    excerpt: str
    score: float
    source: SearchSource
    observed_at: datetime | None
    published_at: datetime | None
    content_type: str | None
    entity_refs: list[UUID] = Field(default_factory=list)
    citation: Citation


class SearchResponse(BaseModel):
    """Return ranked search hits, pagination state, effective mode, and warnings."""
    items: list[SearchHit]
    next_cursor: str | None
    effective_mode: Literal["lexical", "hybrid"]
    warnings: list[str]


class ReindexResponse(BaseModel):
    """Return the durable run identifier for an accepted reindex request."""
    run_id: UUID


class SearchIndexStatus(BaseModel):
    """Expose the current or most recent search index generation status."""
    run_id: UUID | None
    status: str
    model_id: str | None
    dimensions: int | None
    indexed_items: int
    failed_items: int


class GlobalSearchResponse(BaseModel):
    """Return independently paginated document, task, and goal search results.

    Task and goal entries retain their owners' read DTOs. Each continuation token
    pages only its corresponding domain; ``total`` counts items returned in this
    response and does not claim to count the full matching corpus.
    """

    documents: list[SearchHit] = Field(default_factory=list)
    tasks: list[TaskRead] = Field(default_factory=list)
    goals: list[GoalRead] = Field(default_factory=list)
    document_next_cursor: str | None = None
    task_next_cursor: str | None = None
    goal_next_cursor: str | None = None
    total: int = Field(
        default=0,
        description="Number of document, task, and goal items returned in this response; not a corpus count.",
    )

