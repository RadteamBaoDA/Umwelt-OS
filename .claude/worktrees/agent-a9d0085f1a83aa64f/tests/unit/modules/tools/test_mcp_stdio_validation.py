"""Validation stages extracted from ``stdio_client_transport`` (pure checks and fence handling)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from modules.tools import mcp_stdio as stdio
from modules.tools.mcp_transport import McpTransportError


def _profile(root: Path, **over: Any) -> stdio.StdioDeploymentProfile:
    """Build a native profile whose executable and cwd live under ``root``."""
    exe = root / "server"
    exe.write_text("#!/bin/sh\n")
    values: dict[str, Any] = {
        "profile_id": "demo",
        "profile_hash": "a" * 64,
        "reviewed_profile_hash": "a" * 64,
        "enabled": True,
        "platform": "posix",
        "executable": str(exe),
        "argv": ("--flag",),
        "cwd": str(root),
        "environment": {"LANG": "C"},
        "immutable_root": str(root),
        "artifact_sha256": {str(exe): "b" * 64},
        "operation_kind": "ordinary",
        "runtime_kind": "native",
        "entry_script": None,
    }
    values.update(over)
    return stdio.StdioDeploymentProfile(**values)


def test_profile_fields_accept_valid(tmp_path: Path) -> None:
    """A valid profile yields independent copies of argv, env and the manifest."""
    argv, env, artifacts = stdio._validate_profile_fields(_profile(tmp_path))
    assert argv == ("--flag",) and env == {"LANG": "C"} and len(artifacts) == 1


@pytest.mark.parametrize(
    "override",
    [
        {"profile_id": "bad id"},
        {"enabled": False},
        {"platform": "nt"},
        {"operation_kind": "x"},
        {"runtime_kind": "ruby"},
        {"profile_hash": "zz"},
        {"reviewed_profile_hash": "c" * 64},
        {"argv": ()},
        {"argv": ("a\x00",)},
        {"environment": {"PATH": "/bin"}},
        {"artifact_sha256": {}},
    ],
)
def test_profile_fields_reject(tmp_path: Path, override: dict[str, Any]) -> None:
    """Every violated bound surfaces as the transport's sanitized error."""
    with pytest.raises(McpTransportError):
        stdio._validate_profile_fields(_profile(tmp_path, **override))


def test_launch_paths_resolve_and_reject_unsafe(tmp_path: Path) -> None:
    """Canonical paths pass; relative, escaping, shell and evaluation-flag launches are refused."""
    profile = _profile(tmp_path)
    root, cwd, exe = stdio._resolve_launch_paths(profile, profile.argv)
    assert (root, cwd, exe) == (
        tmp_path.resolve(),
        tmp_path.resolve(),
        (tmp_path / "server").resolve(),
    )
    with pytest.raises(McpTransportError, match="absolute"):
        stdio._resolve_launch_paths(_profile(tmp_path, cwd="relative"), ("--flag",))
    with pytest.raises(McpTransportError, match="outside"):
        stdio._resolve_launch_paths(_profile(tmp_path, cwd="/"), ("--flag",))
    with pytest.raises(McpTransportError, match="evaluation"):
        stdio._resolve_launch_paths(profile, ("-c", "x"))
    with pytest.raises(McpTransportError, match="entry script"):
        stdio._resolve_launch_paths(_profile(tmp_path, entry_script="/x"), ("--flag",))
    sh = tmp_path / "sh"
    sh.write_text("")
    with pytest.raises(McpTransportError, match="launchers"):
        stdio._resolve_launch_paths(_profile(tmp_path, executable=str(sh)), ("--flag",))


def test_policy_hash_binds_inputs(tmp_path: Path) -> None:
    """The reviewed hash must equal the canonical digest of the resolved launch policy."""
    profile = _profile(tmp_path)
    argv, env, artifacts = stdio._validate_profile_fields(profile)
    root, cwd, exe = stdio._resolve_launch_paths(profile, argv)
    plan = stdio._LaunchPlan(argv, env, artifacts, root, cwd, exe)
    with pytest.raises(McpTransportError, match="hash mismatch"):
        stdio._verify_policy_hash(profile, plan)
    policy = {
        "policy": "bbd-os-mcp-stdio-v1",
        "profile_id": "demo",
        "platform": "posix",
        "enabled": True,
        "runtime_kind": "native",
        "entry_script": None,
        "executable": str(exe),
        "argv": argv,
        "cwd": str(cwd),
        "environment": env,
        "immutable_root": str(root),
        "artifact_sha256": artifacts,
        "limits": {
            "ordinary_frame": stdio._ORDINARY_FRAME_BYTES,
            "ordinary_total": stdio._ORDINARY_TOTAL_BYTES,
            "ordinary_frames": stdio._ORDINARY_FRAMES,
            "discovery_frame": stdio._DISCOVERY_FRAME_BYTES,
            "discovery_total": stdio._DISCOVERY_TOTAL_BYTES,
            "discovery_frames": stdio._DISCOVERY_FRAMES,
            "outbound_frame": stdio._OUTBOUND_FRAME_BYTES,
        },
    }
    digest = hashlib.sha256(
        json.dumps(policy, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    stdio._verify_policy_hash(
        _profile(tmp_path, profile_hash=digest, reviewed_profile_hash=digest), plan
    )


@pytest.mark.asyncio
async def test_require_fence_messages() -> None:
    """A stale fence, a failing fence and a hung fence map to the stale/failed messages."""

    async def ok() -> bool:
        """Report a current fence."""
        return True

    async def stale() -> bool:
        """Report a revoked fence."""
        return False

    async def boom() -> bool:
        """Fail while checking."""
        raise RuntimeError("secret detail")

    await stdio._require_fence(ok, "stale", "failed")
    with pytest.raises(McpTransportError, match="^stale$"):
        await stdio._require_fence(stale, "stale", "failed")
    with pytest.raises(McpTransportError, match="^failed$") as caught:
        await stdio._require_fence(boom, "stale", "failed")
    assert "secret" not in str(caught.value)


def test_killpg_probe_classifies_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A vanished group is gone, an unreadable one is uncertain, a live one is neither."""
    process = type("P", (), {"pid": 123})()

    def raising(error: BaseException) -> Any:
        """Build a killpg replacement raising ``error``."""

        def fake(_pid: int, _sig: int) -> None:
            raise error

        return fake

    monkeypatch.setattr(stdio.os, "killpg", raising(ProcessLookupError()), raising=False)
    assert stdio._killpg_probe(process) == (True, False)  # type: ignore[arg-type]
    monkeypatch.setattr(stdio.os, "killpg", raising(PermissionError()), raising=False)
    assert stdio._killpg_probe(process) == (False, True)  # type: ignore[arg-type]
    monkeypatch.setattr(stdio.os, "killpg", lambda *_a: None, raising=False)
    assert stdio._killpg_probe(process) == (False, False)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_wait_and_terminate_helpers_fail_closed() -> None:
    """Exited leaders skip the wait; a missing PID or exhausted budget makes termination uncertain."""
    exited = type("P", (), {"returncode": 0})()
    assert await stdio._wait_for_exit(exited, 1e12) is False  # type: ignore[arg-type]
    no_pid = type("P", (), {"_pid": None})()
    assert await stdio._terminate_leader_tree(no_pid, 1.0) is True  # type: ignore[arg-type]
    has_pid = type("P", (), {"_pid": 5})()
    assert await stdio._terminate_leader_tree(has_pid, 0.0) is True  # type: ignore[arg-type]


def test_decode_jsonrpc_frame_sanitizes_errors() -> None:
    """Invalid UTF-8 or malformed JSON-RPC raise a message that never echoes child bytes."""
    with pytest.raises(McpTransportError, match="invalid JSON-RPC frame") as bad_utf8:
        stdio._decode_jsonrpc_frame(b"\xff\xfe secret")
    assert "secret" not in str(bad_utf8.value)
    with pytest.raises(McpTransportError, match="invalid JSON-RPC frame"):
        stdio._decode_jsonrpc_frame(b'{"not": "jsonrpc"}')
    message = stdio._decode_jsonrpc_frame(b'{"jsonrpc":"2.0","id":1,"method":"ping"}')
    assert message is not None
