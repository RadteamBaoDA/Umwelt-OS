"""Model-proposed scheduled-task drafts (chat tool ``draft_scheduled_task``).

A draft is only shown to the owner as a card; the server never creates or updates an automation from chat.
The owner confirms by saving in the web editor, which uses the normal automations API.
"""

from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

TOOL_DRAFT_SCHEDULED_TASK = "draft_scheduled_task"

# Equal to dashboard ALL_BRIEF_SECTIONS (pinned by a test); not imported because dashboard imports chat.
DRAFT_SECTIONS = ("stories", "markets", "weather", "hazards", "agenda", "source_health")

DraftSection = Literal["stories", "markets", "weather", "hazards", "agenda", "source_health"]


class ScheduledTaskDraft(BaseModel):
    """Model-proposed Daily brief task shown to the owner as a card; never persisted as an automation by the server."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["daily_brief"]
    name: str = Field(min_length=1, max_length=200)
    time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$", description="Local HH:MM")
    days: Literal["daily", "weekdays"] = "daily"
    sections: list[DraftSection] = Field(
        default_factory=lambda: list(get_args(DraftSection)), min_length=1, max_length=6
    )
    extra_instruction: str | None = Field(default=None, max_length=500)
    model_role: Literal["brief", "chat", "background"] = "brief"

    @field_validator("sections")
    @classmethod
    def unique_sections(cls, value: list[DraftSection]) -> list[DraftSection]:
        """Reject a section listed twice (same rule as the Daily brief action)."""
        if len(set(value)) != len(value):
            raise ValueError("sections must not repeat")
        return value


def parse_task_draft(metadata: Any) -> ScheduledTaskDraft | None:
    """Lenient read of ``metadata_json["task_draft"]``; missing or malformed data yields ``None``, never an error."""
    if not isinstance(metadata, dict):
        return None
    try:
        return ScheduledTaskDraft.model_validate(metadata.get("task_draft"))
    except ValidationError:
        return None
