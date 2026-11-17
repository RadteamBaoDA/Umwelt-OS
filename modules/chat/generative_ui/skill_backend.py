"""Read-only loader for bundled skill documents (`read_skill` tool) with a name allowlist."""

import re
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field

SKILL_NAMES: tuple[str, ...] = (
    "master-agent-playbook",
    "svg-diagram-skill",
    "agent-skills-vol2",
)
_SKILL_ROOT: Path = Path(__file__).resolve().parent / "skills"
_VALID_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class SkillMetadata(BaseModel):
    """Validated frontmatter and path metadata for a bundled generative UI skill."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=500)
    path: str = Field(min_length=1, max_length=256)
    allowed_tools: list[str] = Field(default_factory=list)


def _resolve_skill_file(name: str, skills_dir: Path | None = None) -> tuple[str, Path]:
    """Resolve and validate a skill name within the allowed skills directory.

    Args:
        name: Skill identifier or upstream alias.
        skills_dir: Optional root directory override for testing.

    Returns:
        Tuple of canonical skill folder name and resolved SKILL.md path.

    Raises:
        ValueError: If the skill name is empty, malformed, or attempts path traversal.
        FileNotFoundError: If the skill document does not exist inside skills_dir.
    """
    cleaned = name.strip()
    if (
        not cleaned
        or "\x00" in cleaned
        or "/" in cleaned
        or "\\" in cleaned
        or ".." in cleaned
        or not _VALID_SKILL_NAME_RE.fullmatch(cleaned)
    ):
        raise ValueError(f"Invalid skill name: {name!r}")

    if cleaned not in SKILL_NAMES:
        raise ValueError(f"Invalid skill name: {name!r}")
    canonical = cleaned
    root = (skills_dir or _SKILL_ROOT).resolve()
    candidate_dir = (root / canonical).resolve()
    if not candidate_dir.is_relative_to(root) or candidate_dir == root:
        raise ValueError(f"Invalid skill path traversal for: {name!r}")

    skill_file = (candidate_dir / "SKILL.md").resolve()
    if not skill_file.is_relative_to(root):
        raise ValueError(f"Invalid skill path traversal for: {name!r}")
    if not skill_file.is_file():
        raise FileNotFoundError(f"Skill '{name}' not found in {root}")
    return canonical, skill_file


def parse_skill_frontmatter(raw_text: str) -> dict[str, Any]:
    """Extract and parse YAML frontmatter from a SKILL.md document.

    Args:
        raw_text: Complete Markdown text starting with `---` frontmatter fence.

    Returns:
        Parsed dictionary of frontmatter keys.

    Raises:
        ValueError: If the frontmatter block is missing or not a mapping.
    """
    stripped = raw_text.lstrip("\ufeff")
    if not stripped.startswith("---\n"):
        raise ValueError("Skill document must start with YAML frontmatter fence '---'")
    end_idx = stripped.find("\n---\n", 4)
    if end_idx == -1:
        raise ValueError("Skill document is missing closing YAML frontmatter fence '---'")
    yaml_block = stripped[4:end_idx]
    parsed = yaml.safe_load(yaml_block)
    if not isinstance(parsed, dict):
        raise TypeError("Skill frontmatter must parse to a key-value mapping")
    return dict(parsed)


def validate_skill_document(
    name: str,
    raw_text: str,
    *,
    virtual_path: str | None = None,
) -> SkillMetadata:
    """Validate that a skill document has required frontmatter and non-empty body.

    Args:
        name: Expected skill directory or alias name.
        raw_text: Full Markdown text of SKILL.md.
        virtual_path: Virtual mount path (defaults to `/skills/<name>/SKILL.md`).

    Returns:
        Validated SkillMetadata model.

    Raises:
        ValueError: If required frontmatter keys (`name`, `description`) or body are invalid.
        TypeError: If frontmatter or allowed-tools has an invalid type.
    """
    frontmatter = parse_skill_frontmatter(raw_text)
    fm_name = str(frontmatter.get("name", "")).strip()
    fm_desc = str(frontmatter.get("description", "")).strip()
    if not fm_name or not fm_desc:
        raise ValueError(f"Skill '{name}' must define non-empty 'name' and 'description'")
    allowed_raw = frontmatter.get("allowed-tools", [])
    if not isinstance(allowed_raw, list):
        raise TypeError(f"Skill '{name}' allowed-tools must be a list")
    return SkillMetadata(
        name=fm_name,
        description=fm_desc,
        path=virtual_path or f"/skills/{name}/SKILL.md",
        allowed_tools=[str(item) for item in allowed_raw],
    )


def list_skills(skills_dir: Path | None = None) -> list[str]:
    """List canonical bundled skill names in sorted order.

    Args:
        skills_dir: Optional directory override.

    Returns:
        Sorted list of available skill folder names containing a SKILL.md file.
    """
    root = (skills_dir or _SKILL_ROOT).resolve()
    if not root.is_dir():
        return []
    names: list[str] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if child.is_dir() and (child / "SKILL.md").is_file():
            names.append(child.name)
    return names


def load_skill(name: str, skills_dir: Path | None = None) -> str:
    """Load and validate a bundled skill document by name or alias.

    Args:
        name: Canonical skill name or supported upstream alias.
        skills_dir: Optional directory override.

    Returns:
        Full UTF-8 text of the requested SKILL.md document.

    Raises:
        ValueError: On path traversal or malformed skill identifier.
        FileNotFoundError: When the skill does not exist.
    """
    canonical, skill_file = _resolve_skill_file(name, skills_dir=skills_dir)
    text = skill_file.read_text(encoding="utf-8")
    validate_skill_document(canonical, text)
    return text
