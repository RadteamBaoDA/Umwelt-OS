"""DTOs for the selected-day context, revisioned daily briefs and the brief schedule."""

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator

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


class DailyBriefExport(BaseModel):
    """Expose one immutable saved brief revision only while every exact citation remains eligible."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    brief_date: date
    timezone: str
    revision: int = Field(ge=1)
    status: Literal["current"]
    content: str
    citations: list[dict[str, Any]] = Field(max_length=40)
    model_alias: str = Field(min_length=1, max_length=32)
    generated_at: datetime


class BriefExportFence(BaseModel):
    """Bind a saved brief and its current citation-eligibility result for final revalidation."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    generated_at: datetime
    revision: int = Field(ge=1)
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    eligible: bool


class BriefExportPage(BaseModel):
    """Return bounded retained revisions with a cutoff, omission accounting, and exact fences."""

    model_config = ConfigDict(extra="forbid")

    owner_id: int = Field(ge=1)
    record_kind: Literal["daily_briefs"]
    snapshot_at: datetime
    snapshot_count: int = Field(ge=0)
    omitted_count: int = Field(ge=0)
    items: list[DailyBriefExport] = Field(max_length=100)
    fences: list[BriefExportFence] = Field(max_length=100)
    payload_bytes: int = Field(ge=0, le=16_777_216)
    max_payload_bytes: int = Field(default=16_777_216, ge=1, le=16_777_216)
    next_cursor: str | None = Field(default=None, max_length=512)
    available: bool = True
    omission_reason: Literal["unsupported_or_deleted_citation"] | None = None


class BriefExportValidation(BaseModel):
    """Report whether the cutoff brief inventory and every captured eligibility fence still match."""

    model_config = ConfigDict(extra="forbid")

    valid: bool
    reason: Literal["valid", "snapshot_count_changed", "record_changed"]
    observed_snapshot_count: int = Field(ge=0)


class BriefScheduleExport(BaseModel):
    """Project editable schedule fields without exposing automation slot identity or credentials."""

    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool
    hour: StrictInt = Field(ge=0, le=23)
    minute: StrictInt = Field(ge=0, le=59)
    timezone: str

    _tz = field_validator("timezone")(validate_timezone)


class BriefScheduleExportFence(BaseModel):
    """Bind schedule projection to persisted/default state and the owner's current row revision."""

    model_config = ConfigDict(extra="forbid")

    persisted: bool
    updated_at: datetime | None
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class BriefScheduleExportPage(BaseModel):
    """Return the single owner schedule projection and the shared bounded-cursor page contract."""

    model_config = ConfigDict(extra="forbid")

    owner_id: int = Field(ge=1)
    record_kind: Literal["brief_schedule"]
    snapshot_at: datetime
    snapshot_count: int = Field(ge=0, le=1)
    items: list[BriefScheduleExport] = Field(max_length=1)
    fences: list[BriefScheduleExportFence] = Field(max_length=1)
    payload_bytes: int = Field(ge=0, le=16_777_216)
    max_payload_bytes: int = Field(default=16_777_216, ge=1, le=16_777_216)
    next_cursor: str | None = Field(default=None, max_length=512)
    available: bool = True
    omission_reason: Literal["schedule_changed_after_snapshot"] | None = None


class BriefScheduleExportValidation(BaseModel):
    """Report whether the owner's saved/default brief schedule still matches its final fence."""

    model_config = ConfigDict(extra="forbid")

    valid: bool
    reason: Literal["valid", "record_changed"]
    observed_snapshot_count: int = Field(ge=0, le=1)
