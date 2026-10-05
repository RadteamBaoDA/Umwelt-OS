"""Resolve a bounded administrator-owned MCP stdio deployment manifest."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Literal

from modules.tools.mcp_repository import McpUnavailable
from modules.tools.mcp_stdio import StdioDeploymentProfile


class StdioProfileCatalog:
    """Load immutable deployment profiles once and resolve only exact reviewed identities."""

    def __init__(self, manifest_path: str | None) -> None:
        """Read a bounded root-owned manifest at composition; an empty path leaves stdio unavailable.

        A configured but malformed, writable, noncanonical, symlinked or untrusted file prevents
        startup. The file and all parent directories must be root-owned and not writable by the
        service; `/data` and service-owned `/app` therefore cannot become launch authorities.
        """
        self._profiles: dict[str, dict[str, object]] = {}
        if manifest_path in (None, ""):
            return
        if os.name != "posix" or not isinstance(manifest_path, str):
            raise ValueError("MCP stdio manifest requires a POSIX absolute path")
        path = Path(manifest_path)
        if not path.is_absolute() or str(path) != manifest_path or path == Path("/data") or Path("/data") in path.parents:
            raise ValueError("MCP stdio manifest path must be canonical, absolute, and outside /data")
        try:
            resolved = path.resolve(strict=True)
            info = path.stat()
            if (str(resolved) != manifest_path or path.is_symlink() or not stat.S_ISREG(info.st_mode)
                    or info.st_uid != 0 or info.st_mode & 0o022 or os.access(path, os.W_OK)
                    or info.st_size > 1_048_576):
                raise ValueError("MCP stdio manifest must be a bounded root-owned immutable regular file")
            parent = path.parent
            while True:
                parent_info = parent.stat()
                if (not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != 0
                        or parent_info.st_mode & 0o022 or os.access(parent, os.W_OK)):
                    raise ValueError("MCP stdio manifest ancestry must be root-owned and immutable")
                if parent == parent.parent:
                    break
                parent = parent.parent
            with path.open("rb") as manifest_file:
                raw = manifest_file.read(1_048_577)
            if len(raw) > 1_048_576:
                raise ValueError("MCP stdio manifest exceeds its 1 MiB bound")
            data = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=lambda pairs: (
                    dict(pairs) if len(dict(pairs)) == len(pairs) else None
                ),
            )
        except (OSError, RuntimeError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError("MCP stdio manifest is unavailable or invalid") from exc
        if not isinstance(data, dict) or set(data) != {"version", "profiles"} or type(data["version"]) is not int or data["version"] != 1:
            raise ValueError("MCP stdio manifest must use version 1 and a finite profile map")
        profiles = data["profiles"]
        required = {
            "profile_hash", "enabled", "platform", "executable", "argv", "cwd",
            "environment", "immutable_root", "artifact_sha256", "runtime_kind", "entry_script",
        }
        if not isinstance(profiles, dict) or len(profiles) > 32:
            raise ValueError("MCP stdio manifest supports at most 32 profiles")
        for profile_id, profile in profiles.items():
            if (not isinstance(profile_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", profile_id) is None
                    or not isinstance(profile, dict) or set(profile) != required
                    or not isinstance(profile["profile_hash"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", profile["profile_hash"]) is None
                    or type(profile["enabled"]) is not bool or profile["platform"] != "posix"
                    or not isinstance(profile["runtime_kind"], str)
                    or profile["runtime_kind"] not in {"native", "python", "node"}
                    or not all(isinstance(profile[key], str) for key in ("executable", "cwd", "immutable_root"))
                    or not isinstance(profile["argv"], list) or not 1 <= len(profile["argv"]) <= 32
                    or any(not isinstance(value, str) or not value or len(value) > 2048 for value in profile["argv"])
                    or not isinstance(profile["environment"], dict)
                    or any(not isinstance(key, str) or not isinstance(value, str) for key, value in profile["environment"].items())
                    or not isinstance(profile["artifact_sha256"], dict)
                    or any(not isinstance(key, str) or not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None for key, value in profile["artifact_sha256"].items())
                    or (profile["entry_script"] is not None and not isinstance(profile["entry_script"], str))):
                raise ValueError("MCP stdio profile has an invalid bounded shape")
            self._profiles[profile_id] = profile

    def get_identity(self, profile_id: str | None) -> str:
        """Return only the deployment-selected lowercase hash, never launch arguments or environment."""
        profile = self._profiles.get(profile_id)
        if profile is None or profile["enabled"] is not True:
            raise McpUnavailable("MCP stdio deployment profile is unavailable")
        return str(profile["profile_hash"])

    def resolve(
        self,
        profile_id: str,
        *,
        reviewed_profile_hash: str,
        operation_kind: Literal["ordinary", "discovery"],
    ) -> StdioDeploymentProfile:
        """Copy trusted launch inputs only when the durable review identity matches exactly.

        The transport remains responsible for canonical policy hash, executable bytes, path
        ancestry, UID/mode checks, process bounds and teardown. No command material is accepted
        from a connection, UI or model, and the catalog does not reimplement policy hashing.
        """
        profile = self._profiles.get(profile_id)
        if (profile is None or profile["enabled"] is not True
                or profile["profile_hash"] != reviewed_profile_hash
                or operation_kind not in {"ordinary", "discovery"}):
            raise McpUnavailable("MCP stdio deployment profile review is unavailable")
        return StdioDeploymentProfile(
            profile_id=profile_id,
            profile_hash=str(profile["profile_hash"]),
            reviewed_profile_hash=reviewed_profile_hash,
            enabled=True,
            platform="posix",
            executable=str(profile["executable"]),
            argv=tuple(profile["argv"]),
            cwd=str(profile["cwd"]),
            environment=MappingProxyType(dict(profile["environment"])),
            immutable_root=str(profile["immutable_root"]),
            artifact_sha256=MappingProxyType(dict(profile["artifact_sha256"])),
            operation_kind=operation_kind,
            runtime_kind=profile["runtime_kind"],
            entry_script=profile["entry_script"],
        )
