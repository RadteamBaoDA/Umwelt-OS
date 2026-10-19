"""Unit tests for model-facing generative UI tool specs and argument parsing."""

import json

import pytest
from jsonschema import Draft202012Validator

from modules.chat.generative_ui.prompt import build_generative_ui_system_prompt
from modules.chat.generative_ui.tool_specs import (
    TERMINAL_UI_TOOLS,
    build_chat_tool_specs,
    parse_ui_tool_args,
)


def test_specs_names_and_valid_schemas() -> None:
    """All tools are present and every parameter schema is a valid JSON schema."""
    specs = build_chat_tool_specs(include_read_skill=True)
    names = {s["function"]["name"] for s in specs}
    assert names == TERMINAL_UI_TOOLS | {"read_skill"}
    for spec in specs:
        assert spec["type"] == "function"
        Draft202012Validator.check_schema(spec["function"]["parameters"])
        json.dumps(spec)
    assert {s["function"]["name"] for s in build_chat_tool_specs(include_read_skill=False)} == set(TERMINAL_UI_TOOLS)


def test_sandbox_schema_allows_list_expressions_and_no_uuid_fields() -> None:
    """jsExpressions accepts str or list; table/chart cite by integer number only."""
    by_name = {s["function"]["name"]: s["function"]["parameters"] for s in build_chat_tool_specs(include_read_skill=True)}
    kinds = {opt.get("type") for opt in by_name["render_sandboxed_ui"]["properties"]["jsExpressions"]["anyOf"]}
    assert kinds == {"string", "array"}
    for tool in ("render_table", "render_chart"):
        assert by_name[tool]["properties"]["citations"]["items"] == {"type": "integer"}
    assert "chunk_id" not in json.dumps(by_name)


def test_parse_sandbox_normalizes_expression_list() -> None:
    """A list of expressions is joined into the canonical string field."""
    args = parse_ui_tool_args("render_sandboxed_ui", json.dumps({"html": "<p>x</p>", "jsExpressions": ["a()", "b()"]}))
    assert args.model_dump()["jsExpressions"] == "a()\nb()"


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("render_sandboxed_ui", "{not json"),
        ("render_sandboxed_ui", json.dumps({"html": "<style>x</style>"})),
        ("render_table", json.dumps({"title": "t", "columns": [], "rows": []})),
        ("render_table", json.dumps({"title": "t", "columns": [{"key": "a", "label": "A"}], "citations": ["uuid"]})),
        ("read_skill", json.dumps({"name": "x"})),
        ("nope", "{}"),
    ],
)
def test_parse_errors_raise_value_error(name: str, raw: str) -> None:
    """Malformed JSON, schema violations and non-terminal/unknown tools raise ValueError."""
    with pytest.raises(ValueError):
        parse_ui_tool_args(name, raw)


def test_prompt_has_locale_libraries_and_no_retired_tools() -> None:
    """Prompt carries locale, vendored libs and privacy fence, and no references to removed tools."""
    text = build_generative_ui_system_prompt(locale="vi-VN", local_only=True, skills=["svg-diagram-skill"])
    assert "vi-VN" in text and "PRIVACY FENCE" in text and "svg-diagram-skill" in text
    for lib in ("three", "d3", "chart.js", "gsap", "leaflet"):
        assert lib in text
    for retired in ("Jev", "query_data", "A2UI", "plan_visualization", "esm.sh"):
        assert retired not in text
