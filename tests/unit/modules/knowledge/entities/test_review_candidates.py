"""Characterization of ``list_review_candidates`` projection, actionability fences, evidence and paging."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import modules.knowledge.documents.public as documents_public
from modules.knowledge.entities import public as ent

NOW = datetime(2026, 5, 1, tzinfo=UTC)
WS = uuid4()
VERSION = uuid4()
CHUNK = uuid4()


def _digest(snapshot: dict[str, Any]) -> str:
    """Digest of the fenced identity fields exactly as the worker computes it."""
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _entity_candidate(**over: Any) -> dict[str, Any]:
    """Entity review candidate with a valid snapshot digest."""
    snap: dict[str, Any] = {
        "kind": "entity", "candidate_id": str(uuid4()), "candidate_type": "person", "candidate_name": "Zed",
        "candidate_key": "k", "match_fingerprint": "m", "chunk_ids": [str(CHUNK)], "confidence": 0.5,
    }
    snap.update(over)
    return {**snap, "snapshot_digest": _digest(snap), "reason": "ambiguous_identity", "possible_entity_ids": [str(uuid4()), "bad"]}


def _relationship_candidate() -> dict[str, Any]:
    """Relationship review candidate with a valid snapshot digest."""
    snap: dict[str, Any] = {
        "kind": "relationship", "candidate_id": str(uuid4()), "candidate_name": "KNOWS", "relationship_type": "KNOWS",
        "source_candidate_key": "s", "target_candidate_key": "t", "chunk_id": str(CHUNK), "confidence": 0.5,
    }
    return {**snap, "snapshot_digest": _digest(snap), "reason": "relationship_endpoint_requires_review", "possible_entity_ids": []}


def _row(review: list[Any], *, status: str = "succeeded", offset: int = 0) -> tuple[Any, Any]:
    """(work, result) pair."""
    work = SimpleNamespace(id=uuid4(), document_version_id=VERSION, source_generation=1, status=status,
                           updated_at=NOW.replace(second=offset))
    return work, SimpleNamespace(id=uuid4(), review_json=review)


def _fence() -> Any:
    """Document-version fence from the documents owner."""
    return SimpleNamespace(current_source_generation=1, document_id=uuid4(), version_number=2,
                           source_id=uuid4(), source_name="Src")


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Patch admission, version fences, evidence reads and entity refs; expose the session builder."""
    state = SimpleNamespace(endpoint_rows=[], refs=[], fences={VERSION: _fence()})
    monkeypatch.setattr(ent, "_admit", AsyncMock())

    async def fences(session: Any, ids: list[Any], **_k: Any) -> dict[Any, Any]:
        return state.fences

    async def evidence(session: Any, keys: list[Any], **_k: Any) -> list[Any]:
        return [SimpleNamespace(document_id=uuid4(), document_version_id=v, version_number=2, chunk_id=c,
                                source_id=uuid4(), title="T", canonical_url=None, metadata_is_version_snapshot=True,
                                observed_at=NOW, excerpt="x") for v, c in keys]

    async def entity_refs(session: Any, ids: list[Any], **_k: Any) -> list[Any]:
        return [SimpleNamespace(requested_id=i, canonical_id=i, name="E", type="person") for i in ids]

    monkeypatch.setattr(documents_public, "review_version_fences", fences)
    monkeypatch.setattr(documents_public, "read_evidence_refs", evidence)
    monkeypatch.setattr(ent, "get_entity_refs", entity_refs)

    def make_session(rows: list[Any]) -> Any:
        async def execute(_statement: Any) -> Any:
            return SimpleNamespace(all=lambda: rows)

        async def scalars(_statement: Any) -> Any:
            return SimpleNamespace(all=lambda: state.endpoint_rows)

        return SimpleNamespace(execute=AsyncMock(side_effect=execute), scalars=AsyncMock(side_effect=scalars))

    state.session = make_session
    return state


async def _list(session: Any, **over: Any) -> Any:
    """Call ``list_review_candidates``."""
    return await ent.list_review_candidates(
        session, scope=SimpleNamespace(workspace_id=WS), multi_workspace_enabled=False, **over)


async def test_entity_candidate_is_actionable_with_evidence_and_valid_ids_only(harness: SimpleNamespace) -> None:
    """A fenced, digest-valid entity candidate is actionable, keeps only parseable entity ids and gets evidence."""
    candidate = _entity_candidate()
    page = await _list(harness.session([_row([candidate])]))
    item = page.items[0]
    assert item.actionable is True and item.snapshot_digest == candidate["snapshot_digest"]
    assert item.kind == "entity" and item.candidate_name == "Zed" and len(item.possible_entity_ids) == 1
    assert item.source_name == "Src" and item.owner_generation == 1 and item.version_number == 2
    assert [e.chunk_id for e in item.evidence] == [CHUNK] and item.evidence[0].source_name == "Src"
    assert page.next_cursor is None


async def test_tampered_digest_or_unsucceeded_work_or_missing_fence_is_not_actionable(harness: SimpleNamespace) -> None:
    """Any broken fence strips actionability and hides the digest."""
    tampered = _entity_candidate()
    tampered["candidate_name"] = "Changed"
    page = await _list(harness.session([_row([tampered]), _row([_entity_candidate()], status="failed")]))
    assert [(i.actionable, i.snapshot_digest) for i in page.items] == [(False, None), (False, None)]
    harness.fences = {}
    page = await _list(harness.session([_row([_entity_candidate()])]))
    assert page.items[0].actionable is False and page.items[0].source_name is None


async def test_decided_malformed_and_non_dict_candidates_are_skipped(harness: SimpleNamespace) -> None:
    """Terminal decision states, non-dict entries and entries without name/reason are omitted."""
    decided = {**_entity_candidate(), "decision_state": {"kind": "assigned"}}
    pending_state = {**_entity_candidate(), "decision_state": {"kind": "other"}}
    nameless = {"reason": "x"}
    page = await _list(harness.session([_row(["junk", decided, nameless, pending_state])]))
    assert len(page.items) == 1 and page.items[0].actionable is False  # undecided but carries a (non-terminal) state


async def test_limit_one_produces_cursors_inside_and_after_a_review_list(harness: SimpleNamespace) -> None:
    """The cursor points at the next candidate of the same row, or past the row end when exhausted."""
    first, second = _entity_candidate(candidate_name="A"), _entity_candidate(candidate_name="B")
    row = _row([first, second])
    page = await _list(harness.session([row]), limit=1)
    assert [i.candidate_name for i in page.items] == ["A"]
    assert ent._decode_review_cursor(page.next_cursor) == (row[0].updated_at, row[0].id, 1)
    resumed = await _list(harness.session([row]), limit=1, cursor=page.next_cursor)
    assert [i.candidate_name for i in resumed.items] == ["B"]
    # Row fully consumed while fewer than 100 rows were read: the cursor still advances past the row end.
    assert ent._decode_review_cursor(resumed.next_cursor) == (row[0].updated_at, row[0].id, 2)


async def test_more_than_hundred_rows_sets_continuation_cursor(harness: SimpleNamespace) -> None:
    """A 101st row fetched means the page ends with a cursor at row 100 even when the limit is not reached."""
    rows = [_row([], offset=0) for _ in range(101)]
    page = await _list(harness.session(rows))
    assert page.items == []
    assert ent._decode_review_cursor(page.next_cursor)[1] == rows[99][0].id


async def test_relationship_requires_two_distinct_assigned_endpoints(harness: SimpleNamespace) -> None:
    """Relationship candidates stay actionable only when both endpoint memberships resolve to different entities."""
    candidate = _relationship_candidate()
    work, result = _row([candidate])
    one, two = uuid4(), uuid4()

    def member(key: str, entity: Any) -> Any:
        return SimpleNamespace(id=uuid4(), entity_id=entity, extraction_identity=str(work.id), candidate_key=key,
                               document_version_id=VERSION, chunk_id=CHUNK)

    harness.endpoint_rows = [member("s", one), member("t", two)]
    page = await _list(harness.session([(work, result)]))
    item = page.items[0]
    assert item.actionable is True and item.relationship_type == "KNOWS"
    assert [e.state for e in (item.source_endpoint, item.target_endpoint)] == ["assigned", "assigned"]

    harness.endpoint_rows = [member("s", one), member("t", one)]
    same = (await _list(harness.session([(work, result)]))).items[0]
    assert same.actionable is False and same.snapshot_digest is None

    harness.endpoint_rows = [member("s", one), member("s", two)]
    ambiguous = (await _list(harness.session([(work, result)]))).items[0]
    assert (ambiguous.source_endpoint.state, ambiguous.target_endpoint.state) == ("ambiguous", "unassigned")
    assert ambiguous.actionable is False


async def test_limit_bounds_are_enforced(harness: SimpleNamespace) -> None:
    """Limits outside 1..100 raise the stable ValueError before any query."""
    for bad in (0, 101):
        with pytest.raises(ValueError, match="between 1 and 100"):
            await _list(harness.session([]), limit=bad)
