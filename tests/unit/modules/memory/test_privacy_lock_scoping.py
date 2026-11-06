"""Per-workspace privacy advisory lock: key derivation, reader/writer classification, ordering."""

from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from modules.memory import public
from tests.unit.memory_patching import patch_memory

ROOT = pathlib.Path(__file__).resolve().parents[4]
LOCK_CALLS = {"lock_export_privacy", "lock_export_privacy_in_uow", "lock_export_privacy_workspaces"}

# (file, enclosing function) -> shared reader or exclusive writer. Mirrors the table in docs/privacy.md.
READERS = {
    ("modules/agents/harness.py", "_lock_native_output_fences_for_publication"),
    ("modules/chat/_public_export.py", "read_memory_export_origin"),
    ("modules/chat/routes.py", "create_conversation"),
    ("modules/chat/routes.py", "send_message"),
    ("modules/chat/routes.py", "mutate_message"),
    ("modules/chat/routes.py", "_locked_poll"),
    ("modules/chat/routes.py", "cancel_response"),
    ("modules/chat/_routes_support.py", "_redact_cancelled_run_in_fresh_session"),
    ("modules/chat/user_input.py", "expire_stale_requests"),
    ("modules/chat/user_input.py", "await_user_input"),
    ("modules/chat/_worker_base.py", "is_history_storage_enabled"),
    ("modules/chat/_worker_base.py", "_require_privacy_fence"),
    ("modules/chat/_worker_settle.py", "_release_on_shutdown"),
    ("modules/chat/_worker_settle.py", "_mark_cancelled"),
    ("modules/chat/_worker_settle.py", "_mark_privacy_cancelled"),
    ("modules/chat/_worker_settle.py", "_mark_failed"),
    ("modules/chat/worker.py", "_fail_expired_pending"),
    ("modules/memory/_export.py", "export_page"),
    ("modules/memory/_export.py", "validate_export_fences"),
    ("modules/memory/_reads.py", "_verified_memory_read"),
    ("modules/memory/_reads.py", "_verified_candidate_read"),
    ("modules/memory/_service.py", "MemoryService.get_memories"),
    ("modules/memory/_service.py", "MemoryService.get_memory"),
    ("modules/memory/_service.py", "MemoryService.get_candidates"),
}
WRITERS = {
    ("modules/agents/public.py", "_cleanup_finalized_marker"),
    ("modules/agents/public.py", "_cleanup_unavailable_marker"),
    ("modules/agents/public.py", "_cleanup_phase_a"),
    ("modules/agents/public.py", "_cleanup_phase_b"),
    ("modules/chat/_public_conversations.py", "delete_conversation"),
    ("modules/chat/_public_cleanup.py", "purge_document_copied_evidence_page"),
    ("modules/chat/_worker_purge.py", "_purge_expired_conversations"),
    ("modules/chat/_worker_purge.py", "_purge_expired_activity_links"),
    ("modules/chat/_worker_purge.py", "_purge_expired_runs"),
    ("modules/knowledge/documents/worker.py", "_advance_raw_document_cleanup"),
    ("modules/knowledge/documents/worker.py", "_evict_memory_cache_after_commit"),
    ("modules/knowledge/documents/worker.py", "_advance_copied_cleanup"),
    ("modules/knowledge/documents/worker.py", "_recover_attempt"),
    (
        "modules/memory/_privacy_cleanup.py",
        "lock_export_privacy_workspaces",
    ),  # forwards its own shared flag
    ("modules/memory/_privacy_cleanup.py", "lock_export_privacy_in_uow"),
    ("modules/memory/_service.py", "MemoryService.create_memory"),
    ("modules/memory/_service.py", "MemoryService.update_memory"),
    ("modules/memory/_service.py", "MemoryService.forget_memory"),
    ("modules/memory/_service.py", "MemoryService.invalidate_memory"),
    ("modules/memory/_service.py", "MemoryService.supersede_memory"),
    ("modules/memory/_service.py", "MemoryService.get_privacy_config"),
    ("modules/memory/_service.py", "MemoryService._read_privacy_config_under_fence"),
    ("modules/memory/_service.py", "MemoryService.update_privacy_config"),
    ("modules/memory/_service.py", "MemoryService.accept_candidate"),
    ("modules/memory/_service.py", "MemoryService.reject_candidate"),
    ("modules/memory/_service.py", "MemoryService.purge_memories"),
    ("modules/sources/worker.py", "_lock_operation"),
}


def _call_sites() -> list[tuple[str, str, str, str]]:
    """Return (file, function, callee, mode) for every privacy-lock call in production modules."""
    sites: list[tuple[str, str, str, str]] = []
    for path in sorted((ROOT / "modules").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "lock_export_privacy" not in source:
            continue
        rel = path.relative_to(ROOT).as_posix()

        def walk(node: ast.AST, stack: list[str], rel: str = rel) -> None:
            for child in ast.iter_child_nodes(node):
                named = isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                inner = [*stack, child.name] if named else stack  # type: ignore[attr-defined]
                if isinstance(child, ast.Call):
                    func = child.func
                    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                    if name in LOCK_CALLS:
                        shared = [k.value for k in child.keywords if k.arg == "shared"]
                        literal = (
                            bool(shared)
                            and isinstance(shared[0], ast.Constant)
                            and shared[0].value is True
                        )
                        mode = "R" if literal else ("dynamic" if shared else "W")
                        sites.append((rel, ".".join(inner), name, mode))
                walk(child, inner)

        walk(ast.parse(source), [])
    return sites


def test_every_call_site_is_classified_and_writers_never_take_the_shared_form() -> None:
    sites = _call_sites()
    assert sites, "no call sites found"
    seen_readers: set[tuple[str, str]] = set()
    seen_writers: set[tuple[str, str]] = set()
    for rel, function, _callee, mode in sites:
        key = (rel, function)
        if key in WRITERS:
            assert mode in {"W", "dynamic"}, f"writer {key} must use the exclusive form"
            seen_writers.add(key)
        else:
            assert key in READERS, f"unclassified privacy-lock call site {key}: add it to the table"
            assert mode == "R", f"reader {key} must pass shared=True"
            seen_readers.add(key)
    assert seen_readers == READERS
    assert seen_writers == WRITERS


def test_no_function_mixes_shared_then_exclusive() -> None:
    """A function taking both forms would be a lock upgrade (deadlock-prone) on the same key."""
    modes: dict[tuple[str, str], set[str]] = {}
    for rel, function, _callee, mode in _call_sites():
        modes.setdefault((rel, function), set()).add(mode)
    assert [key for key, found in modes.items() if len(found - {"dynamic"}) > 1] == []


def test_key_is_stable_per_workspace_and_distinct_across_workspaces() -> None:
    first, second = uuid4(), uuid4()
    assert public.privacy_lock_key(first) == public.privacy_lock_key(str(first))
    namespace, value = public.privacy_lock_key(first)
    assert namespace == public._PRIVACY_LOCK_NAMESPACE
    assert -(2**31) <= value < 2**31
    assert public.privacy_lock_key(first) != public.privacy_lock_key(second)
    fixed = UUID("00000000-0000-0000-0000-000000000001")
    assert public.privacy_lock_key(fixed) == public.privacy_lock_key(fixed)


class _Recorder:
    """Session stub recording advisory-lock statements and their parameters."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, dict[str, Any]]] = []
        self.info: dict[str, Any] = {}
        self.sync_session = SimpleNamespace(get_transaction=lambda: self)

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> None:
        self.statements.append((str(statement), params or {}))


async def test_shared_and_exclusive_forms_use_the_same_workspace_key() -> None:
    workspace = uuid4()
    session = _Recorder()
    await public.lock_export_privacy(session, workspace)  # type: ignore[arg-type]
    await public.lock_export_privacy(session, workspace, shared=True)  # type: ignore[arg-type]
    (exclusive_sql, exclusive), (shared_sql, shared) = session.statements
    assert "pg_advisory_xact_lock(" in exclusive_sql
    assert "pg_advisory_xact_lock_shared(" in shared_sql
    expected = dict(
        zip(("namespace", "workspace_key"), public.privacy_lock_key(workspace), strict=True)
    )
    assert exclusive == shared == expected


async def test_multi_workspace_locks_are_sorted_by_key_and_deduplicated() -> None:
    workspaces = [uuid4() for _ in range(6)]
    session = _Recorder()
    await public.lock_export_privacy_workspaces(session, [*workspaces, workspaces[0]], shared=True)  # type: ignore[arg-type]
    taken = [params["workspace_key"] for _sql, params in session.statements]
    assert taken == sorted(public.privacy_lock_key(item)[1] for item in workspaces)
    reverse = _Recorder()
    await public.lock_export_privacy_workspaces(reverse, reversed(workspaces))  # type: ignore[arg-type]
    assert [
        p["workspace_key"] for _s, p in reverse.statements
    ] == taken  # input order never matters


async def test_shared_then_exclusive_upgrade_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    workspace = uuid4()
    session = _Recorder()
    await public.lock_export_privacy(session, workspace, shared=True)  # type: ignore[arg-type]
    with caplog.at_level("WARNING"):
        await public.lock_export_privacy(session, workspace)  # type: ignore[arg-type]
    assert "privacy_lock_upgrade_shared_to_exclusive" in caplog.text


async def test_in_uow_lock_uses_the_scope_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = uuid4()
    fence = object()
    lock = AsyncMock()
    patch_memory(monkeypatch, "_admit", AsyncMock(return_value=fence))
    patch_memory(monkeypatch, "lock_export_privacy", lock)
    session = object()
    await public.lock_export_privacy_in_uow(
        session,
        scope=SimpleNamespace(workspace_id=workspace),  # type: ignore[arg-type]
        multi_workspace_enabled=False,
        access_fence=fence,  # type: ignore[arg-type]
    )
    lock.assert_awaited_once_with(session, workspace)
