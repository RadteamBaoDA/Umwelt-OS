"""Read-only bundled skill backend with path-traversal protection and search."""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field

SKILL_SOURCES: list[str] = ["/skills/"]
SKILL_NAMES: tuple[str, ...] = (
    "master-agent-playbook",
    "svg-diagram-skill",
    "agent-skills-vol2",
)
SKILL_ALIASES: dict[str, str] = {
    "master-playbook": "master-agent-playbook",
    "svg-diagrams": "svg-diagram-skill",
    "advanced-visualization": "agent-skills-vol2",
}
_SKILL_ROOT: Path = Path(__file__).resolve().parent / "skills"
_VALID_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class SkillMetadata(BaseModel):
    """Validated frontmatter and path metadata for a bundled generative UI skill."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=500)
    path: str = Field(min_length=1, max_length=256)
    allowed_tools: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class WriteResult:
    """Outcome of a virtual filesystem write operation."""

    error: str | None = None
    files_update: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class EditResult:
    """Outcome of a virtual filesystem edit operation."""

    error: str | None = None
    files_update: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class FileUploadResponse:
    """Outcome of a virtual filesystem upload operation."""

    path: str
    error: str | None = None


@dataclass(frozen=True)
class FileDownloadResponse:
    """Outcome of a virtual filesystem download operation."""

    path: str
    content: bytes | None = None
    error: str | None = None


def create_file_data(content: str) -> dict[str, Any]:
    """Create a virtual state-file entry compatible with agent scratch state.

    Args:
        content: Full UTF-8 text content of the file.

    Returns:
        Dictionary with line-split content and ISO timestamps.
    """
    now = datetime.now(UTC).isoformat()
    return {
        "content": content.splitlines(),
        "raw_text": content,
        "created_at": now,
        "modified_at": now,
    }


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

    canonical = SKILL_ALIASES.get(cleaned, cleaned)
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


def list_skill_metadata(skills_dir: Path | None = None) -> list[dict[str, Any]]:
    """Return validated metadata dictionaries for all bundled skills.

    Args:
        skills_dir: Optional directory override.

    Returns:
        List of serialized SkillMetadata dictionaries sorted by skill name.
    """
    items: list[dict[str, Any]] = []
    for name in list_skills(skills_dir=skills_dir):
        text = load_skill(name, skills_dir=skills_dir)
        meta = validate_skill_document(name, text, virtual_path=f"/skills/{name}/SKILL.md")
        items.append(meta.model_dump())
    return items


def search_skills(query: str, skills_dir: Path | None = None) -> list[dict[str, Any]]:
    """Search bundled skill documents by keyword across name, description, and body.

    Args:
        query: Search term or phrase. Empty queries return all bundled skills.
        skills_dir: Optional directory override.

    Returns:
        Matching skill metadata dictionaries including a matched `snippet`.
    """
    needle = query.strip().lower()
    results: list[dict[str, Any]] = []
    for name in list_skills(skills_dir=skills_dir):
        text = load_skill(name, skills_dir=skills_dir)
        meta = validate_skill_document(name, text, virtual_path=f"/skills/{name}/SKILL.md")
        haystack = f"{meta.name}\n{meta.description}\n{text}".lower()
        if needle and needle not in haystack:
            continue
        snippet = meta.description
        if needle:
            pos = text.lower().find(needle)
            if pos >= 0:
                start = max(0, pos - 40)
                end = min(len(text), pos + len(needle) + 80)
                snippet = " ".join(text[start:end].split())
        entry = meta.model_dump()
        entry["snippet"] = snippet
        results.append(entry)
    return results


def _load_startup_skill_snapshot() -> dict[str, str]:
    """Load all bundled skills into an immutable in-memory map at module import time."""
    snapshot: dict[str, str] = {}
    for canonical in SKILL_NAMES:
        text = load_skill(canonical)
        snapshot[f"/{canonical}/SKILL.md"] = text
        snapshot[f"/skills/{canonical}/SKILL.md"] = text
    for alias, canonical in SKILL_ALIASES.items():
        text = snapshot[f"/{canonical}/SKILL.md"]
        snapshot[f"/{alias}/SKILL.md"] = text
        snapshot[f"/skills/{alias}/SKILL.md"] = text
    return snapshot


_SKILL_TEXT: dict[str, str] = _load_startup_skill_snapshot()


class StateBackend:
    """In-memory per-thread scratch file backend isolated from the host filesystem."""

    def __init__(self, runtime: Any = None) -> None:
        """Initialize the state backend with an optional runtime holding `state['files']`."""
        self._runtime = runtime
        self._local_files: dict[str, dict[str, Any]] = {}

    @property
    def files(self) -> dict[str, dict[str, Any]]:
        """Return the mutable file mapping bound to the runtime state."""
        if self._runtime is not None and isinstance(getattr(self._runtime, "state", None), dict):
            state_files = self._runtime.state.setdefault("files", {})
            if isinstance(state_files, dict):
                return state_files
        return self._local_files

    @staticmethod
    def _is_safe_virtual_path(file_path: str) -> bool:
        """Verify that a virtual path is normalized and contains no traversal segments."""
        if not file_path or not file_path.startswith("/") or "\x00" in file_path or "\\" in file_path:
            return False
        segments = [seg for seg in file_path.split("/") if seg]
        if any(seg in {".", ".."} for seg in segments):
            return False
        return not (
            segments and segments[0] in {"etc", "proc", "sys", "dev", "var", "usr", "Windows"}
        )

    def read(self, file_path: str) -> str:
        """Read a virtual file's text or return a not-found message."""
        if not self._is_safe_virtual_path(file_path):
            return f"Error: file '{file_path}' not found"
        entry = self.files.get(file_path)
        if not entry:
            return f"Error: file '{file_path}' not found"
        if isinstance(entry.get("raw_text"), str):
            return str(entry["raw_text"])
        content = entry.get("content", [])
        if isinstance(content, list):
            return "\n".join(str(line) for line in content)
        return str(content)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Download raw UTF-8 bytes for one or more virtual paths."""
        responses: list[FileDownloadResponse] = []
        for path in paths:
            if not self._is_safe_virtual_path(path) or path not in self.files:
                responses.append(FileDownloadResponse(path=path, content=None, error="file_not_found"))
                continue
            text = self.read(path)
            responses.append(FileDownloadResponse(path=path, content=text.encode("utf-8"), error=None))
        return responses

    def write(self, file_path: str, content: str) -> WriteResult:
        """Write a virtual scratch file and return its state update."""
        if not self._is_safe_virtual_path(file_path):
            return WriteResult(error="invalid_path")
        file_data = create_file_data(content)
        self.files[file_path] = file_data
        return WriteResult(error=None, files_update={file_path: file_data})

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        """Asynchronously write a virtual scratch file."""
        return self.write(file_path, content)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """Replace text inside an existing virtual scratch file."""
        if not self._is_safe_virtual_path(file_path) or file_path not in self.files:
            return EditResult(error="file_not_found")
        current = self.read(file_path)
        if old_string not in current:
            return EditResult(error="old_string_not_found")
        updated = (
            current.replace(old_string, new_string)
            if replace_all
            else current.replace(old_string, new_string, 1)
        )
        file_data = create_file_data(updated)
        self.files[file_path] = file_data
        return EditResult(error=None, files_update={file_path: file_data})

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """Asynchronously edit an existing virtual scratch file."""
        return self.edit(file_path, old_string, new_string, replace_all=replace_all)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Upload byte payloads into virtual scratch paths."""
        results: list[FileUploadResponse] = []
        for path, raw_bytes in files:
            if not self._is_safe_virtual_path(path):
                results.append(FileUploadResponse(path=path, error="permission_denied"))
                continue
            self.write(path, raw_bytes.decode("utf-8", errors="replace"))
            results.append(FileUploadResponse(path=path, error=None))
        return results

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Asynchronously upload byte payloads into virtual scratch paths."""
        return self.upload_files(files)

    def glob_info(self, pattern: str, root: str = "/") -> list[dict[str, Any]]:
        """List virtual files matching a simple glob pattern without touching the host disk."""
        if ".." in pattern or ".." in root or ".env" in pattern:
            return []
        matches: list[dict[str, Any]] = []
        for path in sorted(self.files):
            if path.startswith(root):
                matches.append({"path": path})
        return matches


class BundledSkillsBackend(StateBackend):
    """Read/search support for bundled skills in an isolated, immutable namespace."""

    def __init__(self) -> None:
        """Initialize the read-only skill store from the startup snapshot."""
        super().__init__(runtime=None)
        for path, text in _SKILL_TEXT.items():
            self._local_files[path] = create_file_data(text)

    def write(self, file_path: str, content: str) -> WriteResult:
        """Reject all writes to bundled skills."""
        return WriteResult(
            error="Bundled skills are read-only. Write scratch files outside /skills/."
        )

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """Reject all edits to bundled skills."""
        return EditResult(
            error="Bundled skills are read-only. Write scratch files outside /skills/."
        )

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Reject all file uploads targeting the skills namespace."""
        return [FileUploadResponse(path=path, error="permission_denied") for path, _ in files]


class CompositeBackend:
    """Route `/skills/` operations to the read-only BundledSkillsBackend and others to StateBackend."""

    def __init__(self, default: StateBackend, routes: dict[str, StateBackend]) -> None:
        """Initialize the composite router with a default backend and prefix routes."""
        self.default = default
        self.routes = routes

    def _target(self, file_path: str) -> StateBackend:
        """Select the backend handling `file_path`."""
        if not StateBackend._is_safe_virtual_path(file_path):
            return self.default
        for prefix, backend in self.routes.items():
            if file_path.startswith(prefix):
                return backend
        return self.default

    def read(self, file_path: str) -> str:
        """Read a file from the routed backend."""
        return self._target(file_path).read(file_path)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Download files from their respective routed backends."""
        return [self._target(path).download_files([path])[0] for path in paths]

    def write(self, file_path: str, content: str) -> WriteResult:
        """Write a file through the routed backend."""
        return self._target(file_path).write(file_path, content)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        """Asynchronously write a file through the routed backend."""
        return await self._target(file_path).awrite(file_path, content)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """Edit a file through the routed backend."""
        return self._target(file_path).edit(
            file_path, old_string, new_string, replace_all=replace_all
        )

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """Asynchronously edit a file through the routed backend."""
        return await self._target(file_path).aedit(
            file_path, old_string, new_string, replace_all=replace_all
        )

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Upload files through their respective routed backends."""
        return [self._target(path).upload_files([(path, data)])[0] for path, data in files]

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Asynchronously upload files through their respective routed backends."""
        return self.upload_files(files)

    def glob_info(self, pattern: str, root: str = "/") -> list[dict[str, Any]]:
        """Perform a safe virtual glob across the routed backend."""
        return self._target(root).glob_info(pattern, root)


def build_agent_backend(runtime: Any = None) -> CompositeBackend:
    """Route trusted skill reads separately from normal per-thread scratch state.

    Args:
        runtime: Optional runtime object carrying thread-local `state['files']`.

    Returns:
        CompositeBackend mounting `/skills/` as read-only and `/` as thread-local scratch.
    """
    return CompositeBackend(
        default=StateBackend(runtime),
        routes={"/skills/": BundledSkillsBackend()},
    )
