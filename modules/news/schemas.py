"""Bounded detached contracts for news stories, trends, and recorded scoring."""

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator


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


class BriefStoryEvidence(BaseModel):
    """Identify one exact story support usable in a bounded saved-brief dependency."""

    document_id: UUID
    document_version_id: UUID
    chunk_id: UUID
    source_id: UUID


class BriefStorySupport(BaseModel):
    """Return a current story fact and its complete exact support set for Dashboard capture."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    story_id: UUID
    title: str
    source_ids: list[UUID] = Field(max_length=100)
    evidence: list[BriefStoryEvidence] = Field(max_length=100)
    complete: bool


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


class CorrelationQuery(BaseModel):
    """Bound an evidence correlation request to explicit sources, regions, and a 30-day window."""
    model_config = ConfigDict(extra="forbid")
    source_ids: list[UUID] = Field(default_factory=list, max_length=32)
    regions: list[str] = Field(min_length=1, max_length=32)
    from_at: datetime
    to_at: datetime
    limit_per_domain: StrictInt = Field(default=100, ge=1, le=100)

    @field_validator("from_at", "to_at")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        """Require explicit offsets and normalize correlation instants to UTC."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("correlation time bounds must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_scope(self) -> "CorrelationQuery":
        """Reject duplicate selectors and prevent unbounded history reads."""
        if (
            self.from_at >= self.to_at or self.to_at - self.from_at > timedelta(days=30)
            or len(self.source_ids) != len(set(self.source_ids))
            or len(self.regions) != len(set(self.regions))
            or any(not region or region != region.strip() or len(region) > 80 for region in self.regions)
        ):
            raise ValueError("correlation scope is invalid")
        return self


class CorrelationBucketRead(BaseModel):
    """Describe one region and exact UTC hour containing evidence-backed signals."""
    region: str
    window_start: datetime
    window_end: datetime
    signal_count: StrictInt = Field(ge=1, le=500)
    domain_counts: dict[str, StrictInt]
    domains_present: list[str] = Field(max_length=4)
    signal_ids: list[str] = Field(max_length=500)
    event_ids: list[UUID] = Field(max_length=400)
    observation_ids: list[UUID] = Field(max_length=100)
    document_ids: list[UUID] = Field(max_length=500)
    document_version_ids: list[UUID] = Field(max_length=500)
    event_evidence_ids: list[UUID] = Field(max_length=500)
    omitted_document_ids: StrictInt = Field(default=0, ge=0)
    omitted_document_version_ids: StrictInt = Field(default=0, ge=0)
    omitted_event_evidence_ids: StrictInt = Field(default=0, ge=0)


class CorrelationCoverageRead(BaseModel):
    """Show domain coverage, omitted source count, and bounded-query truncation explicitly."""
    signal_count: StrictInt = Field(ge=0, le=100)
    available: bool
    truncated: bool = False
    omitted_source_count: StrictInt = Field(default=0, ge=0, le=32)


class CorrelationResult(BaseModel):
    """Return deterministic temporal co-occurrence and evidence references, never causal scores."""
    method_version: str = "co_occurrence_v1"
    from_at: datetime
    to_at: datetime
    regions: list[str] = Field(max_length=32)
    groups: list[CorrelationBucketRead] = Field(max_length=500)
    coverage: dict[str, CorrelationCoverageRead]
    included_domains: list[str] = Field(max_length=4)
    missing_domains: list[str] = Field(max_length=4)
    uncertainty_reasons: list[str] = Field(max_length=8)
    interpretation: str = "temporal_co_occurrence_only"


class CiiUnavailableRead(BaseModel):
    """Return the unchanged requested CII scope while method and licensed evidence remain unverified."""
    method_version: str = "v8"
    requested_countries: list[str] = Field(max_length=31)
    score: None = None
    band: None = None
    movement_24h: None = None
    as_of: None = None
    availability: str = "method_data_license_unverified"
    reason: str = "Authoritative v8 method, source licensing, and the specified country set are not verified."


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
    """Carry the admitted workspace, query, sort, snapshot and keyset position."""

    domain: Literal["news_stories"]
    workspace_id: UUID
    actor_user_id: int
    membership_revision: int
    configuration_revision: int
    filter_hash: str
    sort: Literal["observed_at_desc_story_id_desc"]
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
