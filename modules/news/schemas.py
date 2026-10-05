"""Bounded detached contracts for news stories, trends, and recorded scoring."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class StoryFilter(BaseModel):
    """Validate a bounded, cursor-stable owner story query."""

    model_config = ConfigDict(extra="forbid")
    source_ids: list[UUID] = Field(default_factory=list, max_length=32)
    topic_id: UUID | None = None
    entity_id: UUID | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    q: str | None = Field(default=None, max_length=200)
    limit: int = Field(default=25, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=4096)

    @field_validator("source_ids")
    @classmethod
    def unique_source_ids(cls, values: list[UUID]) -> list[UUID]:
        """Reject duplicate source IDs so cursor scope is canonical."""
        if len(set(values)) != len(values):
            raise ValueError("source_ids must be unique")
        return values

    @field_validator("date_from", "date_to")
    @classmethod
    def dates_are_aware(cls, value: datetime | None) -> datetime | None:
        """Require UTC-aware instants so half-open time filters never depend on server locale."""
        if value is not None and value.utcoffset() is None:
            raise ValueError("Story dates must include a timezone")
        return value


class StoryEvidence(BaseModel):
    """Identify one current source observation without exposing stored ORM rows."""

    document_id: UUID
    document_version_id: UUID
    chunk_id: UUID
    source_id: UUID
    source_name: str
    source_type: str
    provider: str | None
    url: str | None
    title: str
    excerpt: str
    observed_at: datetime
    published_at: datetime | None


class StoryRead(BaseModel):
    """Represent a story using only its currently visible evidence."""

    id: UUID
    title: str
    excerpt: str
    summary_method: Literal["excerpt"] = "excerpt"
    generated: bool = False
    observed_at: datetime
    source_count: int
    evidence_count: int
    evidence: list[StoryEvidence]
    incomplete_reasons: list[str] = Field(default_factory=list, max_length=8)
    relevance: float | None = None
    why_relevant: list[str] = Field(default_factory=list)
    relevance_signals: dict[str, dict[str, object]] = Field(default_factory=dict)
    relevance_weights: dict[str, float] = Field(default_factory=dict)
    relevance_profile_revisions: list[dict[str, str | int]] = Field(default_factory=list, max_length=2000)
    relevance_as_of: datetime | None = None
    relevance_state: Literal["available", "partial", "unavailable"] = "unavailable"


class StoryPage(BaseModel):
    """Return a bounded owner story page with truncation and safe incompleteness reasons."""

    items: list[StoryRead]
    next_cursor: str | None
    as_of: datetime
    truncated: bool = False
    incomplete_reasons: list[str] = Field(default_factory=list, max_length=8)
    capability: Literal["available", "partial", "unavailable"] = "available"


class StoryDetail(BaseModel):
    """Return an optionally current story and a separately paged bounded evidence list.

    A null story is an explicit partial page: its omission reasons explain why no
    current support was returned, and the cursor may continue the bounded scan.
    """

    story: StoryRead | None
    evidence_cursor: str | None = None
    incomplete_reasons: list[str] = Field(default_factory=list, max_length=8)


class TrendFilter(BaseModel):
    """Bound trend reads to an owner-selected set of source IDs."""

    model_config = ConfigDict(extra="forbid")
    source_ids: list[UUID] = Field(default_factory=list, max_length=32)
    limit: int = Field(default=25, ge=1, le=100)

    @field_validator("source_ids")
    @classmethod
    def unique_source_ids(cls, values: list[UUID]) -> list[UUID]:
        """Reject duplicate source identifiers in bounded trend queries."""
        if len(set(values)) != len(values):
            raise ValueError("source_ids must be unique")
        return values


class TrendRead(BaseModel):
    """Explain the fixed 24-hour window against its preceding seven-day baseline."""

    story_id: UUID
    title: str
    trend: Literal["rising"]
    current_count: int
    baseline_per_day: float
    ratio: float | None
    low_baseline: bool
    source_count: int
    evidence: list[StoryEvidence]
    incomplete_reasons: list[str] = Field(default_factory=list, max_length=8)
    current_window_start: datetime
    current_window_end: datetime
    baseline_window_start: datetime
    baseline_window_end: datetime
    as_of: datetime


class TrendPage(BaseModel):
    """Return bounded trend candidates and explicit history or scope omission reasons."""

    items: list[TrendRead]
    as_of: datetime
    history_days: int = 8
    truncated: bool = False
    incomplete: bool = False
    incomplete_reasons: list[str] = Field(default_factory=list, max_length=8)


class RecordedSignal(BaseModel):
    """Store one normalized score and whether supporting evidence was available."""

    value: float = Field(ge=0, le=1)
    available: bool
    method: str
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)


class RelevanceRead(BaseModel):
    """Explain the equal-weight score and preserve each signal's provenance."""

    score: float = Field(ge=0, le=1)
    why_relevant: list[str]
    signals: dict[Literal["topic", "entity", "goal", "project", "recency", "importance", "novelty"], RecordedSignal]
    weights: dict[str, float]
    profile_revisions: list[dict[str, str | int]] = Field(default_factory=list, max_length=2000)
    formula_version: int = 1
    as_of: datetime
    complete: bool = True


class StoryCursor(BaseModel):
    """Carry canonical keyset order and immutable filter snapshot in an opaque cursor."""

    owner_id: int
    filter_hash: str
    as_of: datetime
    after_observed_at: datetime
    after_story_id: UUID
    resolved_source_ids: list[UUID] = Field(max_length=32)
    source_selection_incomplete: bool = False

    @field_validator("resolved_source_ids")
    @classmethod
    def cursor_sources_are_unique(cls, values: list[UUID]) -> list[UUID]:
        """Keep the resolved default-source snapshot canonical and bounded."""
        if len(set(values)) != len(values):
            raise ValueError("Cursor source IDs must be unique")
        return values

    @field_validator("as_of", "after_observed_at")
    @classmethod
    def cursor_dates_are_aware(cls, value: datetime) -> datetime:
        """Reject naive cursor timestamps before they can change keyset ordering."""
        if value.utcoffset() is None:
            raise ValueError("Story cursor timestamps must include a timezone")
        return value
