"""Unit tests for skill_backend discovery, read-only isolation, search, and path traversal fences."""

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from modules.chat.generative_ui.skill_backend import (
    SKILL_NAMES,
    SKILL_SOURCES,
    build_agent_backend,
    list_skill_metadata,
    list_skills,
    load_skill,
    search_skills,
)


@dataclass
class _FakeRuntime:
    """Minimal runtime test double holding per-thread virtual files."""

    state: dict[str, Any] = field(default_factory=lambda: {"messages": [], "files": {}})


def _make_runtime(files: dict[str, Any] | None = None) -> _FakeRuntime:
    """Create a fresh _FakeRuntime with optional initial virtual files."""
    return _FakeRuntime(state={"messages": [], "files": files or {}})


def test_discovers_all_bundled_skills_and_aliases() -> None:
    """Verify list_skills, list_skill_metadata, and build_agent_backend expose all 3 skills."""
    assert SKILL_SOURCES == ["/skills/"]
    names = list_skills()
    assert set(names) == set(SKILL_NAMES)
    metadata = list_skill_metadata()
    assert {m["name"] for m in metadata} == {
        "master-agent-playbook",
        "svg-diagram-skill",
        "agent-skills-vol2",
    }

    backend = build_agent_backend(_make_runtime())
    for skill_name in (
        "master-agent-playbook",
        "svg-diagram-skill",
        "agent-skills-vol2",
        "master-playbook",
        "svg-diagrams",
        "advanced-visualization",
    ):
        vpath = f"/skills/{skill_name}/SKILL.md"
        downloaded = backend.download_files([vpath])[0]
        assert downloaded.error is None
        assert downloaded.content is not None
        assert b"generateSandboxedUi" in downloaded.content
        assert "generateSandboxedUi" in backend.read(vpath)


def test_skill_namespace_rejects_mutation_and_state_shadowing() -> None:
    """Verify /skills/ rejects write/edit/upload and ignores state['files'] shadowing."""
    path = "/skills/master-agent-playbook/SKILL.md"
    runtime = _make_runtime()
    backend = build_agent_backend(runtime)
    original = backend.download_files([path])[0].content
    assert original is not None

    assert backend.write(path, "replacement").error
    assert backend.write("/skills/new/SKILL.md", "new").error
    assert backend.edit(path, "Open Generative UI", "replacement").error
    assert backend.upload_files([(path, b"replacement")])[0].error == "permission_denied"
    assert asyncio.run(backend.awrite(path, "replacement")).error
    assert asyncio.run(backend.aedit(path, "Open Generative UI", "replacement")).error
    assert (
        asyncio.run(backend.aupload_files([(path, b"replacement")]))[0].error
        == "permission_denied"
    )
    runtime.state["files"][path] = {
        "content": ["injected"],
        "created_at": "",
        "modified_at": "",
    }
    assert backend.download_files([path])[0].content == original


def test_scratch_files_remain_writable_and_thread_local() -> None:
    """Verify /scratch/ files are writable, editable, and isolated per runtime thread."""
    runtime = _make_runtime()
    backend = build_agent_backend(runtime)
    result = backend.write("/scratch/note.txt", "first")
    assert result.error is None
    runtime.state["files"].update(result.files_update)

    edited = backend.edit("/scratch/note.txt", "first", "second")
    assert edited.error is None
    runtime.state["files"].update(edited.files_update)
    assert "second" in backend.read("/scratch/note.txt")
    assert "not found" in build_agent_backend(_make_runtime()).read("/scratch/note.txt")


def test_no_host_filesystem_or_traversal_access() -> None:
    """Verify both virtual backend and load_skill block path traversal and host files."""
    backend = build_agent_backend(_make_runtime())
    for path in ["/etc/passwd", "/skills/../../.env", "/skills/../main.py"]:
        result = backend.download_files([path])[0]
        assert result.error == "file_not_found"
        assert result.content is None
    assert backend.glob_info("**/.env", "/") == []

    for bad_name in ["../secret", "..\\secret", "/etc/passwd", "master-agent-playbook/../../x", ""]:
        with pytest.raises(ValueError, match="Invalid skill"):
            load_skill(bad_name)


def test_search_skills_matches_keywords_and_returns_snippets() -> None:
    """Verify search_skills locates relevant skills by keyword and returns a snippet."""
    svg_hits = search_skills("viewBox")
    assert any(h["name"] == "svg-diagram-skill" for h in svg_hits)
    assert all("snippet" in h for h in svg_hits)

    three_hits = search_skills("OrbitControls")
    assert [h["name"] for h in three_hits] == ["agent-skills-vol2"]
