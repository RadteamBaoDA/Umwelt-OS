"""Response models for the live-map event proxy and the live-map configuration endpoints.

They document the existing JSON exactly (the handlers still build plain dicts); ``extra="allow"`` keeps any provider- or
catalog-specific key that a model does not declare, so adding a model never drops a field from a response.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class LiveMapSourceLink(BaseModel):
    """One attributed original source of an event."""

    id: str | None
    url: str


class LiveMapEvent(BaseModel):
    """Bounded, display-only provider record with validated coordinates."""

    model_config = ConfigDict(extra="allow")

    id: str
    category: str
    title: str
    observed_at: str | None
    latitude: float | None
    longitude: float | None
    url: str | None
    source: str
    summary: str | None
    sources: list[LiveMapSourceLink]


class LiveMapEventsResponse(BaseModel):
    """Response of ``GET /api/v1/live-map/events``: bounded events plus fetch time and truncation flag."""

    items: list[LiveMapEvent]
    fetched_at: str
    truncated: bool


class LiveTermsCatalogEntry(BaseModel):
    """One provider's terms identity merged with this workspace's acknowledgement state."""

    model_config = ConfigDict(extra="allow")

    id: str
    label: str
    terms_url: str
    version: str
    eligibility: Literal["open", "personal", "noncommercial", "review"]
    attribution: str
    accepted: bool
    acknowledged: bool
    review_state: str | None = None


class LiveConfigResponse(BaseModel):
    """Sanitized live-map settings; saved keys are reported only as configured flags."""

    revision: int
    openweather_configured: bool
    firms_configured: bool
    sec_contact_email: str | None = None
    is_operator: bool
    terms_catalog: list[LiveTermsCatalogEntry]
