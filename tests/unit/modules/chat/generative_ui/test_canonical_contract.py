"""Unit tests pinning the prompt, template, and streaming contracts to generateSandboxedUi."""

import inspect

import pytest

from modules.chat.generative_ui import (
    CANONICAL_SANDBOX_PARAM_ORDER,
    SandboxedUiArgs,
    build_generative_ui_for_turn,
    iter_generative_ui_deltas,
    templates,
)
from modules.chat.generative_ui.prompt import SYSTEM_PROMPT
from modules.chat.generative_ui.templates import apply_template
from modules.chat.generative_ui.tools.plan import plan_visualization


def test_prompt_uses_generate_sandboxed_ui_not_widget_renderer() -> None:
    """Verify SYSTEM_PROMPT references generateSandboxedUi and never legacy widgetRenderer."""
    assert "generateSandboxedUi" in SYSTEM_PROMPT
    assert "widgetRenderer" not in SYSTEM_PROMPT


def test_prompt_documents_ordered_params() -> None:
    """Verify SYSTEM_PROMPT lists the 6 streaming parameters in exact monotone order."""
    anchored = [
        "1. initialHeight",
        "2. placeholderMessages",
        "3. css",
        "4. html",
        "5. jsFunctions",
        "6. jsExpressions",
    ]
    indices: list[int] = []
    for needle in anchored:
        idx = SYSTEM_PROMPT.find(needle)
        assert idx >= 0, f"missing ordered param doc: {needle}"
        indices.append(idx)
    assert indices == sorted(indices), "param docs out of order"
    assert "parameter order" in SYSTEM_PROMPT.lower()
    assert "EXACT order" in SYSTEM_PROMPT


def test_prompt_documents_sandbox_bridge_and_restrictions() -> None:
    """Verify SYSTEM_PROMPT documents the Websandbox bridge and iframe storage restrictions."""
    assert "Websandbox.connection.remote" in SYSTEM_PROMPT
    assert "localStorage" in SYSTEM_PROMPT
    assert "cookies" in SYSTEM_PROMPT.lower()
    assert "same-origin fetch" in SYSTEM_PROMPT.lower()


def test_prompt_documents_library_imports_for_sandbox() -> None:
    """Verify SYSTEM_PROMPT documents bare module imports and importmap libraries."""
    assert "await import('three')" in SYSTEM_PROMPT
    assert '<script type="module">' in SYSTEM_PROMPT
    assert "bare" in SYSTEM_PROMPT.lower()
    assert "importmap" in SYSTEM_PROMPT.lower().replace(" ", "")


def test_prompt_forbids_top_level_await_in_js_channels() -> None:
    """Verify SYSTEM_PROMPT forbids top-level await in classic script channels."""
    normalized = " ".join(SYSTEM_PROMPT.lower().split())
    assert "top-level `await`" in normalized or "top-level await" in normalized
    assert "classic script" in normalized
    assert "inside an async function" in normalized


def test_plan_visualization_docstring_names_canonical_tool() -> None:
    """Verify plan_visualization tool docstring references generateSandboxedUi."""
    assert "generateSandboxedUi" in plan_visualization.description
    assert "widgetRenderer" not in plan_visualization.description


def test_templates_docstrings_name_canonical_tool() -> None:
    """Verify apply_template and templates module never reference widgetRenderer."""
    assert "generateSandboxedUi" in apply_template.description
    assert "widgetRenderer" not in apply_template.description
    assert "widgetRenderer" not in inspect.getsource(templates)


def test_seed_templates_use_sandbox_bridge_not_global_send_prompt() -> None:
    """Verify seed templates invoke Websandbox.connection.remote.sendPrompt."""
    invoice = next(t for t in templates.SEED_TEMPLATES if t["id"] == "seed-invoice-001")
    assert invoice["html"], "seed html must be populated"
    assert "sendPrompt('" not in invoice["html"]
    assert "Websandbox.connection.remote.sendPrompt" in invoice["html"]


class _FakeRuntime:
    """Minimal tool runtime stub for template tests."""

    def __init__(self) -> None:
        """Initialize an empty state dict."""
        self.state: dict[str, object] = {}
        self.tool_call_id = "test-call"


def test_apply_template_appends_canonical_translation_note() -> None:
    """Verify apply_template includes the canonical translation note."""
    result = apply_template.func(runtime=_FakeRuntime(), name="Invoice Card")
    assert "error" not in result
    note = result.get("usage_note", "")
    assert "css parameter" in note
    assert "jsFunctions" in note
    assert "Websandbox.connection.remote.sendPrompt" in note


def test_sandboxed_ui_args_rejects_inline_style_and_preserves_canonical_order() -> None:
    """Verify SandboxedUiArgs rejects <style> in html and emits keys in CANONICAL_SANDBOX_PARAM_ORDER."""
    with pytest.raises(ValueError, match="style"):
        SandboxedUiArgs(html="<style>.a{}</style><div>bad</div>")

    args = SandboxedUiArgs(
        initialHeight=400,
        placeholderMessages=["Loading…"],
        css=".box { color: var(--text); }",
        html='<div class="box">Ready</div>',
        jsFunctions="function init() {}",
        jsExpressions="init();",
    )
    ordered = args.to_ordered_dict()
    assert tuple(ordered.keys()) == CANONICAL_SANDBOX_PARAM_ORDER


def test_iter_generative_ui_deltas_streams_in_strict_canonical_order() -> None:
    """Verify iter_generative_ui_deltas emits 6 progressive deltas in strict parameter order."""
    payload = build_generative_ui_for_turn(
        "Build a compound interest calculator",
        "Use principal, rate, and periods to calculate compound returns.",
    )
    assert payload is not None
    assert payload["kind"] == "sandboxed_ui"
    deltas = iter_generative_ui_deltas(payload)
    assert len(deltas) == len(CANONICAL_SANDBOX_PARAM_ORDER)
    for idx, (delta, expected_param) in enumerate(
        zip(deltas, CANONICAL_SANDBOX_PARAM_ORDER, strict=True)
    ):
        assert delta["status"] == "streaming"
        assert delta["param"] == expected_param
        assert tuple(delta["sandbox"].keys()) == CANONICAL_SANDBOX_PARAM_ORDER[: idx + 1]
