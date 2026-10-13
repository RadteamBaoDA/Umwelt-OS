"""finish_activity deletes clean rows and keeps uncertain ones."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from modules.backup import public as backup


class _Session:
    def __init__(self, row: Any) -> None:
        self.row, self.deleted = row, []

    async def scalar(self, _stmt: Any) -> Any:
        return self.row

    async def delete(self, row: Any) -> None:
        self.deleted.append(row)

    async def flush(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _admit(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ok(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(backup, "admit_write", ok)


def _receipt() -> Any:
    return backup.ActivityReceipt(activity_id=uuid4(), epoch=1, kind="k")


async def test_clean_finish_deletes_row() -> None:
    row = SimpleNamespace(state="active", finished_at=None)
    s = _Session(row)
    assert await backup.finish_activity(s, _receipt()) is True  # type: ignore[arg-type]
    assert s.deleted == [row] and row.state == "active"


async def test_interrupted_deletes_and_uncertain_updates() -> None:
    s = _Session(SimpleNamespace(state="active", finished_at=None))
    assert await backup.finish_activity(s, _receipt(), interrupted=True) is True  # type: ignore[arg-type]
    assert len(s.deleted) == 1
    row = SimpleNamespace(state="active", finished_at=None)
    s = _Session(row)
    assert await backup.finish_activity(s, _receipt(), uncertain=True) is True  # type: ignore[arg-type]
    assert row.state == "uncertain" and row.finished_at is not None and not s.deleted


async def test_missing_row_returns_false() -> None:
    assert await backup.finish_activity(_Session(None), _receipt()) is False  # type: ignore[arg-type]
