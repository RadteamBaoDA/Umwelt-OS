"""Tests for the generic `render_form` tool: limits, payload envelope and the FE contract fixture."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from modules.chat.generative_ui.streaming import finalize_generative_ui, streaming_snapshot
from modules.chat.generative_ui.tool_specs import (
    TERMINAL_UI_TOOLS,
    build_chat_tool_specs,
    parse_ui_tool_args,
)
from modules.chat.schemas import parse_generative_ui

FIXTURE = Path(__file__).parents[4] / "fixtures" / "genui" / "form_payload.json"
ARGS = {
    "title": "Trip details",
    "description": "Tell me a few things.",
    "fields": [
        {"id": "city", "label": "City", "type": "text", "required": True, "maxlength": 40},
        {"id": "days", "label": "Days", "type": "number", "min": 1, "max": 14},
        {"id": "pace", "label": "Pace", "type": "select", "options": ["Slow", "Fast"]},
        {"id": "notes", "label": "Notes", "type": "textarea"},
        {"id": "start", "label": "Start", "type": "date"},
        {"id": "pets", "label": "Bringing pets", "type": "checkbox"},
    ],
    "submit_label": "Send",
}


def _with(**over: object) -> str:
    """Return ARGS JSON with top-level overrides."""
    return json.dumps({**ARGS, **over})


def _field(**over: object) -> str:
    """Return JSON for a one-field form whose field has the given overrides."""
    return json.dumps({"title": "t", "fields": [{"id": "a", "label": "A", **over}]})


def test_tool_registered_and_terminal() -> None:
    """render_form is exposed with a valid schema and is terminal."""
    assert "render_form" in TERMINAL_UI_TOOLS
    assert "render_form" in {s["function"]["name"] for s in build_chat_tool_specs(include_read_skill=False)}


@pytest.mark.parametrize(
    "raw",
    [
        _with(fields=[]),
        _with(fields=[{"id": f"f{i}", "label": "x"} for i in range(13)]),
        _with(fields=[{"id": "a", "label": "x"}, {"id": "a", "label": "y"}]),
        _field(id="1bad"),
        _field(id="has space"),
        _field(label=""),
        _field(type="select"),
        _field(type="select", options=["a", "a"]),
        _field(type="select", options=[str(i) for i in range(21)]),
        _field(type="text", options=["a"]),
        _field(type="number", min=5, max=1),
        _field(type="password"),
        _field(maxlength=0),
        _with(title="x" * 256),
    ],
)
def test_limits_rejected(raw: str) -> None:
    """Server-side field limits reject malformed model output."""
    with pytest.raises(ValidationError):
        parse_ui_tool_args("render_form", raw)


def test_envelope_and_contract_fixture() -> None:
    """Finalized payload uses kind=form, passes persistence parsing and matches the FE fixture."""
    payload = finalize_generative_ui("render_form", json.dumps(ARGS), {}, False)
    assert payload is not None
    assert payload["kind"] == "form" and payload["status"] == "completed"
    assert payload["form"]["fields"][2]["options"] == ["Slow", "Fast"]
    assert parse_generative_ui(payload) is not None
    assert json.loads(FIXTURE.read_text(encoding="utf-8")) == payload


def test_invalid_args_drop_widget_and_streaming_kind() -> None:
    """Invalid args yield no widget; partial args stream under kind=form."""
    assert finalize_generative_ui("render_form", _field(id="9"), {}, False) is None
    snap = streaming_snapshot("render_form", '{"title": "T", "fields": [{"id": "a", "label": "A"')
    assert snap is not None and snap["kind"] == "form"
