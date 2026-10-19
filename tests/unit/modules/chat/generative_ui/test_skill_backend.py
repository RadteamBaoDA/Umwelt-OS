"""Unit tests for the read-only skill loader: allowlist and path-traversal rejection."""

import pytest

from modules.chat.generative_ui.skill_backend import SKILL_NAMES, list_skills, load_skill


def test_lists_and_loads_every_bundled_skill() -> None:
    """Every allowlisted skill is listed and loads non-empty text."""
    assert set(list_skills()) == set(SKILL_NAMES)
    for name in SKILL_NAMES:
        assert load_skill(name).strip()


@pytest.mark.parametrize(
    "bad_name",
    ["../secret", r"..\secret", "/etc/passwd", "master-agent-playbook/../../x", "", "unknown-skill", "SVG-diagram-skill"],
)
def test_load_skill_rejects_traversal_and_unlisted_names(bad_name: str) -> None:
    """Traversal, absolute paths and names outside the allowlist raise ValueError."""
    with pytest.raises(ValueError, match="Invalid skill"):
        load_skill(bad_name)
