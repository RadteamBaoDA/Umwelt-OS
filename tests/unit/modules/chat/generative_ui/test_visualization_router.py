"""Unit tests for the deterministic local routing used by the no-tools fallback."""

import pytest

from modules.chat.generative_ui.visualization_router import route_visualization


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


def test_empty_prompt_routes_to_text() -> None:
    """An empty prompt never selects a widget."""
    assert route_visualization("   ")["kind"] is None
