"""Streaming tool-call accumulation, partial UI snapshots and final payload construction.

The chat worker feeds `delta.tool_calls` chunks into `ToolCallAccumulator`, publishes cumulative
`streaming_snapshot` dicts while a terminal UI tool is being written, and calls
`finalize_generative_ui` once the stream ends. Final arguments are re-validated with pydantic;
the model never supplies evidence identifiers (only citation numbers the server resolves).
"""

import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ValidationError
from pydantic_core import from_json

from modules.chat.generative_ui.prompt import CANONICAL_SANDBOX_PARAM_ORDER, SandboxedUiArgs
from modules.chat.generative_ui.skill_backend import load_skill
from modules.chat.generative_ui.tool_specs import (
    TERMINAL_UI_TOOLS,
    TOOL_ANSWER,
    TOOL_CHART,
    TOOL_FORM,
    TOOL_PLAN,
    TOOL_SANDBOXED_UI,
    TOOL_TABLE,
    TOOL_TIME_PICKER,
    AnswerGalleryBlock,
    AnswerSectionBlock,
    AnswerStatsBlock,
    ChartToolArgs,
    PlanToolArgs,
    ReadSkillArgs,
    RenderAnswerArgs,
    TableToolArgs,
    TimePickerToolArgs,
    parse_ui_tool_args,
)
from modules.chat.generative_ui.tools import (
    ChartPayload,
    CitationRef,
    FormPayload,
    PlanPayload,
    PlanStep,
    TablePayload,
    build_chart_payload,
    build_form_payload,
    build_meeting_time_picker_payload,
    build_plan_payload,
    build_table_payload,
)
from modules.chat.generative_ui.visualization_router import route_visualization

logger = logging.getLogger(__name__)

# A skill document is context for the model, not a transcript row: bound what is re-sent.
MAX_SKILL_RESULT_CHARS = 24_000

# Bound of the validation feedback re-sent to the model for an invalid widget call.
MAX_UI_ERROR_CHARS = 800

# Fallback sandbox-widget title per owner locale (`en-US` / `vi-VN`); unknown locales use English.
_FALLBACK_TITLES = {"en-US": "Interactive view", "vi-VN": "Chế độ xem tương tác"}

# tool name -> (payload slot, renderer id kept for the persisted contract)
_SLOT = {
    TOOL_ANSWER: ("answer", "a2ui"),
    TOOL_SANDBOXED_UI: ("sandbox", "open_generative_ui"),
    TOOL_TABLE: ("table", "a2ui"),
    TOOL_CHART: ("chart", "open_generative_ui"),
    TOOL_PLAN: ("plan", "a2ui"),
    TOOL_TIME_PICKER: ("form", "a2ui"),
    TOOL_FORM: ("form", "a2ui"),
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

    def terminals(self, limit: int) -> list[tuple[str, dict[str, str]]]:
        """Return up to ``limit`` presentation calls in stream order as ``(part_id, call)`` pairs.

        The part id is the provider tool-call id (cut to 64 chars) so it is stable while arguments stream and
        identical in snapshots and the final payload; a missing or repeated id falls back to ``part-<n>``.
        Calls beyond ``limit`` are dropped (turn cap).
        """
        out: list[tuple[str, dict[str, str]]] = []
        used: set[str] = set()
        for call in (c for c in self.calls() if c["name"] in TERMINAL_UI_TOOLS):
            if len(out) >= limit:
                break
            pid = call["id"][:64] or f"part-{len(out) + 1}"
            if pid in used:
                pid = f"part-{len(out) + 1}"
            used.add(pid)
            out.append((pid, call))
        return out


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
            TOOL_TABLE: "table",
            TOOL_PLAN: "plan",
            TOOL_TIME_PICKER: "meeting_time_picker",
            TOOL_FORM: "form",
        }.get(name) or _chart_kind(obj.get("chart_type"))
        if name == TOOL_PLAN and isinstance(body.get("steps"), list):
            body["steps"] = [
                {"id": f"step-{i}", **s}
                for i, s in enumerate(body["steps"], 1)
                if isinstance(s, dict)
            ]
    title = obj.get("title")
    if name == TOOL_ANSWER:
        # Images are resolved only at finalize; a streaming block shows no image yet and no citation numbers.
        raw_blocks = body.get("blocks")
        views = (
            [_snapshot_block(x) for x in raw_blocks if isinstance(x, dict)]
            if isinstance(raw_blocks, list)
            else []
        )
        body["blocks"] = [v for v in views if v is not None]
        kind = "answer"
    return {
        "kind": kind,
        "status": "streaming",
        "title": title if isinstance(title, str) else "",
        slot: body,
    }


def _snapshot_block(block: dict[str, Any]) -> dict[str, Any] | None:
    """Streaming view of one partial ``render_answer`` block: model-side ``citations``/``image_ids`` removed.

    Gallery and section blocks get ``images: []`` (ids resolve to proxy URLs only at finalize). Blocks without a
    string ``type`` are dropped.
    """
    if not isinstance(block.get("type"), str):
        return None
    view = {k: v for k, v in block.items() if k not in ("citations", "image_ids")}
    if view["type"] in ("gallery", "section"):
        view["images"] = []
    if view["type"] == "stats" and isinstance(view.get("items"), list):
        view["items"] = [
            {k: v for k, v in i.items() if k != "citations"}
            for i in view["items"]
            if isinstance(i, dict)
        ]
    return view


def _cite_field(c: Mapping[str, Any], camel: str, snake: str) -> Any:
    """Return a citation field under either spelling (wire camelCase or the worker's snake_case aliases)."""
    return c.get(camel) or c.get(snake)


def _resolve_citations(
    numbers: Sequence[int], citations: Mapping[int, Mapping[str, Any]]
) -> list[CitationRef]:
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
        source_id, document_id, chunk_id = (
            _cite_field(c, "sourceId", "source_id"),
            _cite_field(c, "documentId", "document_id"),
            _cite_field(c, "chunkId", "chunk_id"),
        )
        version_id, title = (
            _cite_field(c, "documentVersionId", "document_version_id"),
            _cite_field(c, "title", "title"),
        )
        if not (source_id and document_id and chunk_id) or str(chunk_id) in seen:
            continue
        seen.add(str(chunk_id))
        refs.append(
            CitationRef(
                source_id=str(source_id),
                document_id=str(document_id),
                chunk_id=str(chunk_id),
                document_version_id=str(version_id) if version_id else None,
                title=str(title) if title else None,
            )
        )
    return refs


def _envelope(
    kind: str,
    visualization: str,
    renderer: str,
    title: str,
    local_only: bool,
    slot: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    """Assemble the persisted payload shape with every slot present and only `slot` filled."""
    payload: dict[str, Any] = {
        "kind": kind,
        "status": "completed",
        "visualization": visualization,
        "renderer": renderer,
        "title": title,
        "local_only": local_only,
        "sandbox": None,
        "plan": None,
        "table": None,
        "chart": None,
        "form": None,
    }
    payload[slot] = dict(body)
    return payload


def _error_paths(exc: ValueError) -> str:
    """Summarize a validation failure as ``path: message`` lines; never echoes the offending input values."""
    if isinstance(exc, ValidationError):
        lines = [
            f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}"
            for e in exc.errors(include_input=False)
        ]
        return "; ".join(lines)[:MAX_UI_ERROR_CHARS]
    return "arguments are not valid JSON" if "json" in str(exc).lower() else type(exc).__name__


def ui_call_error(tool_name: str, raw_args: str) -> str | None:
    """Check a terminal UI call against its argument model and describe any problem for the model.

    Args:
        tool_name: Terminal UI tool name.
        raw_args: Complete argument JSON from the stream.

    Returns:
        None when valid; otherwise a bounded JSON tool-result string with field paths and messages (no
        raw user data) so the model can correct the call in its next round. Logged at warning level.
    """
    try:
        parse_ui_tool_args(tool_name, raw_args)
    except ValueError as exc:
        detail = _error_paths(exc)
        logger.warning("Invalid generative UI call %s: %s", tool_name, detail)
        return json.dumps(
            {
                "status": "error",
                "error": f"invalid {tool_name} arguments; fix and call again. {detail}",
            }
        )
    return None


def _table_envelope(
    args: TableToolArgs, citations: Mapping[int, Mapping[str, Any]], local_only: bool
) -> dict[str, Any]:
    """Build the persisted ``table`` envelope, deriving per-row ``records`` keyed by column key.

    Raises ``ValueError`` (pydantic validation) when the table limits reject the data; the caller drops the
    widget and keeps the answer text.
    """
    table = TablePayload(
        title=args.title,
        columns=args.columns,
        rows=args.rows,
        records=[dict(zip((c.key for c in args.columns), row, strict=False)) for row in args.rows],
        citations=_resolve_citations(args.citations, citations),
        source=args.source,
        is_sample=args.is_sample,
    )
    return _envelope(
        "table",
        "table",
        "a2ui",
        args.title,
        local_only,
        "table",
        table.model_dump(mode="json"),
    )


def _chart_envelope(
    args: ChartToolArgs, citations: Mapping[int, Mapping[str, Any]], local_only: bool
) -> dict[str, Any]:
    """Build the persisted chart envelope whose kind follows ``args.chart_type``.

    Raises ``ValueError`` (pydantic validation) when chart limits reject the data; the caller drops the widget.
    """
    chart = ChartPayload(
        chart_type=args.chart_type,
        title=args.title,
        subtitle=args.subtitle,
        x_key=args.x_key,
        series=args.series,
        data=args.data,
        citations=_resolve_citations(args.citations, citations),
        source=args.source,
        unit=args.unit,
        is_sample=args.is_sample,
    )
    return _envelope(
        _chart_kind(args.chart_type),
        args.chart_type,
        "open_generative_ui",
        args.title,
        local_only,
        "chart",
        chart.model_dump(mode="json"),
    )


def _plan_envelope(args: PlanToolArgs, local_only: bool) -> dict[str, Any]:
    """Build the persisted ``plan`` envelope with numbered step ids and a derived todo checklist.

    A step is ``completed`` only when the model marked it so; every other status maps to ``pending``. Raises
    ``ValueError`` (pydantic validation) when plan limits reject the data; the caller drops the widget.
    """
    steps = [PlanStep(id=f"step-{i}", **s.model_dump()) for i, s in enumerate(args.steps, 1)]
    plan = PlanPayload(
        title=args.title,
        approach=args.approach,
        technology=args.technology,
        key_elements=args.key_elements,
        steps=steps,
        source=args.source,
        todos=[
            {
                "id": s.id,
                "title": s.title,
                "description": s.description,
                "emoji": s.emoji,
                "status": "completed" if s.status == "completed" else "pending",
            }
            for s in steps
        ],
    )
    return _envelope(
        "plan", "plan", "a2ui", args.title, local_only, "plan", plan.model_dump(mode="json")
    )


def _final_citations(numbers: Sequence[int], number_map: Mapping[int, int]) -> list[int]:
    """Map model-written context numbers to the answer's final citation numbers (unmapped dropped, deduped)."""
    return list(dict.fromkeys(number_map[n] for n in numbers if n in number_map))


def _resolve_images(
    ids: Sequence[str], images: Mapping[str, Mapping[str, str]], limit: int
) -> list[dict[str, str]]:
    """Resolve model-written image ids to ``{src, alt, source_host}``; unknown ids are dropped, repeats collapse."""
    out: list[dict[str, str]] = []
    for image_id in dict.fromkeys(ids):
        known = images.get(image_id)
        if known:
            out.append(
                {"src": known["src"], "alt": known["alt"], "source_host": known["source_host"]}
            )
    return out[:limit]


def _answer_block(
    block: BaseModel, images: Mapping[str, Mapping[str, str]], number_map: Mapping[int, int]
) -> dict[str, Any] | None:
    """Persisted form of one validated ``render_answer`` block; ``None`` for a gallery with no resolvable image."""
    data = block.model_dump(mode="json")
    if isinstance(block, AnswerGalleryBlock):
        resolved = _resolve_images(block.image_ids, images, 8)
        return {"type": "gallery", "images": resolved} if resolved else None
    if isinstance(block, AnswerSectionBlock):
        data["images"] = _resolve_images(block.image_ids, images, 6)
        del data["image_ids"]
    if isinstance(block, AnswerStatsBlock):
        for item, raw in zip(block.items, data["items"], strict=True):
            raw["citations"] = _final_citations(item.citations, number_map)
    elif "citations" in data:
        data["citations"] = _final_citations(getattr(block, "citations", []), number_map)
    return data


def _answer_envelope(
    args: RenderAnswerArgs,
    images: Mapping[str, Mapping[str, str]],
    number_map: Mapping[int, int],
    local_only: bool,
) -> dict[str, Any] | None:
    """Build the persisted ``answer`` envelope: images resolved, citations renumbered, empty galleries dropped.

    ``images`` maps the turn's ``imgN`` ids to ``{src, alt, source_host}`` (src is a signed same-origin proxy
    path); ``number_map`` maps context citation numbers to the final citation numbers, exactly as the
    transcript renumbers them. Returns None when no block survives.
    """
    blocks = [b for b in (_answer_block(b, images, number_map) for b in args.blocks) if b]
    if not blocks:
        return None
    return _envelope(
        "answer",
        "rich_answer",
        "a2ui",
        args.title,
        local_only,
        "answer",
        {"title": args.title, "blocks": blocks},
    )


def finalize_generative_ui(
    tool_name: str,
    raw_args: str,
    citations: Mapping[int, Mapping[str, Any]],
    local_only: bool,
    *,
    title_hint: str = "",
    locale: str = "en-US",
    images: Mapping[str, Mapping[str, str]] | None = None,
    number_map: Mapping[int, int] | None = None,
) -> dict[str, Any] | None:
    """Validate a completed terminal tool call and build the persisted `generative_ui` payload.

    Args:
        tool_name: Terminal UI tool name.
        raw_args: Complete argument JSON from the stream.
        citations: Context citation number -> validated citation dict (see `_resolve_citations`).
        local_only: Whether the turn referenced local-only evidence.
        title_hint: Title used for sandbox widgets (their schema has no title).
        locale: Owner locale (`en-US` / `vi-VN`) for the fallback sandbox title when `title_hint` is empty.
        images: ``render_answer`` only: turn image id -> ``{src, alt, source_host}``.
        number_map: ``render_answer`` only: context citation number -> final citation number.

    Returns:
        JSON-serializable payload (the caller adds ``part_id``), or None (logged) when the arguments are
        invalid; the answer text then stands without that widget. Never raises for model-controlled input.
    """
    try:
        args: BaseModel = parse_ui_tool_args(tool_name, raw_args)
    except ValueError as exc:
        logger.warning("Dropping invalid generative UI call %s (%s)", tool_name, _error_paths(exc))
        return None
    try:
        if isinstance(args, SandboxedUiArgs):
            title = title_hint.strip()[:72] or _FALLBACK_TITLES.get(
                locale, _FALLBACK_TITLES["en-US"]
            )
            return _envelope(
                "sandboxed_ui",
                "custom_ui",
                "open_generative_ui",
                title,
                local_only,
                "sandbox",
                args.to_ordered_dict(),
            )
        if isinstance(args, TableToolArgs):
            return _table_envelope(args, citations, local_only)
        if isinstance(args, ChartToolArgs):
            return _chart_envelope(args, citations, local_only)
        if isinstance(args, PlanToolArgs):
            return _plan_envelope(args, local_only)
        if isinstance(args, RenderAnswerArgs):
            return _answer_envelope(args, images or {}, number_map or {}, local_only)
        if isinstance(args, FormPayload):
            return _envelope(
                "form", "form", "a2ui", args.title, local_only, "form", build_form_payload(args)
            )
        if isinstance(args, TimePickerToolArgs):
            picker = build_meeting_time_picker_payload(
                args.title,
                slots=args.slots,
                duration_minutes=args.duration_minutes,
                timezone=args.timezone,
                subtitle=args.subtitle,
            )
            return _envelope(
                "meeting_time_picker",
                "meeting_time_picker",
                "a2ui",
                args.title,
                local_only,
                "form",
                picker.model_dump(mode="json"),
            )
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
    prompt: str,
    answer: str,
    evidence: Sequence[Any],
    *,
    local_only: bool,
    decision: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build an evidence-derived native widget for models without tool calling.

    Only table, pie/bar chart and plan kinds are produced, and only from real evidence or answer
    numbers: when no records can be extracted nothing is rendered (never canned sample content).

    Args:
        prompt: The user's prompt (matched against the local routing rules).
        answer: Final answer text.
        evidence: Grounded evidence items.
        local_only: Whether the turn referenced local-only evidence.
        decision: Pre-computed router decision (external router); None uses the internal local rules.

    Returns:
        Persisted payload dict or None.
    """
    decision = decision or route_visualization(
        prompt, evidence_count=len(evidence), local_only=local_only
    )
    kind = decision.get("kind")
    title = " ".join(prompt.split())[:72] or "Visualization"
    if kind == "plan":
        plan = build_plan_payload(prompt, answer, evidence=evidence)
        return _envelope(
            "plan", "plan", "a2ui", plan.title, local_only, "plan", plan.model_dump(mode="json")
        )
    if kind == "table":
        table = build_table_payload(title, evidence, prompt=prompt, answer_text=answer)
        if not table.rows:
            return None
        return _envelope(
            "table",
            "table",
            "a2ui",
            table.title,
            local_only,
            "table",
            table.model_dump(mode="json"),
        )
    if kind in {"bar_chart", "pie_chart"}:
        chart = build_chart_payload(
            title,
            "pie" if kind == "pie_chart" else "bar",
            evidence,
            prompt=prompt,
            answer_text=answer,
        )
        if not chart.data:
            return None
        return _envelope(
            kind,
            chart.chart_type,
            "open_generative_ui",
            chart.title,
            local_only,
            "chart",
            chart.model_dump(mode="json"),
        )
    return None
