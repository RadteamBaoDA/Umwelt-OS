"""Unit tests pinning the prompt and sandbox argument contracts to generateSandboxedUi."""

import pytest

from modules.chat.generative_ui import CANONICAL_SANDBOX_PARAM_ORDER, SandboxedUiArgs
from modules.chat.generative_ui.prompt import SYSTEM_PROMPT


def test_prompt_uses_generate_sandboxed_ui_not_widget_renderer() -> None:
    """Verify SYSTEM_PROMPT references render_sandboxed_ui and never legacy widgetRenderer."""
    assert "render_sandboxed_ui" in SYSTEM_PROMPT
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
