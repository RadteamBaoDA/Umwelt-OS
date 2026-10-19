"""Backend Generative UI subsystem for Umwelt-OS chat turns."""

from collections.abc import Sequence
from html import escape
from typing import Any, Literal

from modules.chat.generative_ui.assembler import (
    APPROVED_CDN_HOSTS,
    BRIDGE_JS,
    FORM_STYLES_CSS,
    SVG_CLASSES_CSS,
    UMWELT_THEME_CSS,
    assemble_document,
)
from modules.chat.generative_ui.prompt import (
    CANONICAL_SANDBOX_PARAM_ORDER,
    SYSTEM_PROMPT,
    SandboxedUiArgs,
    build_generative_ui_system_prompt,
)
from modules.chat.generative_ui.skill_backend import (
    SKILL_ALIASES,
    SKILL_NAMES,
    SKILL_SOURCES,
    BundledSkillsBackend,
    CompositeBackend,
    SkillMetadata,
    StateBackend,
    build_agent_backend,
    list_skill_metadata,
    list_skills,
    load_skill,
    parse_skill_frontmatter,
    search_skills,
    validate_skill_document,
)
from modules.chat.generative_ui.templates import (
    SEED_TEMPLATES,
    TEMPLATE_USAGE_NOTE,
    UITemplate,
    apply_template,
    clear_pending_template,
    delete_template,
    list_templates,
    save_template,
    template_tools,
)
from modules.chat.generative_ui.tools import (
    ChartPayload,
    ChartSeriesConfig,
    CitationRef,
    MeetingTimePickerPayload,
    PlanPayload,
    PlanStep,
    TableColumn,
    TablePayload,
    TimeSlot,
    Todo,
    build_chart_payload,
    build_local_svg_data_uri,
    build_meeting_time_picker_payload,
    build_plan_payload,
    build_table_payload,
    fetch_card,
    generate_form,
    get_todos,
    get_trip_stop_images,
    manage_todos,
    plan_visualization,
    query_data,
    query_records,
    todo_tools,
    validate_image_url,
)
from modules.chat.generative_ui.visualization_router import (
    JEV_URL,
    RENDER_TOOLS,
    VISUALIZATIONS,
    ChoiceAnswer,
    RoutingState,
    aroute,
    decision_from_response,
    message_text,
    parse_response,
    request_options,
    route,
    route_visualization,
    routed_request,
    routing_input,
)


def _derive_title(prompt: str, visualization: str) -> str:
    """Derive a concise, human-readable widget title from the user prompt and visualization kind."""
    cleaned = " ".join((prompt or "").strip().split())
    if cleaned:
        return cleaned[:72]
    return visualization.replace("_", " ").title()


def _build_sandbox_args_for_visualization(
    visualization: str,
    title: str,
    answer_text: str,
    evidence: Sequence[Any],
    *,
    local_only: bool = False,
) -> SandboxedUiArgs:
    """Construct validated `SandboxedUiArgs` tailored to the requested visualization type."""
    safe_title = escape(title[:80])
    summary_line = escape(
        (" ".join(answer_text.strip().split())[:220])
        or f"Interactive {visualization.replace('_', ' ')} view"
    )
    css = (
        ".u-widget { border: 1px solid var(--line); border-radius: 12px; "
        "background: var(--surface); color: var(--text); padding: 16px; }\n"
        ".u-header { display: flex; justify-content: space-between; align-items: baseline; "
        "gap: 12px; margin-bottom: 12px; }\n"
        ".u-title { margin: 0; font-size: 16px; font-weight: 600; color: var(--text); }\n"
        ".u-badge { font-size: 11px; color: var(--muted); padding: 2px 8px; "
        "border-radius: 999px; border: 1px solid var(--line); background: var(--bg); }\n"
        ".u-summary { margin: 0 0 12px; font-size: 13px; color: var(--muted); }\n"
        ".u-controls { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; "
        "margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--line); }\n"
        ".u-status { margin-top: 8px; font-size: 12px; color: var(--accent); min-height: 18px; }"
    )

    if visualization == "calculator":
        html = (
            '<div class="u-widget">'
            '<div class="u-header">'
            f'<h3 class="u-title">{safe_title}</h3>'
            '<span class="u-badge">Interactive Calculator</span>'
            "</div>"
            f'<p class="u-summary">{summary_line}</p>'
            '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;">'
            '<label style="display:grid;gap:4px;font-size:12px;">'
            "<span>Principal / Base Input</span>"
            '<input id="calc-principal" type="number" value="1000" min="0" max="100000000" step="100">'
            "</label>"
            '<label style="display:grid;gap:4px;font-size:12px;">'
            "<span>Rate / Multiplier (%)</span>"
            '<input id="calc-rate" type="number" value="7.5" min="0" max="100" step="0.5">'
            "</label>"
            '<label style="display:grid;gap:4px;font-size:12px;">'
            "<span>Periods / Units</span>"
            '<input id="calc-periods" type="number" value="10" min="1" max="100" step="1">'
            "</label>"
            "</div>"
            '<div id="calc-output" role="status" style="margin-top:12px;padding:10px;border-radius:8px;background:var(--bg);font-weight:600;">'
            "Projected Output: 2,061.03"
            "</div>"
            '<div class="u-controls">'
            '<button type="button" onclick="recalculateWidget()">Calculate</button>'
            '<button type="button" onclick="resetCalculator()">Reset</button>'
            '<button id="followup-btn" type="button" onclick="sendCalculatorFollowup(this)">Ask about assumptions</button>'
            "</div>"
            '<p id="status" class="u-status" role="status"></p>'
            "</div>"
        )
        js_functions = (
            "function recalculateWidget() {\n"
            "  var pEl = document.getElementById('calc-principal');\n"
            "  var rEl = document.getElementById('calc-rate');\n"
            "  var nEl = document.getElementById('calc-periods');\n"
            "  var out = document.getElementById('calc-output');\n"
            "  var p = pEl ? pEl.valueAsNumber : NaN;\n"
            "  var r = rEl ? rEl.valueAsNumber : NaN;\n"
            "  var n = nEl ? nEl.valueAsNumber : NaN;\n"
            "  if (!Number.isFinite(p) || p < 0 || !Number.isFinite(r) || r < 0 || !Number.isFinite(n) || n < 1) {\n"
            "    out.textContent = 'Enter non-negative finite values (periods >= 1).';\n"
            "    return null;\n"
            "  }\n"
            "  var total = p * Math.pow(1 + (r / 100), n);\n"
            "  out.textContent = 'Projected Output: ' + new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 }).format(total);\n"
            "  return total;\n"
            "}\n"
            "function resetCalculator() {\n"
            "  document.getElementById('calc-principal').value = '1000';\n"
            "  document.getElementById('calc-rate').value = '7.5';\n"
            "  document.getElementById('calc-periods').value = '10';\n"
            "  recalculateWidget();\n"
            "}\n"
            "async function sendCalculatorFollowup(btn) {\n"
            "  var status = document.getElementById('status');\n"
            "  btn.disabled = true;\n"
            "  try {\n"
            "    var outText = document.getElementById('calc-output').textContent;\n"
            "    await Websandbox.connection.remote.sendPrompt({ text: 'Explain sensitivity for ' + outText });\n"
            "    status.textContent = 'Follow-up sent.';\n"
            "  } catch (err) {\n"
            "    status.textContent = 'Could not send follow-up.';\n"
            "  } finally {\n"
            "    btn.disabled = false;\n"
            "  }\n"
            "}"
        )
        js_expressions = "recalculateWidget();"
        return SandboxedUiArgs(
            initialHeight=360,
            placeholderMessages=[
                "Configuring calculator inputs…",
                "Applying validation rules…",
                "Initializing interactive model…",
            ],
            css=css,
            html=html,
            jsFunctions=js_functions,
            jsExpressions=js_expressions,
        )

    plan_steps = build_plan_payload(title, answer_text, evidence=evidence).steps[:4]
    nodes_svg: list[str] = []
    for idx, step in enumerate(plan_steps):
        y_pos = 24 + idx * 68
        step_label = escape(step.title[:36])
        nodes_svg.append(
            f'<g class="c-teal" data-step="{idx}" onclick="selectDiagramStep({idx})" style="cursor:pointer;">'
            f'<rect x="140" y="{y_pos}" width="400" height="44" rx="8" fill="#E1F5EE" stroke="#0F6E56" stroke-width="0.8"/>'
            f'<text x="340" y="{y_pos + 22}" text-anchor="middle" dominant-baseline="central" '
            f'font-size="14" font-weight="500" fill="#085041">{idx + 1}. {step_label}</text>'
            "</g>"
        )
        if idx < len(plan_steps) - 1:
            line_y1 = y_pos + 44
            line_y2 = y_pos + 68
            nodes_svg.append(
                f'<line x1="340" y1="{line_y1}" x2="340" y2="{line_y2}" '
                'stroke="#0F6E56" stroke-width="1.5" marker-end="url(#arrow)"/>'
            )

    svg_height = max(180, 32 + len(plan_steps) * 68)
    badge = "Local-Only SVG" if local_only else visualization.replace("_", " ").title()
    html = (
        '<div class="u-widget">'
        '<div class="u-header">'
        f'<h3 class="u-title">{safe_title}</h3>'
        f'<span class="u-badge">{escape(badge)}</span>'
        "</div>"
        f'<p class="u-summary">{summary_line}</p>'
        f'<svg width="100%" viewBox="0 0 680 {svg_height}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{safe_title}">'
        "<defs>"
        '<marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        '<path d="M2 1L8 5L2 9" fill="none" stroke="#0F6E56" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>'
        "</marker>"
        "</defs>"
        + "".join(nodes_svg)
        + "</svg>"
        '<div class="u-controls">'
        '<button id="followup-btn" type="button" onclick="askAboutDiagram(this)">Explore in chat</button>'
        "</div>"
        '<p id="status" class="u-status" role="status">Click any node to inspect details.</p>'
        "</div>"
    )
    js_functions = (
        "var selectedStepIndex = 0;\n"
        "function selectDiagramStep(idx) {\n"
        "  selectedStepIndex = Number(idx) || 0;\n"
        "  var status = document.getElementById('status');\n"
        "  if (status) status.textContent = 'Selected node ' + (selectedStepIndex + 1) + '.';\n"
        "}\n"
        "async function askAboutDiagram(btn) {\n"
        "  var status = document.getElementById('status');\n"
        "  btn.disabled = true;\n"
        "  try {\n"
        "    await Websandbox.connection.remote.sendPrompt({ text: 'Explain step ' + (selectedStepIndex + 1) + ' in detail.' });\n"
        "    if (status) status.textContent = 'Question sent to chat.';\n"
        "  } catch (err) {\n"
        "    if (status) status.textContent = 'Could not send prompt.';\n"
        "  } finally {\n"
        "    btn.disabled = false;\n"
        "  }\n"
        "}"
    )
    js_expressions = "selectDiagramStep(0);"
    return SandboxedUiArgs(
        initialHeight=min(520, svg_height + 140),
        placeholderMessages=[
            "Structuring visual layout…",
            "Applying Umwelt-OS semantic tokens…",
            "Wiring interactive node handlers…",
        ],
        css=css,
        html=html,
        jsFunctions=js_functions,
        jsExpressions=js_expressions,
    )


def build_generative_ui_for_turn(
    prompt: str,
    answer_text: str,
    evidence: Sequence[Any] = (),
    *,
    decision: dict[str, Any] | None = None,
    local_only: bool = False,
) -> dict[str, Any] | None:
    """Build a complete, JSON-serializable Generative UI payload for a chat turn.

    Returns `None` when the turn is routed to plain `text`. Otherwise constructs a
    validated payload dictionary for `"sandboxed_ui"`, `"plan"`, `"table"`,
    `"bar_chart"`, `"pie_chart"`, or `"meeting_time_picker"`.

    Args:
        prompt: The user's prompt text.
        answer_text: The assistant's grounded response text.
        evidence: Grounded `EvidenceItem` or dict objects retrieved for the turn.
        decision: Optional pre-computed output from `route_visualization` or Jev.
        local_only: Whether external network scripts and media are forbidden.

    Returns:
        JSON-serializable dictionary or `None` for plain text turns.
    """
    inferred_local_only = local_only or any(
        bool(item.get("local_only") if isinstance(item, dict) else getattr(item, "local_only", False))
        for item in evidence
    )
    resolved_decision = decision or route_visualization(
        prompt,
        evidence_count=len(evidence),
        local_only=inferred_local_only,
    )
    visualization = str(resolved_decision.get("visualization", "text"))
    renderer = str(resolved_decision.get("renderer", "text"))
    if visualization == "text" or renderer == "text":
        return None

    kind = resolved_decision.get("kind")
    if not kind:
        kind_map = {
            "table": "table",
            "bar_chart": "bar_chart",
            "part_to_whole": "pie_chart",
            "pie_chart": "pie_chart",
            "plan": "plan",
            "meeting_time_picker": "meeting_time_picker",
        }
        kind = kind_map.get(visualization, "sandboxed_ui")

    title = _derive_title(prompt, visualization)
    sandbox_dict: dict[str, Any] | None = None
    plan_dict: dict[str, Any] | None = None
    table_dict: dict[str, Any] | None = None
    chart_dict: dict[str, Any] | None = None
    form_dict: dict[str, Any] | None = None
    assembled_html: str | None = None

    if kind == "plan":
        plan_payload = build_plan_payload(prompt, answer_text, evidence=evidence)
        plan_dict = plan_payload.model_dump(mode="json")
    elif kind == "table":
        table_payload = build_table_payload(
            title,
            evidence,
            prompt=prompt,
            answer_text=answer_text,
        )
        table_dict = table_payload.model_dump(mode="json")
    elif kind in {"bar_chart", "pie_chart"}:
        chart_type: Literal["pie", "bar"] = "pie" if kind == "pie_chart" else "bar"
        chart_payload = build_chart_payload(
            title,
            chart_type=chart_type,
            records_or_evidence=evidence,
            prompt=prompt,
            answer_text=answer_text,
        )
        chart_dict = chart_payload.model_dump(mode="json")
    elif kind == "meeting_time_picker":
        picker_payload = build_meeting_time_picker_payload(title=title)
        form_dict = picker_payload.model_dump(mode="json")
    else:
        kind = "sandboxed_ui"
        sandbox_args = _build_sandbox_args_for_visualization(
            visualization,
            title,
            answer_text,
            evidence,
            local_only=inferred_local_only,
        )
        sandbox_dict = sandbox_args.to_ordered_dict()
        assembled_html = assemble_document(
            html=sandbox_args.html,
            css=sandbox_args.css,
            js_functions=sandbox_args.jsFunctions,
            js_expressions=sandbox_args.jsExpressions,
            title=title,
            local_only=inferred_local_only,
        )

    return {
        "kind": kind,
        "status": "completed",
        "visualization": visualization,
        "renderer": renderer,
        "title": title,
        "local_only": inferred_local_only,
        "sandbox": sandbox_dict,
        "assembled_html": assembled_html,
        "plan": plan_dict,
        "table": table_dict,
        "chart": chart_dict,
        "form": form_dict,
    }


def iter_generative_ui_deltas(ui_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Emit progressive streaming delta dictionaries for a Generative UI payload.

    For `sandboxed_ui`, emits 6 progressive deltas adding keys in strict
    `CANONICAL_SANDBOX_PARAM_ORDER` (`initialHeight` -> `placeholderMessages` -> `css` ->
    `html` -> `jsFunctions` -> `jsExpressions`) with `"status": "streaming"`.
    For native widgets, emits an initial skeleton streaming delta followed by the populated
    streaming structure.

    Args:
        ui_payload: Completed Generative UI payload from `build_generative_ui_for_turn`.

    Returns:
        Ordered list of JSON-serializable streaming delta dicts.
    """
    kind = str(ui_payload.get("kind", "sandboxed_ui"))
    visualization = str(ui_payload.get("visualization", "interactive_diagram"))
    title = str(ui_payload.get("title", "Visualization"))
    local_only = bool(ui_payload.get("local_only", False))

    if kind == "sandboxed_ui" and isinstance(ui_payload.get("sandbox"), dict):
        sandbox = ui_payload["sandbox"]
        deltas: list[dict[str, Any]] = []
        cumulative_sandbox: dict[str, Any] = {}
        for idx, param_name in enumerate(CANONICAL_SANDBOX_PARAM_ORDER):
            if param_name in sandbox:
                cumulative_sandbox[param_name] = sandbox[param_name]
            deltas.append(
                {
                    "kind": "sandboxed_ui",
                    "status": "streaming",
                    "visualization": visualization,
                    "title": title,
                    "local_only": local_only,
                    "param": param_name,
                    "step": idx + 1,
                    "total_steps": len(CANONICAL_SANDBOX_PARAM_ORDER),
                    "sandbox": dict(cumulative_sandbox),
                    "plan": None,
                    "table": None,
                    "chart": None,
                    "form": None,
                }
            )
        return deltas

    return [
        {
            "kind": kind,
            "status": "streaming",
            "visualization": visualization,
            "title": title,
            "local_only": local_only,
            "step": 1,
            "total_steps": 2,
            "sandbox": None,
            "plan": None,
            "table": None,
            "chart": None,
            "form": None,
        },
        {
            "kind": kind,
            "status": "streaming",
            "visualization": visualization,
            "title": title,
            "local_only": local_only,
            "step": 2,
            "total_steps": 2,
            "sandbox": ui_payload.get("sandbox"),
            "plan": ui_payload.get("plan"),
            "table": ui_payload.get("table"),
            "chart": ui_payload.get("chart"),
            "form": ui_payload.get("form"),
        },
    ]


__all__ = [
    "APPROVED_CDN_HOSTS",
    "BRIDGE_JS",
    "CANONICAL_SANDBOX_PARAM_ORDER",
    "FORM_STYLES_CSS",
    "JEV_URL",
    "RENDER_TOOLS",
    "SEED_TEMPLATES",
    "SKILL_ALIASES",
    "SKILL_NAMES",
    "SKILL_SOURCES",
    "SVG_CLASSES_CSS",
    "SYSTEM_PROMPT",
    "TEMPLATE_USAGE_NOTE",
    "UMWELT_THEME_CSS",
    "VISUALIZATIONS",
    "BundledSkillsBackend",
    "ChartPayload",
    "ChartSeriesConfig",
    "ChoiceAnswer",
    "CitationRef",
    "CompositeBackend",
    "MeetingTimePickerPayload",
    "PlanPayload",
    "PlanStep",
    "RoutingState",
    "SandboxedUiArgs",
    "SkillMetadata",
    "StateBackend",
    "TableColumn",
    "TablePayload",
    "TimeSlot",
    "Todo",
    "UITemplate",
    "apply_template",
    "aroute",
    "assemble_document",
    "build_agent_backend",
    "build_chart_payload",
    "build_generative_ui_for_turn",
    "build_generative_ui_system_prompt",
    "build_local_svg_data_uri",
    "build_meeting_time_picker_payload",
    "build_plan_payload",
    "build_table_payload",
    "clear_pending_template",
    "decision_from_response",
    "delete_template",
    "fetch_card",
    "generate_form",
    "get_todos",
    "get_trip_stop_images",
    "iter_generative_ui_deltas",
    "list_skill_metadata",
    "list_skills",
    "list_templates",
    "load_skill",
    "manage_todos",
    "message_text",
    "parse_response",
    "parse_skill_frontmatter",
    "plan_visualization",
    "query_data",
    "query_records",
    "request_options",
    "route",
    "route_visualization",
    "routed_request",
    "routing_input",
    "save_template",
    "search_skills",
    "template_tools",
    "todo_tools",
    "validate_image_url",
    "validate_skill_document",
]
