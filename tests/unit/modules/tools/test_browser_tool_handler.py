"""Characterization of the registered ``browser.read`` tool handler: gates, reservation, execution, bounds."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from core.heavy_work import HeavyWorkBusy
from modules.tools import browser

RUN_ID, SOURCE_ID, JOB_ID = uuid4(), uuid4(), uuid4()


class Settings:
    """Settings double exposing the secret token accessor."""

    multi_workspace_enabled = False
    browser_shared_token = SimpleNamespace(get_secret_value=lambda: "s" * 32)

    def __bool__(self) -> bool:
        """Report configured settings."""
        return True


def _context(**over: Any) -> dict[str, Any]:
    """Build a valid specialist-run context for the owner principal."""
    scope = SimpleNamespace(actor_user_id=7, user_id=7)
    principal = SimpleNamespace(is_owner=True, actor_id="owner:7", scope=scope)
    ctx: dict[str, Any] = {
        "principal": principal, "session_factory": lambda: _Session(), "run_id": str(RUN_ID), "tool_ordinal": 2,
        "claim_generation": 3, "remaining_active_seconds": 100, "settings": Settings(),
        "workflow_version": "specialist-approved-v1",
    }
    ctx.update(over)
    return ctx


class _Session:
    """Async session double."""

    async def __aenter__(self) -> _Session:  # noqa: PYI034
        """Enter."""
        return self

    async def __aexit__(self, *_a: object) -> None:
        """Leave."""


def _page() -> SimpleNamespace:
    """Build one persisted page read."""
    return SimpleNamespace(
        id=uuid4(), requested_url="https://e/a", final_url="https://e/a", observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        content_digest="d", extracted_text="hello",
    )


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch every collaborator and expose mutable behaviour knobs."""
    state: dict[str, Any] = {
        "verified": True, "source": SimpleNamespace(status="active"), "grant": SimpleNamespace(enabled=True),
        "sources": frozenset({SOURCE_ID}), "bytes": 1000, "job_status": "queued", "job_error": None,
        "result_status": "succeeded", "pages": [_page()], "read_result": "unset", "slot_error": None, "events": [],
    }

    @asynccontextmanager
    async def slot(_factory: Any, timeout_seconds: float) -> Any:
        """Record the slot timeout and optionally fail to acquire it."""
        state["events"].append(f"slot:{timeout_seconds}")
        if state["slot_error"]:
            raise state["slot_error"]
        yield SimpleNamespace(deadline=1e12)

    async def reserve(*_a: Any, **_k: Any) -> Any:
        """Return an authorization carrying the configured budget."""
        state["events"].append("reserve")
        return SimpleNamespace(
            source_ids=state["sources"], remaining_bytes=state["bytes"], remaining_active_seconds=30,
            auth_session_hash="hash",
        )

    async def submit(*_a: Any, **_k: Any) -> Any:
        """Return the durable job."""
        state["events"].append("submit")
        return SimpleNamespace(id=JOB_ID, status=state["job_status"], error_code=state["job_error"])

    async def execute(*_a: Any, **kwargs: Any) -> Any:
        """Return the executed job and pages."""
        state["events"].append("execute")
        state["deadline"] = kwargs["deadline"]
        return SimpleNamespace(job=SimpleNamespace(id=JOB_ID, status=state["result_status"], error_code="boom"), pages=state["pages"])

    async def read_result(*_a: Any, **_k: Any) -> Any:
        """Return the replayed result or None."""
        state["events"].append("read_result")
        if state["read_result"] == "unset":
            return SimpleNamespace(job=SimpleNamespace(id=JOB_ID, status="succeeded", error_code=None), pages=state["pages"])
        return state["read_result"]

    monkeypatch.setattr("modules.tools.browser_public.browser_capability_verified", lambda: state["verified"])
    monkeypatch.setattr("modules.tools.browser_public.execute_browser_read", execute)
    monkeypatch.setattr("modules.tools.browser_public.read_browser_result", read_result)
    monkeypatch.setattr("modules.tools.browser_public.submit_browser_read_in_uow", submit)
    monkeypatch.setattr("core.heavy_work.heavy_job_slot", slot)
    monkeypatch.setattr("modules.agents.public.reserve_browser_run_budget_in_uow", reserve)
    monkeypatch.setattr("modules.connectors.public.resolve_agent_browser_scope", AsyncMock(side_effect=lambda *a, **k: state["grant"]))
    monkeypatch.setattr("modules.sources.public.lock_source", AsyncMock(side_effect=lambda *a, **k: state["source"]))
    monkeypatch.setattr(browser.workspaces, "lock_access_fence", AsyncMock(return_value="fence"))
    monkeypatch.setattr(browser, "commit_with_replay", AsyncMock(side_effect=lambda *a, **k: state["events"].append("commit")))
    return state


async def _call(args: dict[str, Any] | None = None, **ctx: Any) -> Any:
    """Invoke the handler with default valid arguments."""
    return await browser._handle_browser_read(args or {"source_id": str(SOURCE_ID), "max_pages": 2}, _context(**ctx))


@pytest.mark.asyncio
async def test_capability_gate_and_argument_validation(seams: dict[str, Any]) -> None:
    """A closed gate refuses first; malformed arguments are invalid_arguments before authority."""
    seams["verified"] = False
    assert (await _call()).error_code == "tool_unavailable"
    seams["verified"] = True
    for bad in ({"source_id": 1, "max_pages": 1}, {"source_id": str(SOURCE_ID), "max_pages": 4},
                {"source_id": str(SOURCE_ID), "max_pages": True}, {"source_id": "nope", "max_pages": 1}):
        assert (await _call(bad, principal=None)).error_code == "invalid_arguments"


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [
    {"principal": None}, {"session_factory": None}, {"tool_ordinal": "2"}, {"claim_generation": None},
    {"remaining_active_seconds": "9"}, {"workflow_version": "x"}, {"run_id": 5},
    {"principal": SimpleNamespace(is_owner=True, actor_id="owner:9", scope=SimpleNamespace(actor_user_id=7, user_id=7))},
])
async def test_authority_gaps_are_forbidden(seams: dict[str, Any], override: dict[str, Any]) -> None:
    """Missing or mismatched run authority is a uniform forbidden denial with no reservation."""
    result = await _call(**override)
    assert (result.success, result.error_code) == (False, "forbidden") and "reserve" not in seams["events"]


@pytest.mark.asyncio
async def test_invalid_run_id_and_exhausted_budget(seams: dict[str, Any]) -> None:
    """A malformed run UUID is forbidden; no remaining active time is tool_unavailable."""
    assert (await _call(run_id="x")).error_code == "forbidden"
    assert (await _call(remaining_active_seconds=0)).error_code == "tool_unavailable"


@pytest.mark.asyncio
async def test_success_reserves_commits_then_executes(seams: dict[str, Any]) -> None:
    """The reservation commits before execution; the slot timeout is capped at 45 seconds."""
    result = await _call()
    assert result.success is True and result.data["job_id"] == str(JOB_ID)
    assert result.data["pages"][0]["text"] == "hello" and result.evidence_refs == (result.data["pages"][0]["evidence_id"],)
    assert seams["events"] == ["slot:45.0", "reserve", "submit", "commit", "execute"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("knob", "value", "message"), [
    ("source", None, "Browser source is not enabled"), ("source", SimpleNamespace(status="disabled"), "Browser source is not enabled"),
    ("grant", None, "Browser source is not enabled"), ("grant", SimpleNamespace(enabled=False), "Browser source is not enabled"),
    ("sources", frozenset(), "Browser source is outside the saved profile scope"),
])
async def test_reservation_denials(seams: dict[str, Any], knob: str, value: Any, message: str) -> None:
    """Disabled sources/grants and sources outside the profile scope are forbidden without a job."""
    seams[knob] = value
    result = await _call()
    assert (result.error, result.error_code) == (message, "forbidden") and "submit" not in seams["events"]


@pytest.mark.asyncio
async def test_byte_budget_exhausted(seams: dict[str, Any]) -> None:
    """A spent byte budget stops before the job insert."""
    seams["bytes"] = 0
    result = await _call()
    assert (result.error_code, "submit" in seams["events"]) == ("tool_unavailable", False)


@pytest.mark.asyncio
async def test_existing_job_states(seams: dict[str, Any]) -> None:
    """A succeeded job is replayed via the result reader, others are refused with their code."""
    seams["job_status"] = "succeeded"
    assert (await _call()).success is True and "execute" not in seams["events"]
    seams["read_result"] = None
    assert (await _call()).error_code == "authority_revoked"
    seams["job_status"], seams["job_error"] = "failed", "browser_denied"
    assert (await _call()).error_code == "browser_denied"
    seams["job_error"] = None
    assert (await _call()).error_code == "tool_unavailable"


@pytest.mark.asyncio
async def test_failed_execution_and_output_bound(seams: dict[str, Any]) -> None:
    """A failed job reports its code; an oversized page set is invalid_result."""
    seams["result_status"] = "failed"
    assert (await _call()).error_code == "boom"
    seams["result_status"] = "succeeded"
    seams["pages"] = [SimpleNamespace(**{**vars(_page()), "extracted_text": "x" * 70_000})]
    assert (await _call()).error_code == "invalid_result"


@pytest.mark.asyncio
async def test_error_mapping(seams: dict[str, Any]) -> None:
    """Capacity, authority and unexpected errors map to their sanitized codes."""
    seams["slot_error"] = HeavyWorkBusy()
    assert (await _call()).error_code == "tool_unavailable"
    for error, code in ((PermissionError("x"), "forbidden"), (LookupError("x"), "forbidden"), (RuntimeError("x"), "tool_unavailable")):
        seams["slot_error"] = error
        assert (await _call()).error_code == code
    assert MagicMock  # keep the shared import used
