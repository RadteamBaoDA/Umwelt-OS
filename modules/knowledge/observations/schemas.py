"""Strict owner-facing observation and query DTOs."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator


class WorldMeasurement(BaseModel):
    """Validate a provider-owned finite value before persistence."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: str = Field(min_length=1, max_length=64)
    metric: str = Field(min_length=1, max_length=80)
    value: float | None
    unit: str = Field(min_length=1, max_length=64)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    timezone: str | None = Field(default=None, max_length=64)
    symbol: str | None = Field(default=None, max_length=40)
    region: str | None = Field(default=None, max_length=80)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    published_at: datetime | None = None
    quality: str = Field(min_length=1, max_length=32)
    missing_reason: str | None = Field(default=None, max_length=64)
    provider_fields: dict[str, str | float | int | None] = Field(default_factory=dict, max_length=12)

    @field_validator("value")
    @classmethod
    def finite_value(cls, value: float | None) -> float | None:
        """Reject non-finite provider numbers before database writes."""
        import math
        if value is not None and not math.isfinite(value):
            raise ValueError("value must be finite")
        return value


class ObservationQuery(BaseModel):
    """Bound an observation series read to explicit source, metric and UTC time filters."""
    model_config = ConfigDict(extra="forbid")
    source_ids: list[UUID] = Field(min_length=1, max_length=32)
    metrics: list[str] = Field(default_factory=list, max_length=32)
    symbols: list[str] = Field(default_factory=list, max_length=32)
    regions: list[str] = Field(default_factory=list, max_length=32)
    from_at: datetime
    to_at: datetime
    limit: StrictInt = Field(default=100, ge=1, le=100)
    geospatial_only: bool = False

    @field_validator("from_at", "to_at")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        """Require explicit timezones and normalize the half-open query window to UTC."""
        from datetime import UTC
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("date bounds must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("metrics", "symbols", "regions")
    @classmethod
    def validate_filter_values(cls, values: list[str], info: object) -> list[str]:
        """Bound each series selector and reject empty, padded, or control-bearing values."""
        maximum = {"metrics": 80, "symbols": 40, "regions": 80}.get(getattr(info, "field_name", ""), 80)
        if any(
            not isinstance(value, str) or not value or len(value) > maximum
            or value != value.strip() or any(ord(char) < 32 for char in value)
            for value in values
        ):
            raise ValueError("observation selector is invalid")
        return values

    @model_validator(mode="after")
    def validate_window(self) -> "ObservationQuery":
        """Keep windows half-open, finite and within the supported 366-day range."""
        from datetime import timedelta
        if self.from_at >= self.to_at or self.to_at - self.from_at > timedelta(days=366):
            raise ValueError("invalid observation time window")
        for values in (self.source_ids, self.metrics, self.symbols, self.regions):
            if len(values) != len(set(values)):
                raise ValueError("observation filters must be unique")
        return self


class ObservationRead(BaseModel):
    """Expose a selected value and its original provider units and immutable evidence links."""
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    source_id: UUID
    provider: str
    provider_scope_discriminator: str
    source_generation: int
    external_id: str
    revision: int
    metric: str
    symbol: str | None
    region: str | None
    latitude: float | None
    longitude: float | None
    observed_at: datetime
    published_at: datetime | None
    collected_at: datetime
    value: float | None
    unit: str
    currency: str | None
    timezone: str | None
    quality: str
    missing_reason: str | None
    provider_delay_seconds: int | None
    document_id: UUID
    document_version_id: UUID


class GeospatialObservationRead(ObservationRead):
    """Expose a current authorized observation only when the provider recorded both coordinates."""
    latitude: float
    longitude: float
    document_version_number: int | None = Field(default=None, ge=1)


class GeospatialObservationPage(BaseModel):
    """Return a bounded current point page and aggregate unsupported-source coverage."""
    items: list[GeospatialObservationRead] = Field(max_length=100)
    next_cursor: str | None
    truncated: bool = False
    omitted_source_count: int = Field(ge=0, le=32)


class ObservationPage(BaseModel):
    """Return a finite keyset page and its optional filter-bound continuation cursor."""
    items: list[ObservationRead]
    next_cursor: str | None
    truncated: bool = False
