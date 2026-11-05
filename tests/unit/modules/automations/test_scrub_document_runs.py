"""Characterization of ``scrub_document_runs`` classification and per-run revocation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from modules.automations import execution

DOC, SRC, VERSION, OTHER_VERSION = uuid4(), uuid4(), uuid4(), uuid4()
SCOPE = SimpleNamespace(workspace_id=uuid4(), actor_user_id=1)


class FakeSession:
    """Serve the run page first and the action rows second, counting flushes."""

    def __init__(self, runs: list[Any], actions: list[Any] | None = None) -> None:
        """Queue the result pages."""
        self.pages = [runs, actions or []]
        self.flushes = 0

    async def scalars(self, _statement: Any) -> Any:
        """Return the next queued page as a scalar result."""
        return SimpleNamespace(all=lambda: self.pages.pop(0))

    async def flush(self) -> None:
        """Count flushes."""
        self.flushes += 1


def _run(**over: Any) -> SimpleNamespace:
    """Build a recorded-document automation run matched to the cleanup scope."""
    event = uuid4()
    values: dict[str, Any] = {
        "id": uuid4(), "document_evidence_revoked": False, "document_id": DOC, "document_version_id": VERSION,
        "payload": {"source_id": str(SRC), "secret": "x"}, "trigger_event_id": str(event),
        "trigger_key": f"event:{event}", "status": "queued", "reason": None, "finished_at": None,
    }
    values.update(over)
    return SimpleNamespace(**values)


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub admission, proof lookups and payload scrubbing."""
    state: dict[str, Any] = {"proof": SimpleNamespace(document_version_id=VERSION), "elsewhere": False}
    monkeypatch.setattr(execution, "_admit", AsyncMock())
    monkeypatch.setattr(execution, "_actor", lambda _s: 1)
    monkeypatch.setattr(execution, "_scrub_document_payload", lambda payload: {"scrubbed": True})
    monkeypatch.setattr(execution, "_legacy_event_matches_cleanup", AsyncMock(side_effect=lambda *_a, **_k: state["proof"]))
    monkeypatch.setattr(execution, "_event_belongs_elsewhere", AsyncMock(side_effect=lambda *_a, **_k: state["elsewhere"]))
    return state


async def _scrub(session: FakeSession, *, final: bool = False) -> Any:
    """Invoke the hook with the shared cleanup identity."""
    return await execution.scrub_document_runs(
        session, operation_id=uuid4(), document_id=DOC, source_id=SRC, version_ids=(VERSION,),  # type: ignore[arg-type]
        final_reference_page=final, scope=SCOPE, multi_workspace_enabled=False,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_matched_run_is_revoked_and_actions_settled(seams: dict[str, Any]) -> None:
    """A proven run is scrubbed, pending actions skipped and the run dropped."""
    run = _run()
    actions = [SimpleNamespace(status="pending", error_code=None, approved_session_hash="h"),
               SimpleNamespace(status="succeeded", error_code=None, approved_session_hash="h")]
    session = FakeSession([run], actions)
    result = await _scrub(session)
    assert result.changed_count == 1 and result.complete is True
    assert run.document_evidence_revoked is True and run.payload == {"scrubbed": True}
    assert (run.status, run.reason) == ("dropped", "document_evidence_revoked") and run.finished_at is not None
    assert (actions[0].status, actions[0].error_code, actions[0].approved_session_hash) == (
        "skipped", "document_evidence_revoked", None,
    )
    assert actions[1].status == "succeeded" and session.flushes == 1


@pytest.mark.asyncio
async def test_in_flight_action_makes_run_requires_review(seams: dict[str, Any]) -> None:
    """An uncertain effect is retained for review instead of dropping the run."""
    run = _run(status="running")
    action = SimpleNamespace(status="in_flight", error_code=None, approved_session_hash=None)
    await _scrub(FakeSession([run], [action]))
    assert action.status == "requires_review" and run.status == "requires_review"


@pytest.mark.asyncio
async def test_revoked_and_other_document_runs_are_skipped(seams: dict[str, Any]) -> None:
    """Already revoked runs and runs for another document are not touched or reported."""
    revoked, other = _run(document_evidence_revoked=True), _run(document_id=uuid4())
    result = await _scrub(FakeSession([revoked, other]))
    assert (result.provisional_unavailable_ids, result.unavailable_ids, result.changed_count) == ((), (), 0)
    assert not other.document_evidence_revoked


@pytest.mark.asyncio
async def test_contradictory_version_is_provisional_then_unavailable(seams: dict[str, Any]) -> None:
    """A run naming a version outside the cleanup is reported, not scrubbed, per page kind."""
    run = _run(document_version_id=OTHER_VERSION)
    first = await _scrub(FakeSession([run]))
    final = await _scrub(FakeSession([run]), final=True)
    assert (first.provisional_unavailable_ids, first.unavailable_ids) == ((run.id,), ())
    assert (final.provisional_unavailable_ids, final.unavailable_ids) == ((), (run.id,))
    assert run.document_evidence_revoked is False


@pytest.mark.asyncio
async def test_failed_proof_is_unmatched(seams: dict[str, Any]) -> None:
    """Without event proof the run is provisional rather than scrubbed."""
    seams["proof"] = None
    run = _run()
    result = await _scrub(FakeSession([run]))
    assert run.document_evidence_revoked is False
    assert result.provisional_unavailable_ids == (run.id,)


@pytest.mark.asyncio
async def test_legacy_run_recovered_from_event_pair(seams: dict[str, Any]) -> None:
    """A document-less legacy run is recovered through its canonical event pair and gets the proven version."""
    event = uuid4()
    run = _run(document_id=None, document_version_id=None, trigger_event_id=str(event), trigger_key=f"event:{event}")
    await _scrub(FakeSession([run]))
    assert run.document_evidence_revoked is True and run.document_id == DOC and run.document_version_id == VERSION


@pytest.mark.asyncio
async def test_legacy_run_with_mismatched_pair_is_not_proven(seams: dict[str, Any]) -> None:
    """A trigger key that disagrees with the event ID is never trusted as proof."""
    event = uuid4()
    run = _run(document_id=None, document_version_id=None, trigger_event_id=str(event), trigger_key=f"event:{uuid4()}")
    result = await _scrub(FakeSession([run]))
    assert run.document_evidence_revoked is False
    assert execution._legacy_event_matches_cleanup.await_count == 0
    assert result.provisional_unavailable_ids == (run.id,)


@pytest.mark.asyncio
async def test_legacy_event_belonging_elsewhere_is_ignored(seams: dict[str, Any]) -> None:
    """An unproven legacy run whose event belongs to another document is silently left alone."""
    seams["proof"], seams["elsewhere"] = None, True
    event = uuid4()
    run = _run(document_id=None, document_version_id=None, trigger_event_id=str(event), trigger_key=f"event:{event}")
    result = await _scrub(FakeSession([run]))
    assert run.document_evidence_revoked is False
    assert (result.provisional_unavailable_ids, result.unavailable_ids) == ((), ())


@pytest.mark.asyncio
async def test_page_bounds_validated(seams: dict[str, Any]) -> None:
    """Out-of-range limits are rejected before admission."""
    with pytest.raises(ValueError, match="page bound"):
        await execution.scrub_document_runs(
            FakeSession([]), operation_id=uuid4(), document_id=DOC, source_id=SRC, version_ids=(),  # type: ignore[arg-type]
            limit=0, scope=SCOPE, multi_workspace_enabled=False,  # type: ignore[arg-type]
        )
