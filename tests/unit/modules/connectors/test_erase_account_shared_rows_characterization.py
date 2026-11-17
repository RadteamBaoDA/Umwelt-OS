"""Characterization tests for ``erase_account_shared_rows`` checkpoint steps and counter bookkeeping."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from modules.connectors import erasure

SCOPE = SimpleNamespace(workspace_id=uuid4(), account_id=11)


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class _Session:
    """Scripted session: queued answers per call kind plus an ordered operation log."""

    def __init__(
        self,
        scalar: list[Any] | None = None,
        scalars: list[list[Any]] | None = None,
        execute: list[list[Any]] | None = None,
        get: list[Any] | None = None,
    ) -> None:
        self._scalar, self._scalars = list(scalar or []), list(scalars or [])
        self._execute, self._get = list(execute or []), list(get or [])
        self.log: list[str] = []
        self.deleted: list[Any] = []

    async def scalar(self, stmt: object) -> Any:
        self.log.append("scalar")
        return self._scalar.pop(0)

    async def scalars(self, stmt: object) -> _Result:
        self.log.append("scalars")
        return _Result(self._scalars.pop(0))

    async def execute(self, stmt: object) -> _Result:
        self.log.append("execute")
        return _Result(self._execute.pop(0) if self._execute else [])

    async def get(self, model: object, key: object, **kwargs: object) -> Any:
        self.log.append("get")
        return self._get.pop(0)

    async def delete(self, row: Any) -> None:
        self.log.append("delete")
        self.deleted.append(row)

    async def flush(self) -> None:
        self.log.append("flush")


async def _run(session: _Session, cursor: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    with patch.object(erasure, "authorize_account_erasure", AsyncMock()):
        return await erasure.erase_account_shared_rows(session, SCOPE, cursor)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_outbox_step_resets_pages_and_advances_when_none_remain() -> None:
    rows = [
        SimpleNamespace(fanout_page={"x": 1}, binding_cursor="c", state="complete", attempts=3)
        for _ in range(2)
    ]
    done, cursor = await _run(_Session(scalars=[rows]), {})
    assert (done, cursor) == (False, {"step": "outbox"})
    assert all(
        (r.fanout_page, r.binding_cursor, r.state, r.attempts) == (None, None, "pending", 0)
        for r in rows
    )
    assert await _run(_Session(scalars=[[]]), {"step": "outbox"}) == (False, {"step": "hints"})


@pytest.mark.asyncio
async def test_hint_step_releases_reservations_or_detects_inconsistent_capacity() -> None:
    hints = [SimpleNamespace(capacity_reserved=True) for _ in range(3)]
    capacity = SimpleNamespace(pending_count=5)
    done, cursor = await _run(_Session(scalars=[hints], get=[capacity]), {"step": "hints"})
    assert (done, cursor) == (False, {"step": "hints"})
    assert capacity.pending_count == 2 and not any(h.capacity_reserved for h in hints)
    assert await _run(_Session(scalars=[[]]), {"step": "hints"}) == (False, {"step": "deliveries"})
    for bad in (None, SimpleNamespace(pending_count=1)):
        with pytest.raises(RuntimeError, match="reservation is inconsistent"):
            await _run(_Session(scalars=[hints], get=[bad]), {"step": "hints"})


@pytest.mark.asyncio
async def test_delivery_step_deletes_receipt_and_adjusts_counters() -> None:
    delivery = SimpleNamespace(id=uuid4())
    capacity = SimpleNamespace(digest_count=2, pending_count=4)
    session = _Session(scalar=[delivery, SimpleNamespace(capacity_reserved=True)], get=[capacity])
    assert await _run(session, {"step": "deliveries"}) == (False, {"step": "deliveries"})
    assert (capacity.digest_count, capacity.pending_count) == (1, 3) and session.deleted == [
        delivery
    ]
    assert await _run(_Session(scalar=[None]), {"step": "deliveries"}) == (False, {"step": "quota"})
    for bad_capacity, message in (
        (None, "digest is inconsistent"),
        (SimpleNamespace(digest_count=0, pending_count=1), "digest is inconsistent"),
        (SimpleNamespace(digest_count=1, pending_count=0), "pending count is inconsistent"),
    ):
        with pytest.raises(RuntimeError, match=message):
            await _run(
                _Session(
                    scalar=[delivery, SimpleNamespace(capacity_reserved=True)], get=[bad_capacity]
                ),
                {"step": "deliveries"},
            )


@pytest.mark.asyncio
async def test_quota_step_removes_debit_and_window_only_without_peer_debits() -> None:
    debit = SimpleNamespace(
        provider_id="p", budget_kind="k", subject_hash="s", policy_key="pk", window_start=1
    )
    session = _Session(execute=[[debit], []], scalar=[None])
    assert await _run(session, {"step": "quota"}) == (False, {"step": "quota"})
    assert session.log == ["execute", "delete", "flush", "scalar", "execute"]
    shared = _Session(execute=[[debit]], scalar=[uuid4()])
    assert await _run(shared, {"step": "quota"}) == (False, {"step": "quota"})
    assert shared.log == ["execute", "delete", "flush", "scalar"]
    assert await _run(_Session(execute=[[]]), {"step": "quota"}) == (False, {"step": "slots"})


@pytest.mark.asyncio
async def test_slots_step_is_final_and_unknown_steps_are_rejected() -> None:
    session = _Session()
    assert await _run(session, {"step": "slots"}) == (True, {})
    assert session.log == ["execute", "execute"]
    with pytest.raises(ValueError, match="Invalid connector shared-erasure checkpoint"):
        await _run(_Session(), {"step": "nope"})
