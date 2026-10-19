"""Unit tests for assembler.py, end-to-end turn builders, and smoke verification."""

import pytest

from modules.chat.generative_ui import (
    assemble_document,
    build_generative_ui_for_turn,
    iter_generative_ui_deltas,
)
from modules.chat.generative_ui.tools.form import (
    build_meeting_time_picker_payload,
    generate_form,
)
from modules.chat.generative_ui.tools.plan import (
    build_plan_payload,
    manage_todos,
    plan_visualization,
)


def test_plan_visualization_is_a_tool() -> None:
    """Smoke test proving plan_visualization exposes its tool contract."""
    assert plan_visualization.name == "plan_visualization"
    rendered = plan_visualization(
        "Show 3-stage pipeline",
        "inline SVG",
        ["Stage 1: Ingest", "Stage 2: Index"],
    )
    assert "Plan: Show 3-stage pipeline" in rendered
    assert "Tech: inline SVG" in rendered


def test_assemble_document_embeds_umwelt_design_tokens_before_html() -> None:
    """Verify assemble_document injects Umwelt-OS tokens, SVG classes, form styles, and bridge JS."""
    doc = assemble_document(
        "<div>widget-body</div>",
        css=".custom-rule { color: var(--accent); }",
        js_functions="function runWidget() {}",
        js_expressions="runWidget();",
        title="Test Widget",
        theme="dark",
        local_only=True,
    )
    assert '<html class="dark">' in doc
    theme_idx = doc.find("--color-background-primary")
    umwelt_idx = doc.find("--bg: #f4f5f2")
    svg_idx = doc.find(".c-purple")
    form_idx = doc.find("button:hover")
    custom_css_idx = doc.find(".custom-rule")
    html_idx = doc.find("<div>widget-body</div>")
    bridge_idx = doc.find("window.sendPrompt")

    assert umwelt_idx >= 0
    assert theme_idx >= 0
    assert svg_idx >= 0
    assert form_idx >= 0
    assert custom_css_idx >= 0
    assert html_idx > custom_css_idx > svg_idx > theme_idx
    assert bridge_idx > html_idx


def test_assemble_document_sanitizes_external_scripts_when_local_only() -> None:
    """Verify local_only=True strips external <script src='http...'> and remote images."""
    dirty_html = (
        '<div>Safe content</div>'
        '<script src="https://esm.sh/three@0.170.0"></script>'
        '<script src="https://evil.example.com/tracker.js"></script>'
        '<img src="https://tracker.example.com/pixel.png">'
    )
    fenced_doc = assemble_document(dirty_html, local_only=True)
    assert "https://esm.sh/three" not in fenced_doc
    assert "https://evil.example.com" not in fenced_doc
    assert "https://tracker.example.com" not in fenced_doc
    assert "connect-src 'none'" in fenced_doc

    unfenced_doc = assemble_document(dirty_html, local_only=False)
    assert "https://esm.sh/three@0.170.0" in unfenced_doc
    assert "https://evil.example.com" not in unfenced_doc


@pytest.mark.parametrize(
    ("prompt", "expected_kind"),
    [
        ("Create a step-by-step plan for Q4 launch", "plan"),
        ("Compare cloud providers in a table", "table"),
        ("Show a bar chart comparing latency", "bar_chart"),
        ("Show a pie chart of portfolio allocation", "pie_chart"),
        ("Schedule a meeting time slot with the team", "meeting_time_picker"),
        ("Draw a flowchart of request authentication", "sandboxed_ui"),
    ],
)
def test_build_generative_ui_for_turn_supports_all_widget_kinds(
    prompt: str, expected_kind: str
) -> None:
    """Verify build_generative_ui_for_turn constructs completed payloads and streaming deltas."""
    ui = build_generative_ui_for_turn(
        prompt,
        "- Step 1: Prepare\n- Step 2: Execute\nCompute: $120\nStorage: $80",
        local_only=True,
    )
    assert ui is not None
    assert ui["kind"] == expected_kind
    assert ui["status"] == "completed"
    assert ui["local_only"] is True
    deltas = iter_generative_ui_deltas(ui)
    assert len(deltas) >= 2
    assert all(d["status"] == "streaming" for d in deltas)


def test_plain_text_turn_returns_none() -> None:
    """Verify plain conversational turns return None from build_generative_ui_for_turn."""
    assert build_generative_ui_for_turn("Hello, how are you?", "I am doing well.") is None


def test_plan_and_form_builders_produce_valid_structures() -> None:
    """Verify build_plan_payload, manage_todos, generate_form, and meeting picker builders."""
    plan = build_plan_payload("Migration checklist", "1. Backup DB\n2. Run alembic\n3. Verify")
    assert len(plan.steps) == 3
    todo_res = manage_todos.invoke({"todos": [{"title": "Backup DB", "description": "Full dump"}]})
    assert todo_res["todos"][0]["id"]

    picker = build_meeting_time_picker_payload("Architecture Sync", duration_minutes=45)
    assert picker.duration_minutes == 45
    assert len(picker.slots) == 3
    assert "login-form" in generate_form()


async def test_worker_run_response_generation_persists_and_streams_generative_ui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify run_response_generation persists Message.metadata_json['generative_ui'] and emits message.ui.* events."""
    import json
    from collections.abc import AsyncIterator
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from typing import Any
    from unittest.mock import AsyncMock, MagicMock
    from uuid import uuid4

    from modules.chat import worker
    from modules.chat.generative_ui import CANONICAL_SANDBOX_PARAM_ORDER as PARAM_ORDER
    from modules.chat.models import Message, StreamEvent
    from modules.chat.schemas import AnswerContext
    from tests.unit.test_b1c_regressions import _config, _evidence, _factory

    now = datetime(2026, 1, 1, tzinfo=UTC)
    added: list[Any] = []
    context = AnswerContext(
        query="Build an interactive calculator for compound interest",
        evidence=[_evidence(content="Compound interest formula [1].")],
        has_sufficient_evidence=True,
    )
    scalars = iter([
        SimpleNamespace(
            content="Build an interactive calculator for compound interest",
            revision_of_message_id=None,
            created_at=now,
            role="user",
        ),
        None,
        SimpleNamespace(expires_at=None),
    ])
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=SimpleNamespace(fetchone=lambda: (uuid4(), uuid4(), {}, False, uuid4(), 1))
    )
    session.scalar = AsyncMock(side_effect=lambda *a, **k: next(scalars, None))
    session.scalars = AsyncMock(return_value=SimpleNamespace(all=list))
    session.add = added.append
    for name in ("commit", "flush", "rollback", "close", "begin"):
        setattr(session, name, AsyncMock())
    seq = iter(range(10, 1000))
    monkeypatch.setattr(
        worker,
        "_admit_job",
        AsyncMock(return_value=(SimpleNamespace(workspace_id=uuid4()), SimpleNamespace())),
    )
    monkeypatch.setattr(worker, "_lock_live_response", AsyncMock(return_value=(True, MagicMock())))
    monkeypatch.setattr(worker, "is_run_cancelled", AsyncMock(return_value=False))
    monkeypatch.setattr(worker, "revalidate_context_fence", AsyncMock(return_value=(True, [])))
    monkeypatch.setattr(worker, "_privacy_cancel_locked", AsyncMock(return_value=0))
    monkeypatch.setattr(worker, "_next_event_seq", AsyncMock(side_effect=lambda *a, **k: next(seq)))
    monkeypatch.setattr(worker, "build_context", AsyncMock(return_value=context))
    monkeypatch.setattr(
        worker.settings_public,
        "get_ai_execution_config",
        AsyncMock(return_value=_config(aliases={})),
    )

    async def stream(**_kwargs: Any) -> AsyncIterator[str]:
        yield "data: " + json.dumps({"choices": [{"delta": {"content": "Compound interest formula [1]."}}]})
        yield "data: [DONE]"

    monkeypatch.setattr(worker, "ModelGateway", lambda **kw: SimpleNamespace(stream=stream))
    monkeypatch.setattr(worker, "_mark_failed", AsyncMock())

    await worker.run_response_generation(
        uuid4(),
        _factory(session),
        SimpleNamespace(ai_allowed_endpoint_cidrs=()),  # type: ignore[arg-type]
        MagicMock(),
    )

    persisted_msg = next(m for m in added if isinstance(m, Message))
    assert "generative_ui" in persisted_msg.metadata_json
    assert persisted_msg.metadata_json["generative_ui"]["kind"] == "sandboxed_ui"
    assert persisted_msg.metadata_json["generative_ui"]["visualization"] == "calculator"

    ui_deltas = [
        e.data for e in added if isinstance(e, StreamEvent) and e.event_type == "message.ui.delta"
    ]
    assert [d["param"] for d in ui_deltas] == list(PARAM_ORDER)

    ui_done = next(
        e for e in added if isinstance(e, StreamEvent) and e.event_type == "message.ui.done"
    )
    assert ui_done.data["status"] == "completed"

    msg_done = next(
        e for e in added if isinstance(e, StreamEvent) and e.event_type == "message.done"
    )
    assert msg_done.data["generative_ui"]["kind"] == "sandboxed_ui"

