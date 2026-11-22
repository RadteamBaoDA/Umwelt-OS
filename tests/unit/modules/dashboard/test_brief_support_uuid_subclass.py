"""Lock-free brief support validation must accept the database driver's UUID subclass."""

import asyncio
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from modules.dashboard import _brief_evidence as ev


class DriverUUID(UUID):
    """Stands in for asyncpg's ``pgproto.UUID`` (a ``uuid.UUID`` subclass returned from rows)."""


def test_lock_free_support_check_normalizes_driver_uuids(monkeypatch: pytest.MonkeyPatch) -> None:
    document_id = DriverUUID(str(uuid4()))
    source_id = DriverUUID(str(uuid4()))
    seen: list[list[UUID]] = []

    async def existing_document_ids(session, ids, *, scope, multi_workspace_enabled):
        # Mirrors the documents contract: plain uuid.UUID only.
        if any(type(i) is not UUID for i in ids):
            raise ValueError("Document identity batch exceeds 100 IDs")
        seen.append(list(ids))
        return list(ids)

    async def get_source_fence(session, sid, *, scope, multi_workspace_enabled):
        return SimpleNamespace(status="active", local_only=False)

    monkeypatch.setattr(ev.documents, "existing_document_ids", existing_document_ids)
    monkeypatch.setattr(ev.sources, "get_source_fence", get_source_fence)

    ok = asyncio.run(
        ev._captured_support_eligible(
            object(),
            {source_id},
            {document_id},
            scope=object(),
            multi_workspace_enabled=False,
            access_fence=None,
            lock=False,
        )
    )

    assert ok is True
    assert seen == [[UUID(str(document_id))]]
    assert type(seen[0][0]) is UUID
