"""Planning and todo list schemas, builders, and tools for Generative UI."""

import re
import uuid
from collections.abc import Sequence
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field


class Todo(TypedDict, total=False):
    """TypedDict representing an interactive todo item in agent state."""

    id: str
    title: str
    description: str
    emoji: str
    status: Literal["pending", "completed"]


class PlanStep(BaseModel):
    """Single actionable step within a structured execution or visualization plan."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=1000)
    emoji: str = Field(default="🔹", max_length=16)
    status: Literal["pending", "in_progress", "completed"] = "pending"
    source_id: str | None = None
    document_id: str | None = None
    chunk_id: str | None = None


class PlanPayload(BaseModel):
    """Structured plan and checklist widget payload."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=255)
    approach: str = Field(min_length=1, max_length=1000)
    technology: str = Field(default="Umwelt-OS Checklist", max_length=120)
    key_elements: list[str] = Field(default_factory=list, max_length=12)
    steps: list[PlanStep] = Field(default_factory=list, max_length=24)
    todos: list[dict[str, Any]] = Field(default_factory=list, max_length=24)
    source: str = Field(default="Grounded plan", max_length=255)


class _ToolDescriptor:
    """Callable tool wrapper exposing `.name`, `.description`, `.func`, and `.invoke`."""

    def __init__(self, func: Any) -> None:
        """Initialize the descriptor from a function."""
        self.func = func
        self.name = func.__name__.lstrip("_")
        self.description = (func.__doc__ or "").strip()
        self.__doc__ = func.__doc__
        self.__name__ = self.name

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Invoke the wrapped function directly."""
        return self.func(*args, **kwargs)

    def invoke(self, input_data: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        """Invoke the wrapped function with a dictionary of arguments."""
        merged = {**(input_data or {}), **kwargs}
        return self.func(**merged)


def _plan_visualization(
    approach: str, technology: str, key_elements: list[str]
) -> str:
    """Optionally explain a complex visualization approach before building it.

    Use when planning helps the user understand scope; it is not required before
    generateSandboxedUi, pieChart, or barChart.

    Args:
        approach: One sentence describing the visualization strategy.
        technology: The primary technology (e.g. "inline SVG", "Chart.js",
            "HTML + Canvas", "Three.js", "Mermaid", "D3.js").
        key_elements: 2-4 concise bullet points describing what will be built.
    """
    elements = "\n".join(f"  - {e}" for e in key_elements)
    return f"Plan: {approach}\nTech: {technology}\n{elements}"


def _manage_todos(todos: list[Todo], runtime: Any = None) -> dict[str, Any]:
    """Manage and persist the current todos list in agent state."""
    normalized: list[Todo] = []
    for item in todos:
        copy_item: Todo = dict(item)  # type: ignore[assignment]
        if not copy_item.get("id"):
            copy_item["id"] = str(uuid.uuid4())
        if "status" not in copy_item:
            copy_item["status"] = "pending"
        if "emoji" not in copy_item:
            copy_item["emoji"] = "✅" if copy_item["status"] == "completed" else "🔹"
        normalized.append(copy_item)
    if runtime is not None and isinstance(getattr(runtime, "state", None), dict):
        runtime.state["todos"] = normalized
    return {"todos": normalized, "message": "Successfully updated todos"}


def _get_todos(runtime: Any = None) -> list[Todo]:
    """Get the current todos from agent state."""
    if runtime is not None and isinstance(getattr(runtime, "state", None), dict):
        return list(runtime.state.get("todos", []))
    return []


plan_visualization = _ToolDescriptor(_plan_visualization)
manage_todos = _ToolDescriptor(_manage_todos)
get_todos = _ToolDescriptor(_get_todos)
todo_tools = [manage_todos, get_todos]

_BULLET_LINE_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.+)$")


def _extract_steps_from_text(text: str) -> list[str]:
    """Extract bulleted or numbered lines from markdown/prose text."""
    extracted: list[str] = []
    for raw_line in text.splitlines():
        match = _BULLET_LINE_RE.match(raw_line)
        if match:
            cleaned = re.sub(r"\*\*([^*]+)\*\*", r"\1", match.group(1)).strip()
            if cleaned:
                extracted.append(cleaned[:280])
    if not extracted:
        sentences = [
            s.strip()
            for s in re.split(r"(?<=[.!?])\s+", text.strip())
            if len(s.strip()) > 10
        ]
        extracted = [s[:280] for s in sentences[:5]]
    return extracted[:10]


def build_plan_payload(
    prompt: str,
    answer_text: str = "",
    *,
    steps: Sequence[dict[str, Any] | PlanStep] | None = None,
    approach: str | None = None,
    technology: str = "Umwelt-OS Checklist",
    evidence: Sequence[Any] = (),
) -> PlanPayload:
    """Build a validated `PlanPayload` from explicit steps or grounded answer text.

    Args:
        prompt: The user's request text.
        answer_text: The assistant's grounded response text.
        steps: Optional explicit step dicts or `PlanStep` instances.
        approach: Optional summary sentence describing the strategy.
        technology: Technology or workflow label.
        evidence: Optional grounded evidence items to attach provenance IDs.

    Returns:
        Validated `PlanPayload` instance.
    """
    first_source_id: str | None = None
    first_doc_id: str | None = None
    first_chunk_id: str | None = None
    if evidence:
        item = evidence[0]
        first_source_id = str(
            item.get("source_id") if isinstance(item, dict) else getattr(item, "source_id", "")
        ) or None
        first_doc_id = str(
            item.get("document_id") if isinstance(item, dict) else getattr(item, "document_id", "")
        ) or None
        first_chunk_id = str(
            item.get("chunk_id") if isinstance(item, dict) else getattr(item, "chunk_id", "")
        ) or None

    resolved_steps: list[PlanStep] = []
    if steps:
        for idx, raw_step in enumerate(steps, 1):
            if isinstance(raw_step, PlanStep):
                resolved_steps.append(raw_step)
            else:
                resolved_steps.append(
                    PlanStep(
                        id=str(raw_step.get("id") or f"step-{idx}"),
                        title=str(raw_step.get("title") or f"Step {idx}"),
                        description=str(raw_step.get("description") or ""),
                        emoji=str(raw_step.get("emoji") or "🔹"),
                        status=raw_step.get("status", "pending"),
                        source_id=raw_step.get("source_id") or first_source_id,
                        document_id=raw_step.get("document_id") or first_doc_id,
                        chunk_id=raw_step.get("chunk_id") or first_chunk_id,
                    )
                )
    else:
        extracted = _extract_steps_from_text(answer_text or prompt)
        if not extracted:
            extracted = [
                "Review scope and constraints",
                "Execute primary workflow steps",
                "Verify outcomes and citations",
            ]
        for idx, line in enumerate(extracted, 1):
            resolved_steps.append(
                PlanStep(
                    id=f"step-{idx}",
                    title=line,
                    description=f"Step {idx} of the execution plan",
                    emoji="✅" if idx == 1 else "🔹",
                    status="in_progress" if idx == 1 else "pending",
                    source_id=first_source_id,
                    document_id=first_doc_id,
                    chunk_id=first_chunk_id,
                )
            )

    todos = [
        {
            "id": step.id,
            "title": step.title,
            "description": step.description,
            "emoji": step.emoji,
            "status": "completed" if step.status == "completed" else "pending",
        }
        for step in resolved_steps
    ]
    resolved_approach = (
        approach
        or (answer_text.strip().splitlines()[0][:240] if answer_text.strip() else "")
        or f"Structured plan for: {prompt.strip()[:180] or 'requested task'}"
    )
    return PlanPayload(
        title=(prompt.strip()[:80] or "Execution Plan"),
        approach=resolved_approach,
        technology=technology,
        key_elements=[s.title for s in resolved_steps[:4]],
        steps=resolved_steps,
        todos=todos,
        source="Grounded evidence" if evidence else "Assistant execution plan",
    )
