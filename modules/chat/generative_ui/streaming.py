"""Streaming tool-call accumulation, partial UI snapshots and final payload construction.

The chat worker feeds `delta.tool_calls` chunks into `ToolCallAccumulator`, publishes cumulative
`streaming_snapshot` dicts while a terminal UI tool is being written, and calls
`finalize_generative_ui` once the stream ends. Final arguments are re-validated with pydantic;
the model never supplies evidence identifiers (only citation numbers the server resolves).
"""

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel
from pydantic_core import from_json

from modules.chat.generative_ui.prompt import CANONICAL_SANDBOX_PARAM_ORDER, SandboxedUiArgs
from modules.chat.generative_ui.skill_backend import load_skill
from modules.chat.generative_ui.tool_specs import (
    TERMINAL_UI_TOOLS,
    TOOL_CHART,
    TOOL_PLAN,
    TOOL_SANDBOXED_UI,
    TOOL_TABLE,
    TOOL_TIME_PICKER,
    ChartToolArgs,
    PlanToolArgs,
    ReadSkillArgs,
    TableToolArgs,
    TimePickerToolArgs,
    parse_ui_tool_args,
)
from modules.chat.generative_ui.tools import (
    ChartPayload,
    CitationRef,
    PlanPayload,
    PlanStep,
    TablePayload,
    build_chart_payload,
    build_meeting_time_picker_payload,
    build_plan_payload,
    build_table_payload,
)
from modules.chat.generative_ui.visualization_router import route_visualization

logger = logging.getLogger(__name__)

# A skill document is context for the model, not a transcript row: bound what is re-sent.
MAX_SKILL_RESULT_CHARS = 24_000

# Fallback sandbox-widget title per owner locale (`en-US` / `vi-VN`); unknown locales use English.
_FALLBACK_TITLES = {"en-US": "Interactive view", "vi-VN": "Chế độ xem tương tác"}

# tool name -> (payload slot, renderer id kept for the persisted contract)
_SLOT = {
    TOOL_SANDBOXED_UI: ("sandbox", "open_generative_ui"),
    TOOL_TABLE: ("table", "a2ui"),
    TOOL_CHART: ("chart", "open_generative_ui"),
    TOOL_PLAN: ("plan", "a2ui"),
    TOOL_TIME_PICKER: ("form", "a2ui"),
}


class ToolCallAccumulator:
    """Collect streamed `delta.tool_calls` fragments by index into (id, name, arguments) triples."""

    def __init__(self) -> None:
        """Start with no calls."""
        self._calls: dict[int, dict[str, str]] = {}

    def feed(self, fragments: Any) -> None:
        """Merge one chunk's `tool_calls` list; malformed fragments are ignored.

        Args:
            fragments: The raw `delta.tool_calls` value from a chunk (any JSON type).
        """
        if not isinstance(fragments, list):
            return
        for frag in fragments:
            if not isinstance(frag, dict):
                continue
            idx = frag.get("index", 0)
            if not isinstance(idx, int):
                continue
            call = self._calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            if isinstance(frag.get("id"), str):
                call["id"] = frag["id"]
            fn = frag.get("function")
            if isinstance(fn, dict):
                # Providers send the name once (some repeat it); never concatenate it.
                if isinstance(fn.get("name"), str) and fn["name"]:
                    call["name"] = fn["name"]
                if isinstance(fn.get("arguments"), str):
                    call["arguments"] += fn["arguments"]

    def calls(self) -> list[dict[str, str]]:
        """Return calls ordered by stream index, each with `id`, `name`, `arguments`."""
        return [self._calls[i] for i in sorted(self._calls)]

    def terminal(self) -> dict[str, str] | None:
        """Return the first terminal UI call (first wins), or None."""
        return next((c for c in self.calls() if c["name"] in TERMINAL_UI_TOOLS), None)


def _chart_kind(chart_type: Any) -> str:
    """Map a chart type to the persisted widget kind (compat: only pie differs; true type lives in `chart.chart_type`)."""
    return "pie_chart" if chart_type == "pie" else "bar_chart"


def streaming_snapshot(name: str, raw_args: str) -> dict[str, Any] | None:
    """Build a cumulative `status="streaming"` snapshot from partially received tool arguments.

    Args:
        name: Terminal UI tool name.
        raw_args: Argument JSON received so far (may end mid-string).

    Returns:
        `{kind, status, title, <slot>}` or None while nothing parseable has arrived yet.
        Model-side `citations` numbers are stripped: they are only meaningful after server resolution.
    """
    if name not in _SLOT or not raw_args.strip():
        return None
    try:
        obj = from_json(raw_args.encode(), allow_partial="trailing-strings")
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    slot = _SLOT[name][0]
    if name == TOOL_SANDBOXED_UI:
        body: dict[str, Any] = {k: obj[k] for k in CANONICAL_SANDBOX_PARAM_ORDER if k in obj}
        if isinstance(body.get("jsExpressions"), list):
            body["jsExpressions"] = "\n".join(str(x) for x in body["jsExpressions"])
        kind = "sandboxed_ui"
    else:
        body = {k: v for k, v in obj.items() if k != "citations"}
        kind = {
            TOOL_TABLE: "table", TOOL_PLAN: "plan", TOOL_TIME_PICKER: "meeting_time_picker",
        }.get(name) or _chart_kind(obj.get("chart_type"))
        if name == TOOL_PLAN and isinstance(body.get("steps"), list):
            body["steps"] = [
                {"id": f"step-{i}", **s} for i, s in enumerate(body["steps"], 1) if isinstance(s, dict)
            ]
    title = obj.get("title")
    return {
        "kind": kind, "status": "streaming",
        "title": title if isinstance(title, str) else "",
        slot: body,
    }


def _cite_field(c: Mapping[str, Any], camel: str, snake: str) -> Any:
    """Return a citation field under either spelling (wire camelCase or the worker's snake_case aliases)."""
    return c.get(camel) or c.get(snake)


def _resolve_citations(numbers: Sequence[int], citations: Mapping[int, Mapping[str, Any]]) -> list[CitationRef]:
    """Resolve model-written citation numbers to server-validated document citations.

    Unknown numbers and web citations (which carry no document identifiers) are dropped; the model
    can therefore never introduce an identifier the answer validation did not already accept.
    """
    refs: list[CitationRef] = []
    seen: set[str] = set()
    for n in numbers:
        c = citations.get(n)
        if not c:
            continue
        # The worker passes `Citation.model_dump(by_alias=True)`, whose aliases are snake_case; camelCase
        # is the wire/schema spelling. Accept both so a validated citation is never silently dropped.
        source_id, document_id, chunk_id = _cite_field(c, "sourceId", "source_id"), _cite_field(c, "documentId", "document_id"), _cite_field(c, "chunkId", "chunk_id")
        version_id, title = _cite_field(c, "documentVersionId", "document_version_id"), _cite_field(c, "title", "title")
        if not (source_id and document_id and chunk_id) or str(chunk_id) in seen:
            continue
        seen.add(str(chunk_id))
        refs.append(CitationRef(
            source_id=str(source_id), document_id=str(document_id), chunk_id=str(chunk_id),
            document_version_id=str(version_id) if version_id else None,
            title=str(title) if title else None,
        ))
    return refs


def _envelope(
    kind: str, visualization: str, renderer: str, title: str, local_only: bool, slot: str, body: Mapping[str, Any],
) -> dict[str, Any]:
    """Assemble the persisted payload shape with every slot present and only `slot` filled."""
    payload: dict[str, Any] = {
        "kind": kind, "status": "completed", "visualization": visualization, "renderer": renderer,
        "title": title, "local_only": local_only,
        "sandbox": None, "plan": None, "table": None, "chart": None, "form": None,
    }
    payload[slot] = dict(body)
    return payload


def finalize_generative_ui(
    tool_name: str,
    raw_args: str,
    citations: Mapping[int, Mapping[str, Any]],
    local_only: bool,
    *,
    title_hint: str = "",
    locale: str = "en-US",
) -> dict[str, Any] | None:
    """Validate a completed terminal tool call and build the persisted `generative_ui` payload.

    Args:
        tool_name: Terminal UI tool name.
        raw_args: Complete argument JSON from the stream.
        citations: Context citation number -> validated citation dict (see `_resolve_citations`).
        local_only: Whether the turn referenced local-only evidence.
        title_hint: Title used for sandbox widgets (their schema has no title).
        locale: Owner locale (`en-US` / `vi-VN`) for the fallback sandbox title when `title_hint` is empty.

    Returns:
        JSON-serializable payload, or None (logged) when the arguments are invalid; the answer text
        then stands without a widget. Never raises for model-controlled input.
    """
    try:
        args: BaseModel = parse_ui_tool_args(tool_name, raw_args)
    except ValueError as exc:
        logger.warning("Dropping invalid generative UI call %s (%s)", tool_name, type(exc).__name__)
        return None
    try:
        if isinstance(args, SandboxedUiArgs):
            title = title_hint.strip()[:72] or _FALLBACK_TITLES.get(locale, _FALLBACK_TITLES["en-US"])
            return _envelope("sandboxed_ui", "custom_ui", "open_generative_ui", title, local_only,
                             "sandbox", args.to_ordered_dict())
        if isinstance(args, TableToolArgs):
            table = TablePayload(
                title=args.title, columns=args.columns, rows=args.rows,
                records=[dict(zip((c.key for c in args.columns), row, strict=False)) for row in args.rows],
                citations=_resolve_citations(args.citations, citations),
                source=args.source, is_sample=args.is_sample,
            )
            return _envelope("table", "table", "a2ui", args.title, local_only, "table", table.model_dump(mode="json"))
        if isinstance(args, ChartToolArgs):
            chart = ChartPayload(
                chart_type=args.chart_type, title=args.title, subtitle=args.subtitle, x_key=args.x_key,
                series=args.series, data=args.data, citations=_resolve_citations(args.citations, citations),
                source=args.source, unit=args.unit, is_sample=args.is_sample,
            )
            return _envelope(_chart_kind(args.chart_type), args.chart_type, "open_generative_ui", args.title,
                             local_only, "chart", chart.model_dump(mode="json"))
        if isinstance(args, PlanToolArgs):
            steps = [PlanStep(id=f"step-{i}", **s.model_dump()) for i, s in enumerate(args.steps, 1)]
            plan = PlanPayload(
                title=args.title, approach=args.approach, technology=args.technology,
                key_elements=args.key_elements, steps=steps, source=args.source,
                todos=[{"id": s.id, "title": s.title, "description": s.description, "emoji": s.emoji,
                        "status": "completed" if s.status == "completed" else "pending"} for s in steps],
            )
            return _envelope("plan", "plan", "a2ui", args.title, local_only, "plan", plan.model_dump(mode="json"))
        if isinstance(args, TimePickerToolArgs):
            picker = build_meeting_time_picker_payload(
                args.title, slots=args.slots, duration_minutes=args.duration_minutes,
                timezone=args.timezone, subtitle=args.subtitle,
            )
            return _envelope("meeting_time_picker", "meeting_time_picker", "a2ui", args.title, local_only,
                             "form", picker.model_dump(mode="json"))
    except ValueError as exc:  # server-side model construction (limits) rejected the data
        logger.warning("Dropping generative UI payload %s (%s)", tool_name, type(exc).__name__)
    return None


def skill_tool_result(raw_args: str) -> str:
    """Run the non-terminal `read_skill` tool; failures become a short error string for the model.

    Args:
        raw_args: JSON arguments (`{"name": ...}`).

    Returns:
        Skill document text truncated to `MAX_SKILL_RESULT_CHARS`, or an error message. Names are
        resolved through the allowlisting skill backend (no path traversal).
    """
    try:
        name = ReadSkillArgs.model_validate_json(raw_args).name
        return load_skill(name)[:MAX_SKILL_RESULT_CHARS]
    except (ValueError, OSError) as exc:
        logger.info("read_skill failed (%s)", type(exc).__name__)
        return "Error: unknown or invalid skill name."


def fallback_generative_ui(
    prompt: str, answer: str, evidence: Sequence[Any], *, local_only: bool,
) -> dict[str, Any] | None:
    """Build an evidence-derived native widget for models without tool calling.

    Only table, pie/bar chart and plan kinds are produced, and only from real evidence or answer
    numbers: when no records can be extracted nothing is rendered (never canned sample content).

    Args:
        prompt: The user's prompt (matched against the local routing rules).
        answer: Final answer text.
        evidence: Grounded evidence items.
        local_only: Whether the turn referenced local-only evidence.

    Returns:
        Persisted payload dict or None.
    """
    decision = route_visualization(prompt, evidence_count=len(evidence), local_only=local_only)
    kind = decision.get("kind")
    title = " ".join(prompt.split())[:72] or "Visualization"
    if kind == "plan":
        plan = build_plan_payload(prompt, answer, evidence=evidence)
        return _envelope("plan", "plan", "a2ui", plan.title, local_only, "plan", plan.model_dump(mode="json"))
    if kind == "table":
        table = build_table_payload(title, evidence, prompt=prompt, answer_text=answer)
        if not table.rows:
            return None
        return _envelope("table", "table", "a2ui", table.title, local_only, "table", table.model_dump(mode="json"))
    if kind in {"bar_chart", "pie_chart"}:
        chart = build_chart_payload(
            title, "pie" if kind == "pie_chart" else "bar", evidence, prompt=prompt, answer_text=answer,
        )
        if not chart.data:
            return None
        return _envelope(kind, chart.chart_type, "open_generative_ui", chart.title, local_only, "chart",
                         chart.model_dump(mode="json"))
    return None
