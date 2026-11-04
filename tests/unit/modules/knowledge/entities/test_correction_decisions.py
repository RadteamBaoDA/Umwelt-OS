"""Characterization of ``get_document_correction_decisions`` resolution and conflict rules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from modules.knowledge.entities import public as ent

NOW = datetime(2026, 5, 1, tzinfo=UTC)
DOC, VER = uuid4(), uuid4()
FP = "f" * 64
C1, C2 = uuid4(), uuid4()


def _member(chunk: UUID, fingerprint: str | None = FP) -> Any:
    """Evidence membership stand-in."""
    return SimpleNamespace(id=uuid4(), chunk_id=chunk, match_fingerprint=fingerprint)


def _decision(decision: str, entity: UUID | None, *, membership: Any = None, fingerprint: str | None = None, age: int = 0) -> Any:
    """Correction decision stand-in."""
    return SimpleNamespace(
        id=uuid4(), decision=decision, entity_id=entity, membership_id=membership.id if membership else None,
        match_fingerprint=fingerprint if fingerprint is not None else (membership.match_fingerprint if membership else None),
        created_at=NOW + timedelta(seconds=age),
    )


def _session(memberships: list[Any], evidence: list[Any], documents: list[Any]) -> Any:
    """Session answering the three bounded reads in order (evidence read skipped without memberships)."""
    queue = [memberships] + ([evidence] if memberships else []) + [documents]

    async def scalars(_statement: Any) -> Any:
        batch = queue.pop(0)
        return SimpleNamespace(all=lambda: batch)

    return SimpleNamespace(scalars=AsyncMock(side_effect=scalars))


@pytest.fixture(autouse=True)
def _admit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip admission."""
    monkeypatch.setattr(ent, "_admit", AsyncMock())


async def _decide(session: Any, candidates: dict[str, tuple[str, set[UUID]]], **kw: Any) -> Any:
    """Call the function under test."""
    return await ent.get_document_correction_decisions(
        session, DOC, VER, candidates, scope=SimpleNamespace(workspace_id=uuid4()), multi_workspace_enabled=False, **kw)


async def test_bounds_and_empty_input() -> None:
    """Empty input short-circuits; >30 candidates or >200 chunks or empty chunk sets are rejected."""
    assert await _decide(_session([], [], []), {}) == {}
    with pytest.raises(ValueError, match="candidate limit"):
        await _decide(_session([], [], []), {str(i): (FP, {C1}) for i in range(31)})
    with pytest.raises(ValueError, match="chunk limit"):
        await _decide(_session([], [], []), {"a": (FP, {uuid4() for _ in range(201)})})
    with pytest.raises(ValueError, match="chunk limit"):
        await _decide(_session([], [], []), {"a": (FP, set())})


async def test_no_matches_is_none_and_for_update_is_forwarded() -> None:
    """A candidate with no decisions resolves to None; the for_update flag still reads all rows."""
    session = _session([], [], [])
    assert await _decide(session, {"a": (FP, {C1})}, for_update=True) == {"a": None}
    assert session.scalars.await_count == 2  # memberships and document rows (evidence read is skipped)


async def test_full_evidence_coverage_returns_latest_decision() -> None:
    """Exact decisions on every chunk resolve to the newest matching decision."""
    target = uuid4()
    m1, m2 = _member(C1), _member(C2)
    older, newer = _decision("assign", target, membership=m1, age=1), _decision("assign", target, membership=m2, age=2)
    out = await _decide(_session([m1, m2], [older, newer], []), {"a": (FP, {C1, C2})})
    assert out == {"a": (newer.id, "assign", target)}


async def test_partial_evidence_coverage_is_conflict_with_latest_id() -> None:
    """Decisions covering only some chunks conflict and report the newest decision id."""
    m1, m2 = _member(C1), _member(C2)
    only = _decision("suppress", None, membership=m1)
    out = await _decide(_session([m1, m2], [only], []), {"a": (FP, {C1, C2})})
    assert out == {"a": (only.id, "conflict", None)}


async def test_document_decision_applies_and_disagreement_conflicts() -> None:
    """A document-wide decision resolves alone; evidence disagreeing with it is a conflict."""
    target = uuid4()
    doc_row = _decision("assign", target, fingerprint=FP, age=5)
    assert await _decide(_session([], [], [doc_row]), {"a": (FP, {C1})}) == {"a": (doc_row.id, "assign", target)}
    m1 = _member(C1)
    evidence_row = _decision("suppress", None, membership=m1, age=1)
    out = await _decide(_session([m1], [evidence_row], [doc_row]), {"a": (FP, {C1})})
    assert out["a"][1] == "conflict" and out["a"][0] == doc_row.id


async def test_overlapping_candidates_and_nil_uuid_conflict() -> None:
    """Two candidates sharing a selector are ambiguous; with no rows at all the conflict id is the nil UUID."""
    m1 = _member(C1)
    decision = _decision("assign", uuid4(), membership=m1)
    out = await _decide(_session([m1], [decision], []), {"a": (FP, {C1}), "b": (FP, {C1})})
    assert out["a"][1] == "conflict" and out["b"][1] == "conflict"
    two_members = [_member(C1), _member(C1)]
    out = await _decide(_session(two_members, [], []), {"a": (FP, {C1})})
    assert out == {"a": (UUID(int=0), "conflict", None)}
