"""DTOs for the selected-day context, revisioned daily briefs and the brief schedule."""

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

DayRelation = Literal["past", "today", "future"]
WidgetStatus = Literal["ok", "empty", "not_applicable", "unavailable"]


def validate_timezone(value: str) -> str:
    """Reject non-IANA zone names so every date boundary is computed from a real zone."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("timezone must be a valid IANA timezone") from exc
    return value


class BriefRead(BaseModel):
    """One saved brief revision; ``status='stale'`` means cited evidence has since disappeared."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    brief_date: date
    timezone: str
    revision: int
    status: Literal["current", "stale"]
    content: str
    citations: list[dict[str, Any]]
    model_alias: str
    generated_at: datetime


class DailyWidget(BaseModel):
    """Current-record projection for one day gadget; never a historical state snapshot."""

    id: str
    module: str
    title_key: str
    status: WidgetStatus
    items: list[dict[str, Any]] = Field(default_factory=list)
    updated_at: datetime | None = None
    source_status: str | None = None
    history_mode: Literal["current_records"] = "current_records"


class DailyContext(BaseModel):
    """Selected-day context: the saved brief (if any) plus widgets built from current records."""

    selected_date: date
    timezone: str
    relation: DayRelation
    generated_at: datetime
    brief: BriefRead | None
    brief_revisions: int
    widgets_updated_at: datetime
    history_mode: Literal["saved_brief_current_records"] = "saved_brief_current_records"
    unread_notifications: int
    widgets: list[DailyWidget]


class BriefGenerateRequest(BaseModel):
    """Manual generation request; ``force`` appends a revision even when inputs are unchanged."""

    model_config = ConfigDict(extra="forbid")

    brief_date: date
    timezone: str = "Asia/Ho_Chi_Minh"
    force: bool = True

    _tz = field_validator("timezone")(validate_timezone)


class BriefSchedule(BaseModel):
    """Owner-editable daily schedule consumed by the ARQ cron."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    enabled: bool = True
    hour: int = Field(default=7, ge=0, le=23)
    minute: int = Field(default=0, ge=0, le=59)
    timezone: str = "Asia/Ho_Chi_Minh"

    _tz = field_validator("timezone")(validate_timezone)
