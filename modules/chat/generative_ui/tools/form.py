"""Interactive meeting time picker and generic input-form schemas and builders."""

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_FORM_FIELDS = 12
MAX_SELECT_OPTIONS = 20
FIELD_ID_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{0,39}$"


class FormFieldSpec(BaseModel):
    """One input field of a generic form widget; limits are mirrored by the frontend zod schema."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=FIELD_ID_PATTERN, description="Unique field id: letter first, then letters/digits/_/-.")
    label: str = Field(min_length=1, max_length=120)
    type: Literal["text", "textarea", "number", "select", "checkbox", "date"] = "text"
    required: bool = False
    placeholder: str = Field(default="", max_length=120)
    options: list[str] = Field(default_factory=list, max_length=MAX_SELECT_OPTIONS, description="Select choices.")
    min: float | None = Field(default=None, description="Lower bound (number fields).")
    max: float | None = Field(default=None, description="Upper bound (number fields).")
    maxlength: int | None = Field(default=None, ge=1, le=4000, description="Character limit (text/textarea).")

    @model_validator(mode="after")
    def _check_type_rules(self) -> "FormFieldSpec":
        """Require 1..20 distinct, bounded options for `select`, forbid them elsewhere, order min/max."""
        if self.type == "select":
            if not self.options:
                raise ValueError("select fields need options")
            if len(set(self.options)) != len(self.options) or any(not o.strip() or len(o) > 80 for o in self.options):
                raise ValueError("select options must be unique, non-empty and at most 80 characters")
        elif self.options:
            raise ValueError("options are only allowed on select fields")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min must not exceed max")
        return self


class FormPayload(BaseModel):
    """Generic input form; also the model-facing `render_form` arguments. Submit becomes a follow-up prompt."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=500)
    fields: list[FormFieldSpec] = Field(min_length=1, max_length=MAX_FORM_FIELDS)
    submit_label: str = Field(default="", max_length=40, description="Submit button text; empty uses the default.")

    @model_validator(mode="after")
    def _unique_ids(self) -> "FormPayload":
        """Reject duplicate field ids (they key the submitted values)."""
        ids = [f.id for f in self.fields]
        if len(set(ids)) != len(ids):
            raise ValueError("field ids must be unique")
        return self


def build_form_payload(args: FormPayload) -> dict[str, Any]:
    """Serialize validated `render_form` arguments into the persisted `form` slot body.

    Args:
        args: Validated model arguments (limits already enforced by pydantic).

    Returns:
        JSON-serializable dict stored in the envelope's `form` slot (envelope kind `form`).
    """
    return args.model_dump(mode="json")


class TimeSlot(BaseModel):
    """Single selectable time slot in a meeting time picker widget."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=120)
    date: str = Field(min_length=1, max_length=32)
    start_time: str = Field(min_length=1, max_length=32)
    end_time: str = Field(min_length=1, max_length=32)
    timezone: str = Field(default="UTC", max_length=64)
    available: bool = True
    recommended: bool = False


class MeetingTimePickerPayload(BaseModel):
    """Structured payload for an interactive slot/meeting picker widget."""

    model_config = ConfigDict(extra="forbid")

    surface_id: str = "meeting-time-picker"
    title: str = Field(min_length=1, max_length=255)
    subtitle: str = Field(default="Select an available slot to draft a follow-up confirmation", max_length=255)
    duration_minutes: int = Field(default=30, ge=5, le=480)
    timezone: str = Field(default="UTC", max_length=64)
    slots: list[TimeSlot] = Field(min_length=1, max_length=24)
    selected_slot_id: str | None = None
    a2ui_surface: list[dict[str, Any]] = Field(default_factory=list)


def build_meeting_time_picker_payload(
    title: str = "Select a Meeting Time",
    *,
    slots: Sequence[dict[str, Any] | TimeSlot] | None = None,
    duration_minutes: int = 30,
    timezone: str = "UTC",
    subtitle: str = "Choose a time slot to send a scheduling confirmation in chat",
) -> MeetingTimePickerPayload:
    """Build a validated `MeetingTimePickerPayload` with default or caller-supplied time slots.

    Args:
        title: Heading displayed on the picker card.
        slots: Optional sequence of slot dicts or `TimeSlot` models.
        duration_minutes: Meeting duration in minutes.
        timezone: IANA timezone identifier.
        subtitle: Instructional subtitle.

    Returns:
        Validated `MeetingTimePickerPayload` instance.
    """
    resolved_slots: list[TimeSlot] = []
    if slots:
        for idx, raw_slot in enumerate(slots, 1):
            if isinstance(raw_slot, TimeSlot):
                resolved_slots.append(raw_slot)
            else:
                resolved_slots.append(
                    TimeSlot(
                        id=str(raw_slot.get("id") or f"slot-{idx}"),
                        label=str(raw_slot.get("label") or f"Option {idx}"),
                        date=str(raw_slot.get("date") or "2026-10-20"),
                        start_time=str(raw_slot.get("start_time") or "09:00"),
                        end_time=str(raw_slot.get("end_time") or "09:30"),
                        timezone=str(raw_slot.get("timezone") or timezone),
                        available=bool(raw_slot.get("available", True)),
                        recommended=bool(raw_slot.get("recommended", idx == 1)),
                    )
                )
    else:
        resolved_slots = [
            TimeSlot(
                id="slot-1",
                label="Morning Slot (Recommended)",
                date="2026-10-20",
                start_time="09:30",
                end_time="10:00",
                timezone=timezone,
                available=True,
                recommended=True,
            ),
            TimeSlot(
                id="slot-2",
                label="Midday Slot",
                date="2026-10-20",
                start_time="13:00",
                end_time="13:30",
                timezone=timezone,
                available=True,
                recommended=False,
            ),
            TimeSlot(
                id="slot-3",
                label="Afternoon Slot",
                date="2026-10-20",
                start_time="15:30",
                end_time="16:00",
                timezone=timezone,
                available=True,
                recommended=False,
            ),
        ]

    selected_id = next((s.id for s in resolved_slots if s.recommended and s.available), resolved_slots[0].id)
    a2ui_surface = [
        {
            "surfaceUpdate": {
                "surfaceId": "meeting-time-picker",
                "components": [
                    {"id": "root", "component": {"Card": {"child": "slot-column"}}},
                    {
                        "id": "slot-column",
                        "component": {
                            "Column": {
                                "children": {"explicitList": [s.id for s in resolved_slots]},
                                "gap": "small",
                            }
                        },
                    },
                ],
            }
        },
        {"beginRendering": {"surfaceId": "meeting-time-picker", "root": "root"}},
    ]
    return MeetingTimePickerPayload(
        title=title.strip()[:255] or "Select a Meeting Time",
        subtitle=subtitle[:255],
        duration_minutes=duration_minutes,
        timezone=timezone,
        slots=resolved_slots,
        selected_slot_id=selected_id,
        a2ui_surface=a2ui_surface,
    )
