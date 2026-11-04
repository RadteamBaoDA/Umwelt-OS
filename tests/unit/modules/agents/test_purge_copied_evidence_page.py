"""Characterization of the Agent copied-evidence cleanup state machine (phases, lock order, cursors)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from modules.agents import public as agents_public
from modules.agents.models import AgentEvidenceCleanup

RUN_ID = uuid4()
OP_ID = uuid4()
SOURCE_ID = uuid4()
DOC_ID = uuid4()
MATCH = {"kind": "run", "source_id": str(SOURCE_ID)}


class FakeSession:
    """Serve queued ``scalar`` results and record statements, adds, flushes and DELETEs in order."""

    def __init__(self, scalars: list[Any]) -> None:
        """Queue the rows returned by successive ``scalar`` calls."""
        self.scalars = list(scalars)
        self.events: list[str] = []
        self.added: list[Any] = []

    async def scalar(self, statement: Any) -> Any:
        """Return the next row, recording whether the statement locked it."""
        text = str(statement)
        self.events.append("scalar:" + ("lock" if "FOR UPDATE" in text else "read"))
        return self.scalars.pop(0)

    def add(self, item: Any) -> None:
        """Record a new ORM object."""
        self.events.append("add")
        self.added.append(item)

    async def flush(self) -> None:
        """Record a flush."""
        self.events.append("flush")

    async def execute(self, statement: Any, params: Any = None) -> None:
        """Record checkpoint DELETE statements in order."""
        self.events.append(f"delete:{params['thread_id']}:{str(statement).split()[2]}")

    async def __aenter__(self) -> Self:
        """Unused context-manager hook."""
        return self


def _run(status: str = "queued") -> SimpleNamespace:
    """Build an AgentRun double."""
    return SimpleNamespace(
        id=RUN_ID, status=status, answer="x", evidence_revoked=False, cancel_requested=False,
        dispatch_generation=1, completed_at=None, updated_at=None, checkpoint_thread_id="thread-1",
    )


def _marker(state: str, **over: Any) -> SimpleNamespace:
    """Build a cleanup marker double bound to the test scope."""
    base = {
        "source_id": SOURCE_ID, "document_id": DOC_ID, "scope_fingerprint": "scope-fp", "state": state,
        "finalized_at": None, "matched_identity": None,
    }
    return SimpleNamespace(**{**base, **over})


def _preflight(marker_state: str = "absent", **over: Any) -> SimpleNamespace:
    """Build a detached lease preflight matching the default scenario."""
    base = {
        "operation_id": OP_ID, "agent_cursor": None, "scope_fingerprint": "page-fp", "blocked": False,
        "candidate_run_id": RUN_ID, "marker_state": marker_state, "lease_required": False,
        "lease_acquired": False,
    }
    return SimpleNamespace(**{**base, **over})


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the data-access helpers with deterministic spies shared by all scenarios."""
    state: dict[str, Any] = {"candidates": [RUN_ID], "match": None, "unresolved": False, "privacy": 0}
    monkeypatch.setattr(agents_public, "_admit_cleanup_evidence", AsyncMock())
    monkeypatch.setattr(agents_public, "_agent_cleanup_records", lambda _e: [])
    monkeypatch.setattr(agents_public, "_agent_cleanup_fingerprint", lambda _e, _r: "page-fp")
    monkeypatch.setattr(agents_public, "_agent_cleanup_scope_fingerprint", lambda _e: "scope-fp")
    monkeypatch.setattr(agents_public, "_decode_agent_cleanup_cursor", lambda *_a: (None, False))
    monkeypatch.setattr(
        agents_public, "_encode_agent_cleanup_cursor",
        lambda _e, _f, last, flag: f"cursor:{'none' if last is None else 'run'}:{flag}",
    )
    monkeypatch.setattr(agents_public, "actor", lambda _s: 1)
    monkeypatch.setattr(agents_public, "_agent_unavailable_run_count", AsyncMock(return_value=7))
    monkeypatch.setattr(
        agents_public, "_agent_cleanup_candidate_ids", AsyncMock(side_effect=lambda *_a, **_k: state["candidates"]),
    )
    monkeypatch.setattr(agents_public, "_agent_cleanup_ledgers", AsyncMock(return_value=([], [], [])))
    monkeypatch.setattr(agents_public, "_find_agent_scope_match", lambda *_a: state["match"])
    monkeypatch.setattr(agents_public, "_scrub_agent_scope_payloads", lambda *_a: state["unresolved"])

    async def privacy(_session: Any, _workspace_id: Any = None, **_kw: Any) -> None:
        """Count privacy lock acquisitions."""
        state["privacy"] += 1

    monkeypatch.setattr("modules.memory.public.lock_export_privacy", privacy)
    return state


async def _purge(session: FakeSession, preflight: SimpleNamespace, cursor: str | None = None) -> Any:
    """Invoke the public entry point with a stub scope and evidence."""
    evidence = SimpleNamespace(operation_id=OP_ID, source_id=SOURCE_ID, document_id=DOC_ID)
    scope = SimpleNamespace(workspace_id=uuid4())
    return await agents_public.purge_document_copied_evidence_page(
        session, evidence, scope=scope, multi_workspace_enabled=False, preflight=preflight,  # type: ignore[arg-type]
        cursor=cursor,
    )


@pytest.mark.asyncio
async def test_invalid_limit_and_preflight_mismatch(seams: dict[str, Any]) -> None:
    """Bad page size and mismatched preflight identities are programmer errors."""
    session = FakeSession([])
    evidence = SimpleNamespace(operation_id=OP_ID)
    with pytest.raises(ValueError, match="page size"):
        await agents_public.purge_document_copied_evidence_page(
            session, evidence, limit=0, scope=SimpleNamespace(workspace_id=uuid4()),  # type: ignore[arg-type]
            multi_workspace_enabled=False, preflight=_preflight(),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="Documents cursor"):
        await _purge(session, _preflight(agent_cursor="other"))
    with pytest.raises(ValueError, match="evidence scope"):
        await _purge(session, _preflight(scope_fingerprint="other"))


@pytest.mark.asyncio
async def test_blocked_preflight_returns_cursor_unchanged(seams: dict[str, Any]) -> None:
    """A blocked lease leaves the run at the same cursor without touching rows."""
    session = FakeSession([])
    result = await _purge(session, _preflight(blocked=True))
    assert (result.next_cursor, result.complete, result.rows_processed, result.unavailable_count) == (None, False, 0, 7)
    assert session.events == [] and seams["privacy"] == 0


@pytest.mark.asyncio
async def test_stale_candidate_is_reported(seams: dict[str, Any]) -> None:
    """A different first candidate than the preflight's marks the preflight stale."""
    seams["candidates"] = [uuid4()]
    result = await _purge(FakeSession([]), _preflight())
    assert result.preflight_stale is True and result.rows_processed == 0 and result.complete is False


@pytest.mark.asyncio
async def test_no_candidates_completes(seams: dict[str, Any]) -> None:
    """No candidate and no preflight candidate completes with no cursor."""
    seams["candidates"] = []
    result = await _purge(FakeSession([]), _preflight(candidate_run_id=None))
    assert (result.next_cursor, result.complete, result.rows_processed) == (None, True, 0)


@pytest.mark.asyncio
async def test_more_candidates_yield_cursor(seams: dict[str, Any]) -> None:
    """A second candidate makes the page incomplete with an opaque resume cursor."""
    seams["candidates"] = [RUN_ID, uuid4()]
    session = FakeSession([None, _run(), None])
    result = await _purge(session, _preflight())
    assert (result.complete, result.next_cursor, result.rows_processed) == (False, "cursor:run:True", 1)


@pytest.mark.asyncio
async def test_phase_a_unmatched_run_records_unavailable_gate(seams: dict[str, Any]) -> None:
    """No exact identity persists an unavailable marker and never mutates the run."""
    run = _run()
    session = FakeSession([None, run, None])
    result = await _purge(session, _preflight())
    assert session.events == ["scalar:read", "scalar:lock", "scalar:lock", "add", "flush"]
    assert seams["privacy"] == 1
    marker = session.added[0]
    assert isinstance(marker, AgentEvidenceCleanup) and marker.state == "unavailable" and marker.matched_identity is None
    assert (run.evidence_revoked, run.cancel_requested, run.answer, run.status) == (False, False, "x", "queued")
    assert (result.rows_processed, result.unavailable, result.complete, result.next_cursor) == (1, True, True, None)


@pytest.mark.asyncio
async def test_phase_a_matched_run_is_revoked_with_pending_marker(seams: dict[str, Any]) -> None:
    """A matching run gets a pending marker, revocation, cancellation and a lease-pending cursor."""
    seams["match"] = MATCH
    run = _run()
    session = FakeSession([None, run, None])
    result = await _purge(session, _preflight())
    marker = session.added[0]
    assert marker.state == "pending" and marker.matched_identity == MATCH
    assert marker.scope_fingerprint == "scope-fp" and marker.run_id == RUN_ID
    assert (run.evidence_revoked, run.cancel_requested, run.answer, run.dispatch_generation) == (True, True, None, 2)
    assert run.status == "cancelled" and isinstance(run.completed_at, datetime)
    assert session.events[-2:] == ["add", "flush"]
    assert (result.lease_pending, result.rows_processed, result.next_cursor) == (True, 1, "cursor:none:False")


@pytest.mark.asyncio
async def test_phase_a_missing_run_advances(seams: dict[str, Any]) -> None:
    """A vanished run is skipped and counted as processed."""
    session = FakeSession([None, None])
    result = await _purge(session, _preflight())
    assert result.rows_processed == 1 and result.unavailable is False and session.added == []


@pytest.mark.asyncio
async def test_phase_a_pending_marker_appearing_returns_cursor(seams: dict[str, Any]) -> None:
    """A pending marker created concurrently cannot be re-run in the same transaction."""
    session = FakeSession([None, _run(), _marker("pending")])
    result = await _purge(session, _preflight())
    # Preflight said "absent" but the locked re-read sees "pending": stale.
    assert result.preflight_stale is True


@pytest.mark.asyncio
async def test_marker_for_other_scope_is_unavailable(seams: dict[str, Any]) -> None:
    """A marker bound to a different source/document scope reports unavailable and stops."""
    session = FakeSession([_marker("pending", scope_fingerprint="other")])
    result = await _purge(session, _preflight("pending", lease_required=True, lease_acquired=True))
    assert (result.unavailable, result.rows_processed, result.next_cursor) == (True, 0, "cursor:none:True")


@pytest.mark.asyncio
async def test_pending_marker_without_lease_is_stale(seams: dict[str, Any]) -> None:
    """A pending marker needs an affirmative lease before any lock is taken."""
    session = FakeSession([_marker("pending")])
    result = await _purge(session, _preflight("pending", lease_required=True, lease_acquired=False))
    assert result.preflight_stale is True and session.events == ["scalar:read"] and seams["privacy"] == 0


@pytest.mark.asyncio
async def test_phase_b_erases_checkpoints_and_finalizes(seams: dict[str, Any]) -> None:
    """With the lease held, saver tables are erased in order and the marker is finalized."""
    run, marker = _run(), _marker("pending")
    session = FakeSession([marker, run, marker])
    result = await _purge(session, _preflight("pending", lease_required=True, lease_acquired=True))
    assert session.events == [
        "scalar:read", "scalar:lock", "scalar:lock",
        "delete:thread-1:checkpoint_writes", "delete:thread-1:checkpoint_blobs", "delete:thread-1:checkpoints",
    ]
    assert marker.state == "finalized" and isinstance(marker.finalized_at, datetime)
    assert (run.evidence_revoked, run.cancel_requested, run.answer, run.status) == (True, True, None, "cancelled")
    assert (result.rows_processed, result.unavailable, result.complete) == (1, False, True)


@pytest.mark.asyncio
async def test_phase_b_unresolved_payloads_mark_unavailable(seams: dict[str, Any]) -> None:
    """Unresolved identities leave the finalized receipt unavailable and flag the result."""
    seams["unresolved"] = True
    run, marker = _run("running"), _marker("pending")
    result = await _purge(FakeSession([marker, run, marker]), _preflight("pending", lease_required=True, lease_acquired=True))
    assert marker.state == "unavailable" and marker.finalized_at is not None
    assert result.unavailable is True and run.status == "running"


@pytest.mark.asyncio
async def test_finalized_marker_page_only_scrubs(seams: dict[str, Any]) -> None:
    """Later reference pages for a finalized run scrub ledgers without touching saver state."""
    seams["unresolved"] = True
    marker = _marker("finalized", finalized_at=datetime.now(UTC))
    session = FakeSession([marker, _run(), marker])
    result = await _purge(session, _preflight("finalized"))
    assert not any(event.startswith("delete") for event in session.events)
    assert marker.state == "unavailable" and result.unavailable is True and seams["privacy"] == 1


@pytest.mark.asyncio
async def test_unavailable_marker_promotes_on_match(seams: dict[str, Any]) -> None:
    """A later page that proves an exact identity promotes the receipt to pending and revokes."""
    seams["match"] = MATCH
    run, marker = _run(), _marker("unavailable")
    result = await _purge(FakeSession([marker, run, marker]), _preflight("unavailable"))
    assert marker.state == "pending" and marker.matched_identity == MATCH and run.evidence_revoked is True
    assert (result.lease_pending, result.rows_processed, result.unavailable) == (True, 1, True)


@pytest.mark.asyncio
async def test_unavailable_marker_without_match_stays(seams: dict[str, Any]) -> None:
    """Without a match the coverage gate is kept and the run is never mutated."""
    run, marker = _run(), _marker("unavailable")
    result = await _purge(FakeSession([marker, run, marker]), _preflight("unavailable"))
    assert marker.state == "unavailable" and run.evidence_revoked is False
    assert (result.rows_processed, result.unavailable, result.lease_pending) == (1, True, False)
