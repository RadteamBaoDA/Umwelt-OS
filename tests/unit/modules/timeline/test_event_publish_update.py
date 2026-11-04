"""Characterization of ``publish_extracted_events`` and ``update_event``: writes, order and drift errors."""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from core.ids import uuid7
from core.workspaces.schemas import AccessFence, WorkspaceContext
from modules.timeline import public
from modules.timeline.extraction import EventExtractionOutput, EventProposal, ParticipantProposal
from modules.timeline.models import Event, EventAudit, EventEvidence, EventParticipant
from modules.timeline.schemas import EventPatch

WS = uuid4()
SCOPE = WorkspaceContext(user_id=1, workspace_id=WS, role="owner", membership_revision=1)
FENCE = AccessFence(workspace_id=WS, user_id=1, membership_revision=1, configuration_revision=1)
KW: dict[str, Any] = {"scope": SCOPE, "multi_workspace_enabled": True}
NOW = datetime(2026, 2, 1, 9, tzinfo=UTC)


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def all(self) -> list[Any]:
        return list(self.rows)

    def one_or_none(self) -> Any:
        return self.rows[0] if self.rows else None


class _Session:
    """Scripted session: queued ``scalar``/``scalars``/``execute`` results, recording writes in order."""

    def __init__(self, scalar: list[Any] | None = None, scalars: list[list[Any]] | None = None,
                 execute: list[list[Any]] | None = None) -> None:
        self.scalar_q, self.scalars_q, self.execute_q = scalar or [], scalars or [], execute or []
        self.added: list[Any] = []
        self.executed: list[tuple[str, Any]] = []
        self.log: list[str] = []

    async def scalar(self, _stmt: Any) -> Any:
        self.log.append("scalar")
        return self.scalar_q.pop(0) if self.scalar_q else None

    async def scalars(self, _stmt: Any) -> _Result:
        self.log.append("scalars")
        return _Result(self.scalars_q.pop(0) if self.scalars_q else [])

    async def execute(self, stmt: Any, params: Any = None) -> _Result:
        self.log.append("execute")
        self.executed.append((type(stmt).__name__, params))
        return _Result(self.execute_q.pop(0) if self.execute_q else [])

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.log.append("flush")
        for obj in self.added:
            if isinstance(obj, Event):
                obj.id = obj.id or uuid7()
                obj.revision = obj.revision or 1
                obj.owner_fields = obj.owner_fields or []
            elif isinstance(obj, EventParticipant):
                obj.id = obj.id or uuid7()


# --------------------------------------------------------------------------- publish_extracted_events


def _setup_publish(*, participant: bool = True, extra_proposal: bool = False) -> SimpleNamespace:
    doc_version, chunk, entity, membership = uuid4(), uuid4(), uuid4(), uuid4()
    participants = [ParticipantProposal(membership_id=membership, role=" lead ", chunk_ids=[chunk])] if participant else []
    event = EventProposal(
        type="strike", title="Port strike", confidence=0.8, date_precision="date", occurred_date=date(2026, 1, 5),
        evidence_chunk_ids=[chunk], participants=participants,
    )
    events = [event]
    if extra_proposal:
        events.append(event.model_copy(update={"summary": "z"}))  # same candidate hash, larger JSON
    ready = SimpleNamespace(
        document_id=uuid4(), document_version_id=doc_version, source_id=uuid4(), source_generation=2, observed_at=NOW,
    )
    ref = SimpleNamespace(
        chunk_id=chunk, source_id=ready.source_id, document_id=ready.document_id, document_version_id=doc_version,
        version_number=1, observed_at=NOW, title="Doc", canonical_url="https://example.test", excerpt="x",
        metadata_is_version_snapshot=True,
    )
    membership_ref = SimpleNamespace(membership_id=membership, chunk_id=chunk, entity_id=entity)
    work = SimpleNamespace(source_generation=2, document_version_id=doc_version)
    return SimpleNamespace(
        proposals=EventExtractionOutput(events=events), ready=ready, ref=ref, chunk=chunk, entity=entity,
        membership=membership, membership_ref=membership_ref, work=work,
    )


async def _publish(s: SimpleNamespace, session: _Session, *, finish: bool = True, revision: int = 3,
                   canonical: Any = None) -> tuple[bool, SimpleNamespace]:
    probes = SimpleNamespace(
        finish=AsyncMock(return_value=finish), schedule=AsyncMock(),
        entity_refs=AsyncMock(return_value=canonical if canonical is not None else [
            SimpleNamespace(canonical_id=s.entity, revision=revision)]),
    )
    with patch.object(public.documents, "read_extraction_evidence_refs", AsyncMock(return_value=[s.ref])), \
         patch.object(public, "_read_evidence_closure", AsyncMock(return_value=[s.ref])), \
         patch.object(public.entities, "list_version_membership_refs", AsyncMock(return_value=[s.membership_ref])), \
         patch.object(public.entities, "get_entity_refs", probes.entity_refs), \
         patch.object(public, "finish_extraction_work", probes.finish), \
         patch.object(public, "_schedule_temporal_event", probes.schedule):
        result = await public.publish_extracted_events(
            session, work_id=uuid4(), lease_owner="lease", ready=s.ready, proposals=s.proposals, model="m",  # type: ignore[arg-type]
            membership_revisions={s.membership: 3}, **KW,
        )
    return result, probes


@pytest.mark.asyncio
async def test_publish_creates_event_evidence_participant_and_result() -> None:
    """A new candidate becomes a derived event with evidence, participant links and a result payload."""
    s = _setup_publish()
    participant_evidence = SimpleNamespace(id=uuid4())
    session = _Session(scalar=[s.work, None], scalars=[[], [], [participant_evidence]])
    ok, probes = await _publish(s, session)
    assert ok is True
    event = next(obj for obj in session.added if isinstance(obj, Event))
    assert (event.origin, event.type, event.title, event.source_id) == ("derived", "strike", "Port strike", s.ready.source_id)
    assert event.extraction_identity == f"{s.ready.document_version_id}:2:events-v1:events-prompt-v1"
    assert event.candidate_hash and len(event.candidate_hash) == 64
    participant = next(obj for obj in session.added if isinstance(obj, EventParticipant))
    assert (participant.entity_id, participant.role, participant.origin) == (s.entity, "lead", "derived")
    kinds = [kind for kind, _ in session.executed]
    assert kinds == ["Insert", "Insert"]
    evidence_params = session.executed[0][1]
    assert len(evidence_params) == 1 and evidence_params[0]["chunk_id"] == s.chunk
    assert evidence_params[0]["source_generation"] == 2 and evidence_params[0]["candidate_hash"] == event.candidate_hash
    assert session.executed[1][1] == [{"participant_id": participant.id, "event_evidence_id": participant_evidence.id}]
    payload = probes.finish.await_args.args[3]
    assert payload[0]["event_id"] == str(event.id) and payload[0]["refresh_conflicts"] == []
    assert payload[0]["support"]["membership_ids"] == [str(s.membership)]
    assert probes.finish.await_args.args[1:3] == (probes.finish.await_args.args[1], "lease")
    probes.schedule.assert_awaited_once()
    assert probes.schedule.await_args.args[2] == ["extracted"]
    probes.entity_refs.assert_awaited_once()


@pytest.mark.asyncio
async def test_publish_coalesces_equal_candidates_to_smallest_json() -> None:
    """Two proposals with one canonical hash publish a single event (the lexicographically smaller)."""
    s = _setup_publish(participant=False, extra_proposal=True)
    session = _Session(scalar=[s.work], scalars=[[], []])
    ok, probes = await _publish(s, session)
    assert ok is True
    assert len([obj for obj in session.added if isinstance(obj, Event)]) == 1
    published = probes.finish.await_args.args[3]
    assert len(published) == 1 and published[0]["proposal"]["summary"] == "z"  # \"z\" sorts before null in canonical JSON
    probes.entity_refs.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_refreshes_existing_event_respecting_owner_fields_and_conflicts() -> None:
    """Existing events keep owner-corrected fields; conflicting temporal refreshes are named, not applied."""
    s = _setup_publish(participant=False)
    candidate = None

    def existing_event() -> Event:
        return Event(
            id=uuid4(), workspace_id=WS, source_id=s.ready.source_id, type="strike", subtype=None, title="Old title",
            summary=None, importance_score=None, confidence=0.8, metadata_json={}, origin="derived",
            date_precision="date", started_at=None, ended_at=None, occurred_date=date(2026, 1, 9), end_date=None,
            occurrence_timezone=None, observed_at=NOW, valid_from=None, valid_to=None, revision=4,
            owner_fields=["title", "occurred_date", "evidence"], extraction_identity="x", candidate_hash=candidate,
        )

    probe = _Session(scalar=[s.work], scalars=[[], []])
    await _publish(s, probe)
    candidate = next(obj for obj in probe.added if isinstance(obj, Event)).candidate_hash
    event = existing_event()
    session = _Session(scalar=[s.work], scalars=[[event], []])
    ok, probes = await _publish(s, session)
    assert ok is True and session.added == []
    assert event.title == "Old title"  # owner field wins
    assert event.occurred_date == date(2026, 1, 9)  # owner field wins
    assert event.revision == 4  # nothing else changed
    assert session.executed == []  # "evidence" is owner-corrected: no evidence insert
    assert probes.finish.await_args.args[3][0]["refresh_conflicts"] == []

    changed = existing_event()
    changed.owner_fields = ["evidence"]
    changed.date_precision = "timed"
    changed.started_at = NOW
    changed.occurred_date = None
    session = _Session(scalar=[s.work], scalars=[[changed], []])
    await _publish(s, session)
    # Proposal is date precision; unowned refresh sets date fields and bumps the revision.
    assert changed.revision == 5 and changed.date_precision == "date" and changed.occurred_date == date(2026, 1, 5)

    conflict = existing_event()
    conflict.owner_fields = ["evidence", "date_precision"]
    conflict.date_precision = "timed"
    conflict.started_at = NOW
    conflict.occurred_date = None
    session = _Session(scalar=[s.work], scalars=[[conflict], []])
    ok, probes = await _publish(s, session)
    payload = probes.finish.await_args.args[3][0]
    # date_precision is owner-corrected as timed; the proposed occurred_date would make the shape invalid.
    assert payload["refresh_conflicts"] == ["occurred_date", "started_at"]
    assert conflict.occurred_date is None and conflict.title == "Port strike"
    assert conflict.revision == 5


@pytest.mark.asyncio
async def test_publish_skips_suppressed_and_lost_leases() -> None:
    """Suppressed candidates are skipped; a lost lease or drifted version publishes nothing."""
    s = _setup_publish(participant=False)
    probe = _Session(scalar=[s.work], scalars=[[], []])
    await _publish(s, probe)
    candidate = next(obj for obj in probe.added if isinstance(obj, Event)).candidate_hash
    session = _Session(scalar=[s.work], scalars=[[], [candidate]])
    ok, probes = await _publish(s, session)
    assert ok is True and session.added == [] and probes.finish.await_args.args[3] == []
    lost, probes = await _publish(s, _Session(scalar=[None]))
    assert lost is False
    probes.finish.assert_not_awaited()
    drifted, _ = await _publish(s, _Session(scalar=[SimpleNamespace(source_generation=9, document_version_id=s.work.document_version_id)]))
    assert drifted is False
    not_finished, probes = await _publish(s, _Session(scalar=[s.work], scalars=[[], []]), finish=False)
    assert not_finished is False
    probes.schedule.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unknown_membership", "not_subset", "entity_changed", "revision_changed"])
async def test_publish_rejects_drift_with_value_errors(case: str) -> None:
    """Unknown participants and canonical identity or revision drift abort with ``ValueError``."""
    s = _setup_publish()
    canonical = None
    revision = 3
    if case == "unknown_membership":
        s.membership_ref.membership_id = uuid4()
    elif case == "not_subset":
        s.proposals.events[0].participants[0].chunk_ids.append(uuid4())
    elif case == "entity_changed":
        canonical = [SimpleNamespace(canonical_id=uuid4(), revision=3)]
    else:
        revision = 4
    session = _Session(scalar=[s.work], scalars=[[], []])
    with pytest.raises(ValueError):
        await _publish(s, session, canonical=canonical, revision=revision)


# --------------------------------------------------------------------------- update_event


def _event(origin: str = "manual", **over: Any) -> Event:
    base: dict[str, Any] = {
        "id": uuid4(), "workspace_id": WS, "source_id": None, "type": "strike", "subtype": None,
        "title": "Port strike", "summary": None, "importance_score": None, "confidence": None,
        "metadata_json": {}, "origin": origin, "date_precision": "unknown", "started_at": None,
        "ended_at": None, "occurred_date": None, "end_date": None, "occurrence_timezone": None,
        "observed_at": None, "valid_from": None, "valid_to": None, "revision": 2, "owner_fields": [],
        "deleted_at": None,
    }
    base.update(over)
    return Event(**base)


async def _update(event: Event | None, payload: EventPatch, *, initial: Any = "same", snapshots_differ: bool = False,
                  closure_drift: bool = False, lock_none: bool = False, old_rows: list[Any] | None = None,
                  evidence_rows: list[Any] | None = None) -> tuple[Any, SimpleNamespace, _Session]:
    initial_row = SimpleNamespace(revision=2, origin=event.origin if event else "manual", deleted_at=None)
    row = SimpleNamespace(id=uuid4(), source_id=uuid4(), document_id=uuid4(), document_version_id=uuid4(), chunk_id=uuid4())
    evidence_snapshots = [[row], [row, row] if snapshots_differ else [row]]
    execute = [[initial_row] if initial == "same" else [], evidence_snapshots[0], [], evidence_snapshots[1], []]
    session = _Session(scalar=[event], scalars=[old_rows or []], execute=execute)
    closure = [SimpleNamespace(source_id=uuid4(), document_id=uuid4())]
    probes = SimpleNamespace(
        closure=AsyncMock(side_effect=[closure, [] if closure_drift else closure]),
        lock_source=AsyncMock(return_value=None if lock_none else object()), lock_docs=AsyncMock(),
        entity_refs=AsyncMock(), set_participants=AsyncMock(), evidence_rows=AsyncMock(return_value=evidence_rows or []),
        remove=AsyncMock(), schedule=AsyncMock(), commit=AsyncMock(), read=AsyncMock(return_value="READ"),
    )
    with patch.object(public, "_admit", AsyncMock(return_value=FENCE)), \
         patch.object(public, "_read_evidence_closure", probes.closure), \
         patch("modules.sources.public.lock_source", probes.lock_source), \
         patch.object(public.documents, "lock_document_ids", probes.lock_docs), \
         patch.object(public.entities, "get_entity_refs", probes.entity_refs), \
         patch.object(public, "_set_participants", probes.set_participants), \
         patch.object(public, "_evidence_rows", probes.evidence_rows), \
         patch.object(public, "_remove_unsupported_derived_participants", probes.remove), \
         patch.object(public, "_schedule_temporal_event", probes.schedule), \
         patch.object(public, "commit_with_replay", probes.commit), \
         patch.object(public, "_event_read", probes.read):
        result = await public.update_event(session, event.id if event else uuid4(), payload, **KW)  # type: ignore[arg-type]
    return result, probes, session


@pytest.mark.asyncio
async def test_update_applies_scalar_changes_and_audits() -> None:
    """A title correction bumps the revision, writes one audit row, schedules and commits with replay."""
    event = _event()
    result, probes, session = await _update(event, EventPatch(expected_revision=2, reason="typo", title="Port strike ends"))
    assert result == "READ" and event.title == "Port strike ends" and event.revision == 3
    audit = next(obj for obj in session.added if isinstance(obj, EventAudit))
    assert (audit.prior_revision, audit.resulting_revision, audit.reason, audit.actor_id) == (2, 3, "typo", 1)
    assert audit.changed_json == {"title": "Port strike ends"}
    probes.schedule.assert_awaited_once()
    assert probes.schedule.await_args.args[2] == ["title"]
    probes.commit.assert_awaited_once()
    assert probes.commit.await_args.kwargs["access_fence"] == FENCE
    probes.set_participants.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_marks_derived_fields_as_owner_corrected() -> None:
    """Derived events record each corrected field (sorted) in ``owner_fields``."""
    event = _event("derived", owner_fields=["summary"])
    await _update(event, EventPatch(expected_revision=2, reason="r", title="New", metadata=None))
    assert event.owner_fields == ["summary", "title"]
    assert event.metadata_json == {}


@pytest.mark.asyncio
async def test_update_participants_and_evidence_replace_support() -> None:
    """Participant and evidence patches run their helpers, prune derived participants and record keys."""
    event = _event("derived")
    version, chunk = uuid4(), uuid4()
    ref = SimpleNamespace(
        source_id=uuid4(), document_id=uuid4(), document_version_id=version, chunk_id=chunk, version_number=1,
        observed_at=NOW, title="T", canonical_url=None, metadata_is_version_snapshot=True,
    )
    payload = EventPatch(expected_revision=2, reason="support", participants=[], evidence=[(version, chunk)])
    result, probes, session = await _update(event, payload, evidence_rows=[ref])
    assert result == "READ"
    probes.set_participants.assert_awaited_once()
    probes.remove.assert_awaited_once()
    assert event.source_id == ref.source_id
    assert event.owner_fields == ["evidence", "participants"]
    audit = next(obj for obj in session.added if isinstance(obj, EventAudit))
    assert audit.changed_json == {"participants": [], "evidence": [f"{version}:{chunk}"]}
    assert any(isinstance(obj, EventEvidence) and obj.chunk_id == chunk for obj in session.added)
    assert [kind for kind, _ in session.executed][-1] == "Delete"


@pytest.mark.asyncio
async def test_update_missing_stale_and_noop_cases() -> None:
    """Missing/deleted events return None; stale revisions, drift and no-op patches raise ``ValueError``."""
    patch_title = EventPatch(expected_revision=2, reason="r", title="Other")
    result, _probes, _session = await _update(_event(), patch_title, initial=None)
    assert result is None
    deleted = _event(deleted_at=NOW)
    result, _probes, _session = await _update(deleted, patch_title)
    assert result is None
    with pytest.raises(ValueError, match="stale event revision"):
        await _update(_event(), EventPatch(expected_revision=1, reason="r", title="Other"))
    with pytest.raises(ValueError, match="stale event revision"):
        await _update(_event(), patch_title, snapshots_differ=True)
    with pytest.raises(ValueError, match="stale event evidence closure"):
        await _update(_event(), patch_title, closure_drift=True, old_rows=[SimpleNamespace(
            document_version_id=uuid4(), chunk_id=uuid4())])
    with pytest.raises(ValueError, match="source is unavailable"):
        await _update(_event(), patch_title, lock_none=True)
    with pytest.raises(ValueError, match="no changes"):
        await _update(_event(), EventPatch(expected_revision=2, reason="r", title="Port strike"))
    with pytest.raises(ValidationError):
        await _update(_event(), EventPatch(expected_revision=2, reason="r", date_precision="timed"))
    with pytest.raises(ValueError, match="one source"):
        two = [SimpleNamespace(source_id=uuid4()), SimpleNamespace(source_id=uuid4())]
        await _update(_event(), EventPatch(expected_revision=2, reason="r", evidence=[(uuid4(), uuid4())]), evidence_rows=two)
