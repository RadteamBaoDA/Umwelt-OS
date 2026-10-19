"""Interactive meeting time picker and A2UI demonstration form schemas and builders."""

import json
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _ToolDescriptor:
    """Callable tool wrapper exposing `.name`, `.description`, `.func`, and `.invoke`."""

    def __init__(self, func: Any) -> None:
        """Bind a Python function with tool metadata."""
        self.func = func
        self.name = func.__name__
        self.description = (func.__doc__ or "").strip()
        self.__doc__ = func.__doc__
        self.__name__ = func.__name__

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Invoke the underlying function directly."""
        return self.func(*args, **kwargs)

    def invoke(self, input_data: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        """Invoke the underlying function using a dictionary of keyword arguments."""
        merged = {**(input_data or {}), **kwargs}
        return self.func(**merged)


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


def _generate_form() -> str:
    """Return a non-functional sample login-form schema for protocol demonstrations.

    This does not authenticate or save data. Never use it to collect real credentials.
    """
    components = [
        {"id": "root", "component": {"Card": {"child": "main-column"}}},
        {
            "id": "main-column",
            "component": {
                "Column": {
                    "children": {
                        "explicitList": [
                            "header",
                            "email-field",
                            "password-field",
                            "login-btn",
                            "divider",
                            "signup-text",
                        ]
                    },
                    "gap": "medium",
                }
            },
        },
        {
            "id": "header",
            "component": {
                "Column": {
                    "children": {"explicitList": ["title", "subtitle"]},
                    "alignment": "center",
                }
            },
        },
        {
            "id": "title",
            "component": {
                "Text": {"text": {"literalString": "Welcome back"}, "usageHint": "h2"}
            },
        },
        {
            "id": "subtitle",
            "component": {
                "Text": {
                    "text": {"literalString": "Sign in to your account"},
                    "usageHint": "caption",
                }
            },
        },
        {
            "id": "email-field",
            "component": {
                "TextField": {
                    "value": {"path": "/email"},
                    "placeholder": {"literalString": "Email address"},
                    "label": {"literalString": "Email"},
                    "action": "updateEmail",
                }
            },
        },
        {
            "id": "password-field",
            "component": {
                "TextField": {
                    "value": {"path": "/password"},
                    "placeholder": {"literalString": "Password"},
                    "label": {"literalString": "Password"},
                    "action": "updatePassword",
                }
            },
        },
        {
            "id": "login-btn-text",
            "component": {"Text": {"text": {"literalString": "Sign in"}}},
        },
        {
            "id": "login-btn",
            "component": {"Button": {"child": "login-btn-text", "action": "login"}},
        },
        {"id": "divider", "component": {"Divider": {}}},
        {
            "id": "signup-text",
            "component": {
                "Row": {
                    "children": {"explicitList": ["no-account", "signup-link"]},
                    "distribution": "center",
                    "gap": "small",
                }
            },
        },
        {
            "id": "no-account",
            "component": {
                "Text": {
                    "text": {"literalString": "Don't have an account?"},
                    "usageHint": "caption",
                }
            },
        },
        {
            "id": "signup-link-text",
            "component": {"Text": {"text": {"literalString": "Sign up"}}},
        },
        {
            "id": "signup-link",
            "component": {"Button": {"child": "signup-link-text", "action": "signup"}},
        },
    ]
    return json.dumps(
        [
            {"surfaceUpdate": {"surfaceId": "login-form", "components": components}},
            {"beginRendering": {"surfaceId": "login-form", "root": "root"}},
        ]
    )


generate_form = _ToolDescriptor(_generate_form)


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
