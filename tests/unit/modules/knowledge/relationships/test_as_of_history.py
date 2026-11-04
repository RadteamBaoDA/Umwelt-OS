"""Characterization of ``_list_relationships_as_of`` snapshot paging and per-item support re-checks."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from core.pagination import encode_cursor
from modules.knowledge.relationships import public as rel

CUTOFF = datetime(2026, 5, 1, tzinfo=UTC)
WS = uuid4()
SCOPE = SimpleNamespace(workspace_id=WS)
KW: dict[str, Any] = {
    "limit": 2, "cursor": None, "entity_id": None, "valid_at": None, "include_unknown_validity": False,
    "knowledge_as_of": CUTOFF, "fingerprint": "fp", "scope": SCOPE, "multi_workspace_enabled": False,
}


def _state(origin: str = "owner", *, valid: bool = True) -> dict[str, Any]:
    """Serialized RelationshipRead state with bounded (or unknown) validity."""
    return {
        "id": str(uuid4()), "source_entity_id": str(uuid4()), "target_entity_id": str(uuid4()), "type": "KNOWS",
        "origin": origin, "confidence": 0.5,
        "valid_from": CUTOFF.isoformat() if valid else None, "valid_to": CUTOFF.isoformat() if valid else None,
        "metadata": {}, "created_at": CUTOFF.isoformat(),
    }


def _history(state: dict[str, Any] | None, *, deleted: bool = False, support: list[Any] | None = None) -> Any:
    """Snapshot history row."""
    return SimpleNamespace(
        relationship_id=uuid4(), recorded_at=CUTOFF, deleted=deleted, state=state, support=support or [],
    )


def _session(*results: list[Any], exists: bool = True) -> Any:
    """Session: ``scalars`` yields the given lists in order (missing probe, then page); ``scalar`` is the live-row check."""
    pending = list(results)

    async def scalars(_statement: Any) -> Any:
        batch = pending.pop(0)
        return SimpleNamespace(all=lambda: batch)

    return SimpleNamespace(
        scalars=AsyncMock(side_effect=scalars), scalar=AsyncMock(return_value=uuid4() if exists else None),
    )


@pytest.fixture(autouse=True)
def _entities(monkeypatch: pytest.MonkeyPatch) -> None:
    """Endpoint refs resolve to themselves."""
    async def refs(session: Any, ids: list[Any], **_k: Any) -> list[Any]:
        return [SimpleNamespace(requested_id=i, canonical_id=i) for i in ids]

    monkeypatch.setattr(rel.entities, "get_entity_refs", refs)


async def test_owner_snapshot_without_support_is_listed_and_history_complete() -> None:
    """A live owner snapshot with no support becomes an item with empty evidence; coverage stays complete."""
    row = _history(_state())
    page = await rel._list_relationships_as_of(_session([], [row]), **KW)
    assert [str(item.id) for item in page.items] == [row.state["id"]]
    assert page.items[0].evidence == [] and page.canonical_history_available is True
    assert page.next_cursor is None and page.knowledge_as_of == CUTOFF and page.unavailable_relationship_ids == []


async def test_deleted_and_unknown_validity_snapshots_are_skipped() -> None:
    """Deleted rows vanish silently; unknown validity is hidden unless explicitly included."""
    rows = [_history(_state(), deleted=True), _history(_state(valid=False))]
    page = await rel._list_relationships_as_of(_session([], rows), **KW)
    assert page.items == [] and page.unavailable_relationship_ids == []
    page = await rel._list_relationships_as_of(_session([], [_history(_state(valid=False))]), **{**KW, "include_unknown_validity": True})
    assert len(page.items) == 1


async def test_missing_state_or_live_row_and_derived_without_support_are_unavailable() -> None:
    """Empty state, a vanished live row and derived snapshots lacking support are reported unavailable."""
    no_state, derived = _history(None), _history(_state("derived"))
    page = await rel._list_relationships_as_of(_session([], [no_state, derived]), **KW)
    assert page.unavailable_relationship_ids == [no_state.relationship_id, derived.relationship_id]
    assert page.canonical_history_available is False
    gone = _history(_state())
    page = await rel._list_relationships_as_of(_session([], [gone], exists=False), **KW)
    assert page.unavailable_relationship_ids == [gone.relationship_id]


async def test_missing_snapshots_are_hints_and_break_coverage() -> None:
    """Relationships without any snapshot at the cutoff are listed (max 100) and mark history incomplete."""
    missing = [uuid4() for _ in range(3)]
    page = await rel._list_relationships_as_of(_session(missing, []), **KW)
    assert page.unavailable_relationship_ids == missing and page.canonical_history_available is False


async def test_extra_row_produces_cursor_that_round_trips() -> None:
    """limit+1 snapshots trim the page and emit a fingerprint-bound cursor at the last returned row."""
    rows = [_history(_state()) for _ in range(3)]
    page = await rel._list_relationships_as_of(_session([], rows), **KW)
    assert len(page.items) == 2 and page.next_cursor is not None
    bound, position = json.loads(base64.b64decode(page.next_cursor + "=" * (-len(page.next_cursor) % 4), altchars=b"-_"))
    assert bound == "fp" and position == encode_cursor(rows[1].recorded_at, rows[1].relationship_id)
    again = await rel._list_relationships_as_of(_session([], []), **{**KW, "cursor": page.next_cursor})
    assert again.items == []


async def test_cursor_for_other_fingerprint_is_invalid() -> None:
    """A cursor bound to another fingerprint (or garbage) raises ValueError before the page query runs."""
    cursor = base64.urlsafe_b64encode(json.dumps(["other", encode_cursor(CUTOFF, uuid4())]).encode()).decode().rstrip("=")
    for bad in (cursor, "!!!", "A" * 2000):
        session = _session([])
        with pytest.raises(ValueError, match="Invalid relationship history cursor"):
            await rel._list_relationships_as_of(session, **{**KW, "cursor": bad})
        assert session.scalars.await_count == 1  # only the missing-snapshot probe ran


async def test_valid_at_filters_out_of_range_intervals() -> None:
    """Snapshots whose validity interval excludes ``valid_at`` are dropped."""
    row = _history(_state())
    page = await rel._list_relationships_as_of(_session([], [row]), **{**KW, "valid_at": datetime(2027, 1, 1, tzinfo=UTC)})
    assert page.items == []
