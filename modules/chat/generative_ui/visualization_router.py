"""Local, deterministic prompt classification used by the no-tools Generative UI fallback.

The model-driven path chooses its own presentation tool; this module only decides whether a
fallback native widget (table/chart/plan) is warranted when the configured model cannot call tools.
It performs no network I/O.
"""

import re
from typing import Any

# Ordered heuristic patterns for deterministic local classification.
_LOCAL_RULES: tuple[tuple[str, str, str, re.Pattern[str]], ...] = (
    (
        "meeting_time_picker",
        "a2ui",
        "meeting_time_picker",
        re.compile(
            r"\b(meeting\s+time|time\s+slot|schedule\s+a\s+(?:meeting|call|sync)|"
            r"pick\s+a\s+(?:time|slot)|book\s+a\s+(?:slot|meeting)|availability\s+picker|"
            r"meeting\s+picker|calendar\s+slot)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "plan",
        "a2ui",
        "plan",
        re.compile(
            r"\b(step[-\s]by[-\s]step\s+plan|action\s+plan|project\s+plan|execution\s+plan|"
            r"implementation\s+plan|todo\s+list|to[-\s]do\s+list|checklist|rollout\s+plan|"
            r"migration\s+plan|study\s+plan|manage[_\s]todos)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "calculator",
        "open_generative_ui",
        "sandboxed_ui",
        re.compile(
            r"\b(calculator|mortgage|compound\s+interest|loan\s+estimator|roi\s+tool|"
            r"tax\s+estimator|unit\s+converter|interactive\s+estimator)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "animated_route",
        "open_generative_ui",
        "sandboxed_ui",
        re.compile(
            r"\b(animated\s+route|trip\s+itinerary|road\s+trip|multi[-\s]stop\s+(?:trip|journey|route)|"
            r"travel\s+itinerary\s+map|sequential\s+stops)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "map",
        "open_generative_ui",
        "sandboxed_ui",
        re.compile(
            r"\b(geographic\s+map|live\s+map|tiled\s+map|map\s+of\b|show\s+on\s+a\s+map|"
            r"plot\s+locations|map\s+markers|leaflet\s+map)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "interactive_diagram",
        "open_generative_ui",
        "sandboxed_ui",
        re.compile(
            r"\b(interactive\s+diagram|interactive\s+simulation|interactive\s+model|"
            r"simulate\b|simulation\s+of|adjustable\s+sliders|explore\s+variables)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "flowchart",
        "open_generative_ui",
        "sandboxed_ui",
        re.compile(
            r"\b(flowchart|flow\s+chart|process\s+flow|decision\s+tree|workflow\s+diagram|"
            r"state\s+machine\s+diagram|pipeline\s+diagram)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "static_diagram",
        "open_generative_ui",
        "sandboxed_ui",
        re.compile(
            r"\b(svg\s+diagram|architecture\s+diagram|system\s+diagram|static\s+diagram|"
            r"structural\s+diagram|block\s+diagram|network\s+topology|component\s+diagram)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "part_to_whole",
        "open_generative_ui",
        "pie_chart",
        re.compile(
            r"\b(pie\s+chart|donut\s+chart|doughnut\s+chart|part[-\s]to[-\s]whole|"
            r"proportion\s+breakdown|percentage\s+share|allocation\s+breakdown)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "line_chart",
        "open_generative_ui",
        "sandboxed_ui",
        re.compile(
            r"\b(line\s+chart|line\s+graph|trend\s+over\s+time|time\s+series|"
            r"monthly\s+trend|historical\s+trend|growth\s+curve)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "scatter_plot",
        "open_generative_ui",
        "sandboxed_ui",
        re.compile(
            r"\b(scatter\s+plot|scatter\s+chart|scatter\s+graph|correlation\s+plot|"
            r"x\s+vs\.?\s+y\s+plot)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "distribution",
        "open_generative_ui",
        "sandboxed_ui",
        re.compile(
            r"\b(histogram|box\s+plot|boxplot|distribution\s+chart|frequency\s+distribution)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "bar_chart",
        "open_generative_ui",
        "bar_chart",
        re.compile(
            r"\b(bar\s+chart|bar\s+graph|column\s+chart|rank\s+categories|"
            r"compare\s+magnitudes|chart\s+comparing)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "table",
        "a2ui",
        "table",
        re.compile(
            r"\b(in\s+a\s+table|comparison\s+table|tabular\s+comparison|"
            r"side[-\s]by[-\s]side\s+table|summary\s+table|data\s+table|table\s+of\b)\b",
            re.IGNORECASE,
        ),
    ),
)


def route_visualization(
    prompt: str,
    *,
    evidence_count: int = 0,
    local_only: bool = False,
) -> dict[str, Any]:
    """Deterministically classify a user prompt into a visualization and renderer locally.

    Never performs external network calls when `local_only=True` or during local heuristic
    routing, preserving owner privacy fences.

    Args:
        prompt: The user's message text for the current turn.
        evidence_count: Number of grounded evidence chunks retrieved for the turn.
        local_only: Whether the turn is fenced by a `local_only` source policy.

    Returns:
        Dictionary with `renderer`, `visualization`, `kind`, `confidence`, `source`,
        `evidence_count`, and `local_only`.
    """
    normalized = (prompt or "").strip()
    if not normalized:
        return {
            "renderer": "text",
            "visualization": "text",
            "kind": None,
            "confidence": 1.0,
            "source": "local",
            "evidence_count": evidence_count,
            "local_only": local_only,
        }

    for visualization, renderer, kind, pattern in _LOCAL_RULES:
        if pattern.search(normalized):
            return {
                "renderer": renderer,
                "visualization": visualization,
                "kind": kind,
                "confidence": 0.92,
                "source": "local",
                "evidence_count": evidence_count,
                "local_only": local_only,
            }

    return {
        "renderer": "text",
        "visualization": "text",
        "kind": None,
        "confidence": 0.95,
        "source": "local",
        "evidence_count": evidence_count,
        "local_only": local_only,
    }
