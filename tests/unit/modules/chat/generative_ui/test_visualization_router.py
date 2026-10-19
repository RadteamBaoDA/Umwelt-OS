"""Unit tests for visualization_router Jev contracts and deterministic local routing."""

from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from modules.chat.generative_ui import visualization_router as router
from modules.chat.generative_ui.visualization_router import (
    VISUALIZATIONS,
    decision_from_response,
    parse_response,
    request_options,
    route,
    route_visualization,
    routed_request,
    routing_input,
)


@dataclass
class _FakeModelRequest:
    """Minimal ModelRequest test double supporting `.override(...)`."""

    model: Any = None
    messages: list[Any] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    system_message: SystemMessage | None = None
    state: dict[str, Any] = field(default_factory=dict)

    def override(
        self,
        *,
        tools: list[dict[str, Any]] | None = None,
        system_message: SystemMessage | None = None,
    ) -> "_FakeModelRequest":
        """Return a new _FakeModelRequest with overridden tools and system_message."""
        return _FakeModelRequest(
            model=self.model,
            messages=self.messages,
            tools=tools if tools is not None else self.tools,
            system_message=system_message if system_message is not None else self.system_message,
            state=self.state,
        )


def _response(choice: str) -> dict[str, Any]:
    """Build a valid synthetic Jev choice response dictionary."""
    return {
        "answers": {
            "visualization": {
                "type": "choice",
                "choice": choice,
                "confidence": 0.9,
                "probabilities": {choice: 0.9},
            }
        }
    }


@pytest.mark.parametrize(
    ("choice", "renderer"),
    [
        ("table", "a2ui"),
        ("line_chart", "open_generative_ui"),
        ("bar_chart", "open_generative_ui"),
        ("scatter_plot", "open_generative_ui"),
        ("distribution", "open_generative_ui"),
        ("part_to_whole", "open_generative_ui"),
        ("flowchart", "open_generative_ui"),
        ("static_diagram", "open_generative_ui"),
        ("interactive_diagram", "open_generative_ui"),
        ("map", "open_generative_ui"),
        ("animated_route", "open_generative_ui"),
        ("calculator", "open_generative_ui"),
        ("text", "text"),
    ],
)
def test_valid_decisions_map_to_exact_renderer(choice: str, renderer: str) -> None:
    """Verify every valid Jev visualization choice maps to its exact canonical renderer."""
    decision = decision_from_response(_response(choice))
    assert decision["renderer"] == renderer
    assert decision["visualization"] == choice
    assert decision["source"] == "jev"


@pytest.mark.parametrize(
    "raw",
    [
        {},
        _response("invented"),
        {"answers": {"visualization": {"choice": "table"}}},
        {
            "answers": {
                "visualization": {
                    "type": "choice",
                    "choice": "table",
                    "confidence": 0.9,
                    "probabilities": {"table": 1.5},
                }
            }
        },
    ],
)
def test_bad_decisions_fail_loudly(raw: dict[str, Any]) -> None:
    """Verify malformed or out-of-domain Jev responses raise an actionable ValueError."""
    with pytest.raises(ValueError, match="Jev"):
        decision_from_response(raw)


def test_continuation_reuses_decision_but_new_human_reroutes() -> None:
    """Verify routing_input skips already-routed turns and reroutes on a new human message."""
    state: dict[str, Any] = {
        "messages": [HumanMessage(content="table", id="one")],
        "visualization_turn": "one",
        "visualization_decision": decision_from_response(_response("table")),
    }
    assert routing_input(state) is None
    state["messages"].append(AIMessage(content="done"))
    assert routing_input(state) is None
    state["messages"].append(HumanMessage(content="now chart it", id="two"))
    routed = routing_input(state)
    assert routed is not None
    turn, context = routed
    assert turn == "two"
    assert context[-1]["content"] == "now chart it"


@pytest.mark.parametrize(
    ("choice", "allowed"),
    [
        ("table", "send_a2ui_json_to_client"),
        ("line_chart", "generateSandboxedUi"),
        ("map", "generateSandboxedUi"),
        ("animated_route", "generateSandboxedUi"),
        ("text", None),
    ],
)
def test_route_filters_renderers_preserving_data_tools(
    choice: str, allowed: str | None
) -> None:
    """Verify routed_request removes non-selected renderers while preserving data tools."""
    tools = [
        {"name": n}
        for n in [
            "render_a2ui",
            "send_a2ui_json_to_client",
            "generate_a2ui",
            "generateSandboxedUi",
            "barChart",
            "pieChart",
            "query_data",
        ]
    ]
    request = _FakeModelRequest(
        model=None,
        messages=[],
        tools=tools,
        system_message=SystemMessage(content="base"),
        state={"visualization_decision": decision_from_response(_response(choice))},
    )
    result = routed_request(request)
    names = {t["name"] for t in result.tools}
    assert "query_data" in names
    assert ("generateSandboxedUi" in names) == (allowed == "generateSandboxedUi")
    assert ("send_a2ui_json_to_client" in names) == (
        allowed == "send_a2ui_json_to_client"
    )
    assert ("render_a2ui" in names) == (choice == "table")
    assert "barChart" not in names and "pieChart" not in names
    if choice == "map":
        assert result.system_message is not None
        assert "basemap.nationalmap.gov" in str(result.system_message.content)
        assert "USGS attribution" in str(result.system_message.content)
        assert "coverage limits" in str(result.system_message.content)


def test_missing_key_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify request_options raises an actionable error when TYPESAFE_API_KEY is absent."""
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="TYPESAFE_API_KEY"):
        request_options([])


def test_provider_errors_do_not_expose_response_body() -> None:
    """Verify parse_response reports HTTP status without leaking sensitive response text."""
    with pytest.raises(ValueError, match="HTTP 401") as error:
        parse_response(httpx.Response(401, text="sensitive provider details"))
    assert "sensitive" not in str(error.value)


def test_timeout_is_actionable_without_secret_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify network timeouts raise an actionable message without leaking secret keys."""
    monkeypatch.setenv("TYPESAFE_API_KEY", "private-test-value")

    def _timeout(*args: Any, **kwargs: Any) -> httpx.Response:
        raise httpx.ReadTimeout("private-test-value")

    monkeypatch.setattr(httpx.Client, "post", _timeout)
    with pytest.raises(ValueError, match="timed out") as error:
        route([])
    assert "private-test-value" not in str(error.value)


def test_context_excludes_system_and_tool_content_and_is_bounded() -> None:
    """Verify routing_input strips system/tool messages and truncates long messages to 6000 chars."""
    state = {
        "messages": [
            SystemMessage(content="secret system"),
            ToolMessage(content="tool payload", tool_call_id="x"),
            HumanMessage(content="a" * 20000, id="bounded"),
        ]
    }
    routed = routing_input(state)
    assert routed is not None
    _, context = routed
    assert len(context) == 1
    assert len(context[0]["content"]) == 6000


def test_idless_continuation_has_stable_turn_key() -> None:
    """Verify messages without an explicit ID use a deterministic content-hash turn key."""
    state: dict[str, Any] = {"messages": [HumanMessage(content="table")]}
    first = routing_input(state)
    assert first is not None
    turn, _ = first
    state.update(
        visualization_turn=turn,
        visualization_decision=decision_from_response(_response("table")),
    )
    state["messages"].append(AIMessage(content="done"))
    assert routing_input(state) is None
    state["messages"].append(HumanMessage(content="table"))
    second = routing_input(state)
    assert second is not None
    assert second[0] != turn


@pytest.mark.parametrize(
    ("prompt", "expected_vis", "expected_renderer", "expected_kind"),
    [
        ("What is the capital of France?", "text", "text", None),
        ("Compare Q1 and Q2 revenue in a table", "table", "a2ui", "table"),
        ("Show a bar chart comparing regional sales", "bar_chart", "open_generative_ui", "bar_chart"),
        ("Plot a line chart of monthly trend over time", "line_chart", "open_generative_ui", "sandboxed_ui"),
        ("Show a scatter plot of latency vs throughput", "scatter_plot", "open_generative_ui", "sandboxed_ui"),
        ("Render a histogram of response times", "distribution", "open_generative_ui", "sandboxed_ui"),
        ("Show a pie chart of budget allocation breakdown", "part_to_whole", "open_generative_ui", "pie_chart"),
        ("Draw a flowchart of the ingestion pipeline", "flowchart", "open_generative_ui", "sandboxed_ui"),
        ("Draw an SVG diagram of the system architecture", "static_diagram", "open_generative_ui", "sandboxed_ui"),
        ("Build an interactive simulation of pendulum motion", "interactive_diagram", "open_generative_ui", "sandboxed_ui"),
        ("Create a trip itinerary for a multi-stop road trip", "animated_route", "open_generative_ui", "sandboxed_ui"),
        ("Plot locations on a geographic map", "map", "open_generative_ui", "sandboxed_ui"),
        ("Build a compound interest calculator", "calculator", "open_generative_ui", "sandboxed_ui"),
        ("Create a step-by-step plan for database migration", "plan", "a2ui", "plan"),
        ("Schedule a meeting time slot for tomorrow", "meeting_time_picker", "a2ui", "meeting_time_picker"),
    ],
)
def test_local_route_visualization_classifies_all_supported_modes(
    prompt: str,
    expected_vis: str,
    expected_renderer: str,
    expected_kind: str | None,
) -> None:
    """Verify route_visualization deterministically classifies prompts without network calls."""
    decision = route_visualization(prompt, evidence_count=3, local_only=True)
    assert decision["visualization"] == expected_vis
    assert decision["renderer"] == expected_renderer
    assert decision["kind"] == expected_kind
    assert decision["evidence_count"] == 3
    assert decision["local_only"] is True
    assert decision["source"] == "local"
    assert len(VISUALIZATIONS) == 13
    assert router.JEV_URL.startswith("https://")
