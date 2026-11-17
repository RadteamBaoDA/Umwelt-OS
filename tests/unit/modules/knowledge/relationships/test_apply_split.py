"""Characterization of relationship ``apply_entity_split`` support re-homing."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from modules.knowledge.relationships import public as rel
from tests.unit.relationships_patching import patch_relationship

WS, ENTITY, NEW, OTHER = uuid4(), uuid4(), uuid4(), uuid4()


def _row(origin: str = "derived", metadata: Any = None) -> Any:
    """Source relationship row."""
    return SimpleNamespace(
        id=uuid4(),
        source_entity_id=ENTITY,
        target_entity_id=OTHER,
        type="KNOWS",
        origin=origin,
        valid_from=None,
        valid_to=None,
        metadata_json=metadata or {},
    )


def _support(row: Any, source_membership: Any = None, target_membership: Any = None) -> Any:
    """Support row for ``row``."""
    return SimpleNamespace(
        id=uuid4(),
        relationship_id=row.id,
        source_membership_id=source_membership,
        target_membership_id=target_membership,
    )


class _Session:
    """Scripted session recording ORM side effects."""

    def __init__(self, scalars: list[Any], scalar: list[Any]) -> None:
        self.scalar_queue, self.scalars_queue = list(scalar), list(scalars)
        self.log: list[str] = []

    async def scalars(self, _statement: Any) -> Any:
        batch = self.scalars_queue.pop(0)
        return SimpleNamespace(all=lambda: batch)

    async def scalar(self, _statement: Any) -> Any:
        return self.scalar_queue.pop(0)

    def add(self, row: Any) -> None:
        self.log.append(f"add:{type(row).__name__}")
        if getattr(row, "id", None) is None:
            row.id = uuid4()

    async def delete(self, row: Any) -> None:
        self.log.append(f"delete:{type(row).__name__}")

    async def flush(self) -> None:
        self.log.append("flush")


@pytest.fixture(autouse=True)
def _patched(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize admission, validation, confidence refresh and history recording."""
    patch_relationship(monkeypatch, "_admit", AsyncMock())
    patch_relationship(monkeypatch, "validate_entity_split_plan", lambda *_a: None)
    patch_relationship(monkeypatch, "_refresh_derived_confidence", AsyncMock())
    patch_relationship(monkeypatch, "record_relationship_history", AsyncMock())


def _refs(*rows: Any) -> AsyncMock:
    """Patch target for ``list_correction_relationship_refs``."""
    return AsyncMock(return_value=[SimpleNamespace(id=row.id) for row in rows])


async def _split(session: Any, row: Any, membership_ids: set[Any]) -> Any:
    """Run ``apply_entity_split`` for one relationship."""
    return await rel.apply_entity_split(
        session,
        ENTITY,
        NEW,
        membership_ids,
        {row.id},
        scope=SimpleNamespace(workspace_id=WS),
        multi_workspace_enabled=False,
    )


async def test_partial_move_keeps_source_and_reuses_equal_existing_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only some supports move: the source stays, and an equal existing edge is reused instead of created."""
    row, member = _row(), uuid4()
    moving, staying = _support(row, source_membership=member), _support(row)
    existing = SimpleNamespace(id=uuid4(), metadata_json={})
    patch_relationship(monkeypatch, "list_correction_relationship_refs", _refs(row))
    session = _Session([[row], [moving, staying]], [existing, uuid4(), uuid4()])
    out = await _split(session, row, {member})
    assert (
        out == [(row.id, existing.id)]
        and moving.relationship_id == existing.id
        and staying.relationship_id == row.id
    )
    assert session.log == ["flush"]
    assert rel._refresh_derived_confidence.await_args.args[1] == {row.id, existing.id}


async def test_all_supports_moving_creates_destination_and_deletes_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every support moves: a new edge is created, supports re-pointed and the old edge deleted."""
    row, member = _row(), uuid4()
    support = _support(row, source_membership=member)
    patch_relationship(monkeypatch, "list_correction_relationship_refs", _refs(row))
    session = _Session([[row], [support]], [None, uuid4(), uuid4()])
    out = await _split(session, row, {member})
    destination_id = support.relationship_id
    assert out == [(row.id, destination_id)] and destination_id != row.id
    assert session.log[:3] == ["add:Relationship", "flush", "delete:SimpleNamespace"]


async def test_split_rejections(monkeypatch: pytest.MonkeyPatch) -> None:
    """Owner edges, self relationships, metadata conflicts and a changed closure are rejected."""
    member = uuid4()
    owner = _row("owner")
    patch_relationship(monkeypatch, "list_correction_relationship_refs", _refs(owner))
    with pytest.raises(ValueError, match="owner-authored relationship"):
        await _split(
            _Session([[owner], [_support(owner, source_membership=member)]], []), owner, {member}
        )
    selfish = _row()
    selfish.target_entity_id = NEW
    patch_relationship(monkeypatch, "list_correction_relationship_refs", _refs(selfish))
    with pytest.raises(ValueError, match="self relationship"):
        await _split(
            _Session([[selfish], [_support(selfish, source_membership=member)]], []),
            selfish,
            {member},
        )
    row = _row(metadata={"a": 1})
    patch_relationship(monkeypatch, "list_correction_relationship_refs", _refs(row))
    conflicting = SimpleNamespace(id=uuid4(), metadata_json={"b": 2})
    with pytest.raises(ValueError, match="conflicting relationship metadata"):
        await _split(
            _Session([[row], [_support(row, source_membership=member)]], [conflicting]),
            row,
            {member},
        )
    patch_relationship(monkeypatch, "list_correction_relationship_refs", AsyncMock(return_value=[]))
    with pytest.raises(ValueError, match="closure changed"):
        await _split(_Session([], []), row, {member})
