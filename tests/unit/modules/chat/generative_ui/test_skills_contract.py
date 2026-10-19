"""Unit tests pinning the bundled SKILL.md files to the canonical render_sandboxed_ui contract."""

import re
from pathlib import Path

import pytest

from modules.chat.generative_ui.skill_backend import (
    SKILL_NAMES,
    load_skill,
    validate_skill_document,
)

SKILLS_DIR = (
    Path(__file__).resolve().parents[5]
    / "modules"
    / "chat"
    / "generative_ui"
    / "skills"
)
SKILL_FILES = sorted(SKILLS_DIR.glob("*/SKILL.md"))
PROCEDURAL_SKILLS = ["master-agent-playbook", "agent-skills-vol2"]
STALE_INLINE_STYLE_FENCE = "```html\n<style>"


def _skill_text(name: str) -> str:
    """Load the UTF-8 contents of a bundled skill document."""
    return load_skill(name)


def test_all_three_skills_present() -> None:
    """Verify all three canonical skill folders exist in modules/chat/generative_ui/skills."""
    assert [p.parent.name for p in SKILL_FILES] == sorted(SKILL_NAMES)


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.parent.name)
def test_every_skill_has_valid_frontmatter_and_design_tokens(path: Path) -> None:
    """Verify every SKILL.md has valid frontmatter, Umwelt-OS CSS tokens, and sendPrompt docs."""
    text = path.read_text(encoding="utf-8")
    meta = validate_skill_document(path.parent.name, text)
    assert meta.name == path.parent.name
    assert meta.description
    for token in ("--bg", "--surface", "--text", "--muted", "--line", "--accent", "--danger", "--chart-1"):
        assert token in text, f"{path.parent.name} missing design token {token}"
    assert "sendPrompt" in text


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.parent.name)
def test_no_skill_references_widget_renderer(path: Path) -> None:
    """Verify no skill references the retired widgetRenderer tool."""
    assert "widgetRenderer" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.parent.name)
def test_every_skill_names_canonical_tool(path: Path) -> None:
    """Verify every skill names the canonical render_sandboxed_ui tool."""
    assert "render_sandboxed_ui" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", PROCEDURAL_SKILLS)
def test_procedural_skills_document_js_channels(name: str) -> None:
    """Verify procedural skills document both jsFunctions and jsExpressions."""
    text = _skill_text(name)
    assert "jsFunctions" in text
    assert "jsExpressions" in text


@pytest.mark.parametrize("name", PROCEDURAL_SKILLS)
def test_procedural_skills_document_css_first_reveal(name: str) -> None:
    """Verify procedural skills explain that the placeholder remains until css is complete."""
    text = _skill_text(name)
    assert "css parameter" in text
    assert "until css is complete" in text.lower()


def test_advanced_visualization_library_guidance_is_canonical() -> None:
    """Verify agent-skills-vol2 documents importmap libraries and module script rules."""
    text = _skill_text("agent-skills-vol2")
    assert "await import('three')" in text
    assert '<script type="module">' in text
    assert "bare" in text.lower()
    assert "importmap" in text.lower().replace(" ", "")
    for lib in ["three", "gsap", "d3", "chart.js"]:
        assert lib in text.lower(), f"importmap library not documented: {lib}"


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.parent.name)
def test_no_inline_style_blocks_in_html_examples(path: Path) -> None:
    """Verify no html fenced example contains an inline <style> block."""
    text = path.read_text(encoding="utf-8")
    assert STALE_INLINE_STYLE_FENCE not in text
    for fence in re.findall(r"```html\n(.*?)```", text, re.DOTALL):
        assert "<style" not in fence.lower(), (
            "inline <style> in html fence:\n" + fence[:200]
        )


@pytest.mark.parametrize("name", PROCEDURAL_SKILLS)
def test_send_prompt_examples_use_sandbox_bridge(name: str) -> None:
    """Verify sendPrompt calls take an object literal via the sandbox bridge."""
    text = _skill_text(name)
    assert "Websandbox.connection.remote.sendPrompt" in text
    assert not re.search(r"sendPrompt\s*\((?!\s*\{)", text)


@pytest.mark.parametrize("name", PROCEDURAL_SKILLS)
def test_js_fences_never_show_bare_top_level_await_imports(name: str) -> None:
    """Verify js fenced examples wrap dynamic await import(...) inside a function."""
    text = _skill_text(name)
    fences = re.findall(r"```js\n(.*?)```", text, re.DOTALL)
    assert fences, "expected js fenced examples in procedural skills"
    for fence in fences:
        if "await import(" in fence:
            assert "function" in fence, (
                "bare top-level await import in js fence:\n" + fence
            )


@pytest.mark.parametrize("name", PROCEDURAL_SKILLS)
def test_procedural_skills_warn_about_classic_script_semantics(name: str) -> None:
    """Verify procedural skills warn that jsFunctions/jsExpressions run as classic scripts."""
    normalized = " ".join(_skill_text(name).lower().split())
    assert "top-level await" in normalized or "top-level `await`" in normalized
    assert "classic script" in normalized or "classic-script" in normalized
