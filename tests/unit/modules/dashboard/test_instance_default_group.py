"""Cover default-group selection for instance creation without a database."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from modules.dashboard import public
from modules.dashboard.schemas import InstanceCreate


class _Session:
    """Minimal async session stub returning a fixed scalar and recording adds."""

    def __init__(self, found: object) -> None:
        self.found = found
        self.added: list[object] = []

    async def scalar(self, _query: object) -> object:
        return self.found

    def add(self, row: object) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None


_SCOPE = SimpleNamespace(workspace_id=uuid4())


def test_instance_create_accepts_missing_group_id() -> None:
    payload = InstanceCreate(expected_revision=1, definition_id=uuid4())
    assert payload.group_id is None


@pytest.mark.asyncio
async def test_default_group_created_when_dashboard_has_none() -> None:
    session = _Session(None)
    group = await public._resolve_instance_group(session, SimpleNamespace(id=uuid4()), None, scope=_SCOPE)  # type: ignore[arg-type]
    assert (group.name, group.position) == ("General", 0)
    assert session.added == [group]


@pytest.mark.asyncio
async def test_default_group_reuses_existing() -> None:
    existing = object()
    session = _Session(existing)
    assert await public._resolve_instance_group(session, SimpleNamespace(id=uuid4()), None, scope=_SCOPE) is existing  # type: ignore[arg-type]
    assert session.added == []


@pytest.mark.asyncio
async def test_explicit_foreign_group_is_missing() -> None:
    with pytest.raises(public.DashboardMissing):
        await public._resolve_instance_group(_Session(None), SimpleNamespace(id=uuid4()), uuid4(), scope=_SCOPE)  # type: ignore[arg-type]
