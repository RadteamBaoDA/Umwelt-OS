"""Validated detached DTOs for event commands and timeline reads."""

import json
import math
from datetime import UTC, date, datetime
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from modules.knowledge.entities.schemas import validate_metadata


def _aware_utc(value: datetime | None) -> datetime | None:
    """Reject naive timestamps and normalize valid instants to UTC."""
    if value is not None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must include an explicit UTC offset")
        return value.astimezone(UTC)
    return None


def _clean(value: str | None) -> str | None:
    """Collapse user-provided whitespace while preserving explicit nulls."""
    return " ".join(value.split()) if value is not None else None


class TimelineExportParticipant(BaseModel):
    """Serialize a stable participant identity and role without arbitrary metadata."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    entity_id: UUID
    role: str = Field(min_length=1, max_length=64)
    origin: Literal["manual", "derived"]


class TimelineExportEvidence(BaseModel):
    """Bind one exported event support to an exact retained source generation and chunk."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: UUID
    source_generation: int = Field(ge=1)
    document_id: UUID
    document_version_id: UUID
    chunk_id: UUID


class TimelineExportRead(BaseModel):
    """Expose portable canonical event facts and only eligible stable references."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    record_kind: Literal["event"] = "event"
    id: UUID
    type: str = Field(min_length=1, max_length=64)
    subtype: str | None = None
    title: str = Field(min_length=1, max_length=300)
    summary: str | None = Field(default=None, max_length=20_000)
    importance_score: float | None = Field(default=None, ge=0, le=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    origin: Literal["manual", "derived"]
    date_precision: Literal["timed", "date", "unknown"]
    started_at: datetime | None = None
    ended_at: datetime | None = None
    occurred_date: date | None = None
    end_date: date | None = None
    occurrence_timezone: str | None = None
    observed_at: datetime
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    revision: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    participants: list[TimelineExportParticipant] = Field(max_length=100)
    evidence: list[TimelineExportEvidence] = Field(max_length=100)


class TimelineExportFence(BaseModel):
    """Bind the current event revision, children and source generations for finalization."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    created_at: datetime
    updated_at: datetime
    revision: int = Field(ge=1)
    record_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    participant_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_fences: list[tuple[UUID, int]] = Field(max_length=100)


class TimelineExportPage(BaseModel):
    """Return an immutable bounded event page and its snapshot fences."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    owner_id: int = Field(ge=1)
    record_kind: Literal["events"]
    snapshot_at: datetime
    snapshot_count: int = Field(ge=0)
    items: list[TimelineExportRead] = Field(max_length=100)
    fences: list[TimelineExportFence] = Field(max_length=100)
    payload_bytes: int = Field(ge=0, le=16_777_216)
    max_payload_bytes: int = Field(default=16_777_216, ge=1, le=16_777_216)
    next_cursor: str | None = None


class TimelineExportFenceValidation(BaseModel):
    """Report whether the event snapshot and captured parent/child fences remain current."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    valid: bool
    reason: Literal["valid", "owner_unavailable", "snapshot_count_changed", "record_changed"]
    observed_snapshot_count: int = Field(ge=0)


class ParticipantInput(BaseModel):
    """Validate one canonical participant and its role metadata."""
    model_config = ConfigDict(extra="forbid")
    entity_id: UUID
    role: str = Field(min_length=1, max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("role")
    @classmethod
    def clean_role(cls, value: str) -> str:
        """Normalize role spacing and reject a blank value."""
        result = _clean(value)
        if not result:
            raise ValueError("role cannot be blank")
        return result

    @field_validator("metadata")
    @classmethod
    def bounded_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Apply the shared JSON byte and finite-number bound."""
        return validate_metadata(value)


class EventCreate(BaseModel):
    """Validate a manual event without permitting derived or server-owned fields."""
    model_config = ConfigDict(extra="forbid")
    type: str = Field(min_length=1, max_length=64)
    subtype: str | None = Field(default=None, max_length=64)
    title: str = Field(min_length=1, max_length=300)
    summary: str | None = Field(default=None, max_length=20_000)
    importance_score: float | None = None
    confidence: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    date_precision: Literal["timed", "date", "unknown"] = "unknown"
    started_at: datetime | None = None
    ended_at: datetime | None = None
    occurred_date: date | None = None
    end_date: date | None = None
    occurrence_timezone: str | None = Field(default=None, max_length=64)
    observed_at: datetime | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    participants: list[ParticipantInput] = Field(default_factory=list, max_length=100)
    evidence: list[tuple[UUID, UUID]] = Field(default_factory=list, max_length=100)

    @field_validator("type", "subtype", "title", "summary")
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        """Normalize whitespace and reject required strings that become blank."""
        result = _clean(value)
        if result == "" and value is not None:
            raise ValueError("text cannot be blank")
        return result

    @field_validator("importance_score", "confidence", mode="before")
    @classmethod
    def finite_unit_score(cls, value: object) -> float | None:
        """Reject booleans, NaN, infinity, and scores outside the closed unit interval."""
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("score must be a JSON number")
        score = float(value)
        if not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("score must be finite and between 0 and 1")
        return score

    @field_validator("metadata")
    @classmethod
    def bounded_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Apply the shared JSON byte and finite-number bound."""
        return validate_metadata(value)

    @field_validator("started_at", "ended_at", "observed_at", "valid_from", "valid_to")
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        """Require explicit-offset instants and store them in UTC."""
        return _aware_utc(value)

    @field_validator("occurrence_timezone")
    @classmethod
    def valid_timezone(cls, value: str | None) -> str | None:
        """Accept only installed IANA timezone names."""
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError("occurrence_timezone must be a valid IANA zone") from exc
        return value

    @model_validator(mode="after")
    def validate_time_shape(self) -> "EventCreate":
        """Enforce distinct timed, date-only, and unknown occurrence representations."""
        if self.date_precision == "timed":
            if self.started_at is None or self.occurred_date is not None or self.end_date is not None:
                raise ValueError("timed events require started_at and forbid calendar occurrence fields")
            if self.ended_at is not None and self.ended_at < self.started_at:
                raise ValueError("ended_at must be at or after started_at")
        elif self.date_precision == "date":
            if self.occurred_date is None or self.started_at is not None or self.ended_at is not None:
                raise ValueError("date events require occurred_date and forbid timestamps")
            if self.end_date is not None and self.end_date < self.occurred_date:
                raise ValueError("end_date must be at or after occurred_date")
        elif any((self.started_at is not None, self.ended_at is not None, self.occurred_date is not None, self.end_date is not None)):
            raise ValueError("unknown events cannot contain occurrence fields")
        if self.valid_to is not None and (self.valid_from is None or self.valid_to <= self.valid_from):
            raise ValueError("validity must be a nonempty half-open range")
        pairs = [tuple(item) for item in self.evidence]
        if len(set(pairs)) != len(pairs):
            raise ValueError("evidence pairs must be unique")
        participant_keys = [(item.entity_id, item.role) for item in self.participants]
        if len(set(participant_keys)) != len(participant_keys):
            raise ValueError("participant entity and role pairs must be unique")
        return self


class EventPatch(BaseModel):
    """Validate a partial manual correction fenced by expected revision and reason."""
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=300)
    type: str | None = Field(default=None, min_length=1, max_length=64)
    subtype: str | None = Field(default=None, max_length=64)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    summary: str | None = Field(default=None, max_length=20_000)
    importance_score: float | None = None
    confidence: float | None = None
    metadata: dict[str, Any] | None = None
    date_precision: Literal["timed", "date", "unknown"] | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    occurred_date: date | None = None
    end_date: date | None = None
    occurrence_timezone: str | None = Field(default=None, max_length=64)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    participants: list[ParticipantInput] | None = Field(default=None, max_length=100)
    evidence: list[tuple[UUID, UUID]] | None = Field(default=None, max_length=100)

    @field_validator("type", "subtype", "title", "summary")
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        """Normalize supplied event text and reject required strings that become blank."""
        result = _clean(value)
        if result == "" and value is not None:
            raise ValueError("text cannot be blank")
        return result

    @field_validator("type", "title", mode="before")
    @classmethod
    def required_patch_text(cls, value: object) -> object:
        """Reject explicit null for event fields whose persisted columns are required."""
        if value is None:
            raise ValueError("type and title cannot be cleared")
        return value

    @field_validator("date_precision", mode="before")
    @classmethod
    def required_patch_precision(cls, value: object) -> object:
        """Require an explicit valid precision value when the field is supplied."""
        if value is None:
            raise ValueError("date_precision cannot be cleared")
        return value

    @field_validator("importance_score", "confidence", mode="before")
    @classmethod
    def finite_unit_score(cls, value: object) -> float | None:
        """Reject booleans, NaN, infinity, and scores outside the closed unit interval."""
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("score must be a JSON number")
        score = float(value)
        if not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("score must be finite and between 0 and 1")
        return score

    @field_validator("started_at", "ended_at", "valid_from", "valid_to")
    @classmethod
    def explicit_offset(cls, value: datetime | None) -> datetime | None:
        """Require explicit UTC offsets on supplied event instants."""
        return _aware_utc(value)

    @field_validator("metadata")
    @classmethod
    def bounded_metadata(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        """Apply the shared JSON byte/finiteness validation to replacement metadata."""
        return validate_metadata(value) if value is not None else None

    @field_validator("occurrence_timezone")
    @classmethod
    def valid_timezone(cls, value: str | None) -> str | None:
        """Accept only installed IANA timezone names in a supplied correction."""
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError("occurrence_timezone must be a valid IANA zone") from exc
        return value

    @field_validator("reason")
    @classmethod
    def clean_reason(cls, value: str) -> str:
        """Normalize the required owner audit reason."""
        result = _clean(value)
        if not result:
            raise ValueError("reason cannot be blank")
        return result

    @model_validator(mode="after")
    def unique_replacement_participants(self) -> "EventPatch":
        """Reject duplicate entity/role replacement keys while preserving null and empty clears."""
        if self.participants is not None:
            keys = [(item.entity_id, item.role) for item in self.participants]
            if len(keys) != len(set(keys)):
                raise ValueError("participant entity and role pairs must be unique")
        return self


class EventRead(BaseModel):
    """Serialize canonical event state with participant and evidence provenance."""
    id: UUID
    source_id: UUID | None
    type: str
    subtype: str | None
    title: str
    summary: str | None
    importance_score: float | None
    confidence: float | None
    metadata: dict[str, Any]
    origin: Literal["manual", "derived"]
    date_precision: Literal["timed", "date", "unknown"]
    started_at: datetime | None
    ended_at: datetime | None
    occurred_date: date | None
    end_date: date | None
    occurrence_timezone: str | None
    observed_at: datetime
    valid_from: datetime | None
    valid_to: datetime | None
    revision: int
    created_at: datetime
    updated_at: datetime
    participants: list[ParticipantInput] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class EventPage(BaseModel):
    """Return bounded event results and a continuation cursor."""
    items: list[EventRead]
    next_cursor: str | None


class CorrelationSignalRead(BaseModel):
    """Expose one timed, region-scoped canonical event with exact live evidence IDs only."""
    signal_id: UUID
    event_id: UUID
    event_type: Literal["military", "economic", "disaster", "escalation"]
    region: str
    observed_at: datetime
    source_id: UUID | None
    event_evidence_ids: list[UUID] = Field(max_length=100)
    chunk_ids: list[UUID] = Field(max_length=100)
    document_ids: list[UUID] = Field(max_length=100)
    document_version_ids: list[UUID] = Field(max_length=100)
    omitted_event_evidence_ids: int = Field(default=0, ge=0)
    omitted_document_ids: int = Field(default=0, ge=0)
    omitted_document_version_ids: int = Field(default=0, ge=0)


class CorrelationSignalPage(BaseModel):
    """Return a fixed-size bounded domain slice and whether its query was truncated."""
    items: list[CorrelationSignalRead] = Field(max_length=100)
    truncated: bool = False


class TimelineQuery(BaseModel):
    """Normalize timeline filters for cursor fingerprinting and calendar boundaries."""
    date_from: date | None = None
    date_to: date | None = None
    timezone: str = "Asia/Ho_Chi_Minh"
    source_id: UUID | None = None
    entity_id: UUID | None = None
    type: str | None = Field(default=None, min_length=1, max_length=64)
    precision: Literal["all", "timed", "date", "unknown"] = "all"

    @field_validator("type")
    @classmethod
    def normalize_type_filter(cls, value: str | None) -> str | None:
        """Trim the optional literal type substring and reject whitespace-only filters."""
        if value is None:
            return None
        result = value.strip()
        if not result:
            raise ValueError("type filter cannot be blank")
        return result

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        """Require a zone available to stdlib zoneinfo."""
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be a valid IANA zone") from exc
        return value

    @model_validator(mode="after")
    def valid_range(self) -> "TimelineQuery":
        """Require both date boundaries or neither and a nonempty half-open range."""
        if (self.date_from is None) != (self.date_to is None):
            raise ValueError("date_from and date_to must be supplied together")
        if self.date_from is not None and self.date_to is not None and self.date_to <= self.date_from:
            raise ValueError("date_to must be after date_from")
        if self.precision == "unknown" and self.date_from is not None:
            raise ValueError("unknown precision cannot be combined with date filters")
        return self


class TimelinePage(EventPage):
    """Return timeline events in explicitly ordered time-precision partitions."""
    partition_order: tuple[str, ...] = ("timed", "date", "unknown")
