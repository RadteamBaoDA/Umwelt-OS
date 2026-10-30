"""OpenAI-format tool specs and argument parsing for model-driven Generative UI.

Presentation tools are terminal: the model requests them together in one response (up to the turn cap). Evidence is referenced by
citation number only; the server resolves numbers to validated citations, so no model-supplied
identifiers (UUIDs) ever reach persisted payloads.
"""

import copy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from modules.chat.generative_ui.prompt import SandboxedUiArgs
from modules.chat.generative_ui.tools.form import FormPayload, TimeSlot
from modules.chat.generative_ui.tools.query import ChartSeriesConfig, TableColumn
from modules.chat.templates import (
    ApplyTemplateArgs,
    run_template_tool,
)
from modules.chat.todos import TodoReplace, run_todo_tool
from modules.chat.ui_theme import SetThemeArgs

__all__ = ["run_template_tool", "run_todo_tool"]  # re-exported so the worker imports every tool runner from here

TOOL_SANDBOXED_UI = "render_sandboxed_ui"
TOOL_TABLE = "render_table"
TOOL_CHART = "render_chart"
TOOL_PLAN = "render_plan"
TOOL_TIME_PICKER = "render_time_picker"
TOOL_FORM = "render_form"
TOOL_READ_SKILL = "read_skill"
TOOL_MANAGE_TODOS = "manage_todos"  # non-terminal; conversation todos, offered only when the worker opts in
TOOL_GET_TODOS = "get_todos"
TODO_TOOLS: frozenset[str] = frozenset({TOOL_MANAGE_TODOS, TOOL_GET_TODOS})
# No model-callable save: a prompt-injected model must not persist widget HTML; owners save via POST /api/v1/chat/templates.
TOOL_LIST_TEMPLATES = "list_templates"
TOOL_APPLY_TEMPLATE = "apply_template"
TOOL_CLEAR_PENDING_TEMPLATE = "clear_pending_template"
TEMPLATE_TOOLS: frozenset[str] = frozenset({
    TOOL_LIST_TEMPLATES, TOOL_APPLY_TEMPLATE, TOOL_CLEAR_PENDING_TEMPLATE,
})
TOOL_SET_THEME = "set_theme"  # non-terminal; emits a transient `ui.theme` event, offered only when the worker opts in
LOCAL_DATA_TOOLS: frozenset[str] = TODO_TOOLS | TEMPLATE_TOOLS | {TOOL_SET_THEME}  # run under the worker's live-run fence
TOOL_REQUEST_USER_INPUT = "request_user_input"  # non-terminal HITL tool: parks the run until the user answers
TOOL_TRIP_IMAGES = "get_trip_stop_images"  # non-terminal data tool; offered only with allow_widget_remote_media

TERMINAL_UI_TOOLS: frozenset[str] = frozenset(
    {TOOL_SANDBOXED_UI, TOOL_TABLE, TOOL_CHART, TOOL_PLAN, TOOL_TIME_PICKER, TOOL_FORM}
)



SAMPLE_SOURCE = "Illustrative sample dataset"  # same label tools/query.py uses for is_sample data


def _citation_numbers() -> Any:
    """Field for citation numbers [n] shown to the model in the grounded context (resolved server-side)."""
    return Field(
        default_factory=list, max_length=100,
        description="Citation numbers [n] from the context that support this widget. Integers only, never IDs.",
    )


class TableToolArgs(BaseModel):
    """Model-facing table arguments; evidence is cited by number, never by identifier."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=255)
    columns: list[TableColumn] = Field(min_length=1, max_length=16)
    rows: list[list[str]] = Field(default_factory=list, max_length=200)
    citations: list[int] = _citation_numbers()
    source: str = Field(default="Grounded evidence", max_length=255)
    is_sample: bool = False

    @model_validator(mode="after")
    def _sample_source(self) -> "TableToolArgs":
        """Default `source` to the sample-data label when `is_sample` is set and no source was given."""
        if self.is_sample and "source" not in self.model_fields_set:
            self.source = SAMPLE_SOURCE
        return self


class ChartToolArgs(BaseModel):
    """Model-facing chart arguments; evidence is cited by number, never by identifier."""

    model_config = ConfigDict(extra="forbid")

    chart_type: Literal["bar", "pie", "line", "scatter", "distribution"] = "bar"
    title: str = Field(min_length=1, max_length=255)
    subtitle: str | None = Field(default=None, max_length=255)
    x_key: str = Field(default="label", min_length=1, max_length=64)
    series: list[ChartSeriesConfig] = Field(min_length=1, max_length=8)
    data: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    citations: list[int] = _citation_numbers()
    source: str = Field(default="Grounded evidence", max_length=255)
    unit: str | None = Field(default=None, max_length=32)
    is_sample: bool = False

    @model_validator(mode="after")
    def _sample_source(self) -> "ChartToolArgs":
        """Default `source` to the sample-data label when `is_sample` is set and no source was given."""
        if self.is_sample and "source" not in self.model_fields_set:
            self.source = SAMPLE_SOURCE
        return self


class PlanStepToolArgs(BaseModel):
    """One plan step as the model writes it (server assigns ids and provenance)."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=1000)
    emoji: str = Field(default="🔹", max_length=16)
    status: Literal["pending", "in_progress", "completed"] = "pending"


class PlanToolArgs(BaseModel):
    """Model-facing plan/checklist arguments."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=255)
    approach: str = Field(min_length=1, max_length=1000)
    technology: str = Field(default="Umwelt-OS Checklist", max_length=120)
    key_elements: list[str] = Field(default_factory=list, max_length=12)
    steps: list[PlanStepToolArgs] = Field(min_length=1, max_length=24)
    source: str = Field(default="Grounded plan", max_length=255)


class TimePickerToolArgs(BaseModel):
    """Model-facing meeting time picker arguments; a click on a slot becomes a sendPrompt."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=255)
    subtitle: str = Field(default="", max_length=255)
    duration_minutes: int = Field(default=30, ge=5, le=480)
    timezone: str = Field(default="UTC", max_length=64)
    slots: list[TimeSlot] = Field(min_length=1, max_length=24)


class ReadSkillArgs(BaseModel):
    """Arguments of the non-terminal `read_skill` tool."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64, description="Skill name from the skill index.")


# Reserved answer meaning "none of these"; never a valid option/slot id.
INPUT_DECLINE = "__decline__"


class InputOption(BaseModel):
    """One selectable answer of a `choice` input request."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=120)


class RequestUserInputArgs(BaseModel):
    """Arguments of the non-terminal `request_user_input` tool (pause the turn and ask the user).

    `time_slots` needs `slots`, `choice` needs 2+ `options`, `text` needs neither. Bounds keep the persisted
    request and the SSE payload small; ids must be unique and may not equal `INPUT_DECLINE`.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["time_slots", "choice", "text"]
    prompt: str = Field(min_length=1, max_length=500, description="Question shown to the user.")
    options: list[InputOption] = Field(default_factory=list, max_length=12)
    slots: list[TimeSlot] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def _shape_matches_kind(self) -> "RequestUserInputArgs":
        """Enforce the per-kind payload shape and unique, non-reserved ids."""
        if self.kind == "time_slots" and not self.slots:
            raise ValueError("time_slots requires slots")
        if self.kind == "choice" and len(self.options) < 2:
            raise ValueError("choice requires at least two options")
        if self.kind != "time_slots" and self.slots:
            raise ValueError("slots are only valid for time_slots")
        if self.kind != "choice" and self.options:
            raise ValueError("options are only valid for choice")
        ids = [o.id for o in self.options] + [s.id for s in self.slots]
        if len(set(ids)) != len(ids) or INPUT_DECLINE in ids:
            raise ValueError("ids must be unique and not reserved")
        return self


class TripStopImagesToolArgs(BaseModel):
    """Arguments of the non-terminal `get_trip_stop_images` tool."""

    model_config = ConfigDict(extra="forbid")

    titles: list[str] = Field(
        min_length=1, max_length=8,
        description="English Wikipedia article titles of the trip stops (landmarks, cities), 1-8.",
    )


_UI_MODELS: dict[str, tuple[type[BaseModel], str]] = {
    TOOL_SANDBOXED_UI: (
        SandboxedUiArgs,
        (
            "Render a custom sandboxed HTML/CSS/JS view (diagram, 3D, map, animation, calculator). "
            "Emit parameters in the documented order. Terminal: the turn ends after the widget calls."
        ),
    ),
    TOOL_TABLE: (TableToolArgs, "Render a table of exact values. Terminal: the turn ends after the widget calls."),
    TOOL_CHART: (ChartToolArgs, "Render a bar/pie/line/scatter/distribution chart. Terminal."),
    TOOL_PLAN: (PlanToolArgs, "Render a step-by-step plan or checklist. Terminal."),
    TOOL_TIME_PICKER: (TimePickerToolArgs, "Render selectable time slots. Terminal."),
    TOOL_FORM: (
        FormPayload,
        "Render an input form to collect details needed to proceed (never credentials). Terminal.",
    ),
}


def _sandbox_schema() -> dict[str, Any]:
    """Build the sandbox JSON schema, widening `jsExpressions` to `string | string[]`."""
    schema = SandboxedUiArgs.model_json_schema()
    prop = schema["properties"]["jsExpressions"]
    schema["properties"]["jsExpressions"] = {
        **{k: v for k, v in prop.items() if k not in {"type", "maxLength"}},
        "anyOf": [{"type": "string", "maxLength": 64_000}, {"type": "array", "items": {"type": "string"}}],
    }
    return schema


def build_chat_tool_specs(
    *, include_read_skill: bool, include_trip_images: bool = False, include_todos: bool = False,
    include_user_input: bool = False, include_templates: bool = False, include_theme: bool = False,
    include_db_query: bool = False,
) -> list[dict[str, Any]]:
    """Build the OpenAI-format `tools` list for a generative-UI chat turn.

    Args:
        include_read_skill: Whether to expose the non-terminal `read_skill` tool.
        include_trip_images: Whether to expose `get_trip_stop_images` (only when remote widget media is allowed).
        include_todos: Whether to expose the non-terminal `manage_todos` and `get_todos` tools.
        include_user_input: Whether to expose the non-terminal `request_user_input` (pause for a user answer).
        include_templates: Whether to expose the three non-terminal template tools (list, apply, clear pending).
        include_theme: Whether to expose the non-terminal `set_theme` tool.
        include_db_query: Whether to expose the non-terminal `query_collected_data` database query tool.

    Returns:
        Fresh (caller-mutable) list of function specs whose parameters come from the pydantic models.
    """
    specs: list[dict[str, Any]] = []
    for name, (model, description) in _UI_MODELS.items():
        schema = _sandbox_schema() if model is SandboxedUiArgs else model.model_json_schema()
        specs.append({"type": "function", "function": {"name": name, "description": description, "parameters": schema}})
    if include_read_skill:
        specs.append({"type": "function", "function": {
            "name": TOOL_READ_SKILL,
            "description": "Load a bundled guidance document by name before building a widget. Not terminal.",
            "parameters": ReadSkillArgs.model_json_schema(),
        }})
    if include_user_input:
        specs.append({"type": "function", "function": {
            "name": TOOL_REQUEST_USER_INPUT,
            "description": (
                "Pause and ask the user to pick a time slot, choose an option, or type a short answer; the "
                "result is their answer (or expired/cancelled). Use for a decision only the user can make. Not terminal."
            ),
            "parameters": RequestUserInputArgs.model_json_schema(),
        }})
    if include_trip_images:
        specs.append({"type": "function", "function": {
            "name": TOOL_TRIP_IMAGES,
            "description": (
                "Fetch licensed Wikipedia/Commons photos with credit for trip stops; returns same-origin image_url, "
                "credit, license and article_url per stop, or status 'unavailable'. Not terminal."
            ),
            "parameters": TripStopImagesToolArgs.model_json_schema(),
        }})
    if include_todos:
        specs.append({"type": "function", "function": {
            "name": TOOL_MANAGE_TODOS,
            "description": (
                "Replace the conversation's whole todo list. Send the full list every time; omit ids for new items "
                "(existing ids are kept). Not terminal."
            ),
            "parameters": TodoReplace.model_json_schema(),
        }})
        specs.append({"type": "function", "function": {
            "name": TOOL_GET_TODOS,
            "description": "Read the conversation's current todo list. Not terminal.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        }})
    if include_templates:
        no_args = {"type": "object", "properties": {}, "additionalProperties": False}
        for tool, description, params in (
            (TOOL_LIST_TEMPLATES, "List built-in and saved templates (no html) and the pending one. Not terminal.", no_args),
            (TOOL_APPLY_TEMPLATE, (
                "Get a template's html to adapt and render, by template_id or by name (most recent match). Omit both "
                "to use the template the user selected (pending); it is cleared afterwards. Not terminal."
            ), ApplyTemplateArgs.model_json_schema()),
            (TOOL_CLEAR_PENDING_TEMPLATE, "Discard the user's pending template selection. Not terminal.", no_args),
        ):
            specs.append({"type": "function", "function": {"name": tool, "description": description, "parameters": params}})
    if include_theme:
        specs.append({"type": "function", "function": {
            "name": TOOL_SET_THEME,
            "description": (
                "Switch the app theme (light, dark or system) for the user's current browser session; they can keep "
                "or revert it. Use only when the user asks. Not terminal."
            ),
            "parameters": SetThemeArgs.model_json_schema(),
        }})
    if include_db_query:
        from modules.chat.db_query import build_db_query_tool_spec

        specs.append(build_db_query_tool_spec())
    return copy.deepcopy(specs)


def parse_ui_tool_args(name: str, raw_json: str) -> BaseModel:
    """Validate the final JSON arguments of a terminal UI tool call.

    Args:
        name: Tool name; must be in `TERMINAL_UI_TOOLS`.
        raw_json: Complete arguments string accumulated from the stream.

    Returns:
        The validated pydantic model for that tool.

    Raises:
        ValueError: Unknown/non-terminal tool, malformed JSON, or schema violation
            (pydantic's ValidationError is a ValueError subclass).
    """
    if name not in TERMINAL_UI_TOOLS:
        raise ValueError(f"Unknown UI tool: {name!r}")
    return _UI_MODELS[name][0].model_validate_json(raw_json)
