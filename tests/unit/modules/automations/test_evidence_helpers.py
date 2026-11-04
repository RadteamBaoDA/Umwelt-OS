"""Run-evidence and trigger-source helpers shared by fan-out, scrub and action admission."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from modules.automations import execution

DOC, VERSION, SOURCE = uuid4(), uuid4(), uuid4()


def _trigger(event: Any = None, **over: Any) -> SimpleNamespace:
    """Build an inbox trigger for a ready-document event."""
    event = event or uuid4()
    values: dict[str, Any] = {
        "trigger_type": "new_document", "document_evidence_revoked": False, "event_key": str(event),
    }
    values.update(over)
    return SimpleNamespace(**values)


@pytest.fixture
def provenance(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Stub ingestion's ready-event provenance resolver."""
    resolver = AsyncMock(return_value=SimpleNamespace(source_id=SOURCE, document_id=DOC, document_version_id=VERSION))
    monkeypatch.setattr("modules.ingestion.public.resolve_ready_event_provenance", resolver)
    return resolver


@pytest.mark.asyncio
async def test_trigger_sources_skip_unusable_triggers(provenance: AsyncMock) -> None:
    """Other types, revoked rows and non-canonical keys never reach provenance."""
    triggers = [
        _trigger(trigger_type="webhook"), _trigger(document_evidence_revoked=True),
        _trigger(event_key="not-a-uuid"), _trigger(event_key=str(uuid4()).upper()), _trigger(),
    ]
    found = await execution._document_trigger_sources(
        SimpleNamespace(), triggers, scope=SimpleNamespace(), multi_workspace_enabled=False,  # type: ignore[arg-type]
    )
    assert found == {SOURCE} and provenance.await_count == 1


@pytest.mark.asyncio
async def test_trigger_sources_held_set_short_circuits(provenance: AsyncMock) -> None:
    """A Source outside the held set stops the scan and reports None."""
    triggers = [_trigger(), _trigger()]
    result = await execution._document_trigger_sources(
        SimpleNamespace(), triggers, scope=SimpleNamespace(), multi_workspace_enabled=False,  # type: ignore[arg-type]
        held=set(),
    )
    assert result is None and provenance.await_count == 1
    ok = await execution._document_trigger_sources(
        SimpleNamespace(), triggers, scope=SimpleNamespace(), multi_workspace_enabled=False,  # type: ignore[arg-type]
        held={SOURCE},
    )
    assert ok == {SOURCE}


def _run(**over: Any) -> SimpleNamespace:
    """Build a new-document run with a recorded document and canonical event pair."""
    event = uuid4()
    values: dict[str, Any] = {
        "trigger_type": "new_document", "document_evidence_revoked": False, "document_id": DOC,
        "document_version_id": VERSION, "trigger_event_id": str(event), "trigger_key": f"event:{event}",
        "payload": {"source_id": str(SOURCE)},
    }
    values.update(over)
    return SimpleNamespace(**values)


async def _current(run: Any, monkeypatch: pytest.MonkeyPatch, retained: bool = True) -> bool:
    """Evaluate ``_run_evidence_current`` with the retained-evidence check stubbed."""
    monkeypatch.setattr(execution, "_retained_document_evidence_current", AsyncMock(return_value=retained))
    return await execution._run_evidence_current(
        SimpleNamespace(), run, scope=SimpleNamespace(), multi_workspace_enabled=False, access_fence=SimpleNamespace(),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_non_document_and_revoked_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Other trigger types pass; revoked or half-recorded document identity fails."""
    assert await _current(_run(trigger_type="webhook"), monkeypatch) is True
    assert await _current(_run(document_evidence_revoked=True), monkeypatch) is False
    assert await _current(_run(document_version_id=None), monkeypatch) is False


@pytest.mark.asyncio
async def test_recorded_document_requires_canonical_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recorded document needs the exact event pair, then defers to retained evidence."""
    assert await _current(_run(), monkeypatch) is True
    assert await _current(_run(), monkeypatch, retained=False) is False
    assert await _current(_run(trigger_key="event:" + str(uuid4())), monkeypatch) is False
    assert await _current(_run(trigger_event_id=None), monkeypatch) is False


@pytest.mark.asyncio
async def test_legacy_run_is_proven_and_backfilled(provenance: AsyncMock, monkeypatch: pytest.MonkeyPatch) -> None:
    """A legacy run is stamped with the proven document only after provenance and evidence agree."""
    run = _run(document_id=None, document_version_id=None)
    assert await _current(run, monkeypatch) is True
    assert (run.document_id, run.document_version_id) == (DOC, VERSION)
    other = _run(document_id=None, document_version_id=None, payload={"source_id": str(uuid4())})
    assert await _current(other, monkeypatch) is False and other.document_id is None
    bad = _run(document_id=None, document_version_id=None, payload={"source_id": "nope"})
    assert await _current(bad, monkeypatch) is False
    provenance.return_value = None
    assert await _current(_run(document_id=None, document_version_id=None), monkeypatch) is False
    mismatched = _run(document_id=None, document_version_id=None, trigger_key="event:" + str(uuid4()))
    assert await _current(mismatched, monkeypatch) is False
