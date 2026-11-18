"""Topic watch Sources: owner-managed only (generic routes refuse), no orphan after a failed provision."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException

from core.errors import ConflictError
from modules.connectors import routes as connector_routes
from modules.connectors import topic_watch as tw
from modules.connectors.provisioning import routes as provisioning_routes
from tests.unit.modules.connectors._routes_patch import patch_routes_mp

SCOPE = SimpleNamespace(workspace_id=uuid4())
CONFIG = tw.TopicWatchConfig("ai chips", None, ("en",), (), (), 5, 23400, "background")


class _Session:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def rollback(self) -> None:
        self.events.append("rollback")


def _sources(monkeypatch: pytest.MonkeyPatch, session: _Session, *, archive_raises=False) -> Any:
    source_id = uuid4()

    async def create(_s: Any, _payload: Any, **_k: Any) -> Any:
        session.events.append("create")
        return SimpleNamespace(id=source_id)

    async def get(_s: Any, sid: Any, **_k: Any) -> Any:
        return SimpleNamespace(id=sid, provider="topic_watch", status="active")

    async def archive(_s: Any, sid: Any, **_k: Any) -> None:
        session.events.append(f"archive:{sid == source_id}")
        if archive_raises:
            raise RuntimeError("boom")

    monkeypatch.setattr(tw.sources, "create_source", create)
    monkeypatch.setattr(tw.sources, "get_connector_source", get)
    monkeypatch.setattr(tw.sources, "archive_source", archive)
    return source_id


@pytest.mark.asyncio
@pytest.mark.parametrize("archive_raises", [False, True])
async def test_failed_provisioning_archives_the_created_source(monkeypatch, archive_raises) -> None:
    """create_source committed; any later failure must not leave an orphan Source behind."""
    session = _Session()
    _sources(monkeypatch, session, archive_raises=archive_raises)

    async def refuse(*_a: Any, **_k: Any) -> None:
        raise ConflictError("source_inactive")

    monkeypatch.setattr(tw, "_save_and_activate", refuse)
    with pytest.raises(ConflictError, match="source_inactive"):  # the cause is never masked
        await tw.provision_topic_watch_source(
            session,  # type: ignore[arg-type]
            name="Watch",
            config=CONFIG,
            scope=SCOPE,  # type: ignore[arg-type]
            multi_workspace_enabled=False,
        )
    assert session.events == ["create", "rollback", "archive:True"]


@pytest.mark.asyncio
async def test_update_of_an_externally_archived_source_has_its_own_code(monkeypatch) -> None:
    async def get(_s: Any, sid: Any, **_k: Any) -> Any:
        return SimpleNamespace(id=sid, provider="topic_watch", status="archived")

    monkeypatch.setattr(tw.sources, "get_connector_source", get)
    with pytest.raises(ConflictError, match="topic_watch_source_archived"):
        await tw.update_topic_watch_source(
            None,  # type: ignore[arg-type]
            uuid4(),
            CONFIG,
            scope=SCOPE,  # type: ignore[arg-type]
            multi_workspace_enabled=False,
        )


def _request() -> Any:
    settings = SimpleNamespace(multi_workspace_enabled=False)
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)))


async def _fence(*_a: Any, **_k: Any) -> None:
    return None


async def _watch_source(*_a: Any, **_k: Any) -> Any:
    return SimpleNamespace(
        id=uuid4(), provider="topic_watch", type="web", status="active", generation=1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route", ["put_configuration", "activate_source", "start_backend_transition"]
)
async def test_provisioning_routes_refuse_a_topic_watch_source(monkeypatch, route) -> None:
    patch_routes_mp(monkeypatch, "_owner_access", _fence)
    patch_routes_mp(monkeypatch, "_source", _watch_source)
    with pytest.raises(HTTPException) as caught:
        await getattr(provisioning_routes, route)(
            uuid4(), None, None, _request(), SimpleNamespace()
        )
    assert caught.value.status_code == 409 and caught.value.detail == "managed_by_scheduled_task"


@pytest.mark.asyncio
async def test_generic_configure_route_refuses_a_topic_watch_source(monkeypatch) -> None:
    monkeypatch.setattr(connector_routes, "_owner_access", _fence)
    monkeypatch.setattr(connector_routes, "_source", _watch_source)
    with pytest.raises(HTTPException) as caught:
        await connector_routes.configure_source(
            uuid4(),
            None,
            None,
            _request(),
            SimpleNamespace(),  # type: ignore[arg-type]
        )
    assert caught.value.status_code == 409 and caught.value.detail == "managed_by_scheduled_task"


@pytest.mark.asyncio
async def test_generic_collect_route_refuses_a_topic_watch_source() -> None:
    """The native queueing step every generic collect passes through refuses before any request."""
    source = await _watch_source()
    with pytest.raises(HTTPException) as caught:
        await connector_routes._queue_native_collection(
            None,  # type: ignore[arg-type]
            _request(),
            source.id,
            source,
            1,
            scope=SCOPE,  # type: ignore[arg-type]
            multi_workspace_enabled=False,
            access_fence=None,  # type: ignore[arg-type]
        )
    assert caught.value.status_code == 409 and caught.value.detail == "managed_by_scheduled_task"
