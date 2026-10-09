"""W4-jobs-k: a typed admission denial is terminal (or a no-retry skip) for every knowledge worker."""

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from core.job_denial import PERMISSION_LOST, STALE_SCOPE, admit_retry_stale, denial_code
from modules.dashboard import worker as dashboard_worker
from modules.knowledge.documents import worker as documents_worker
from modules.knowledge.entities import worker as entities_worker
from modules.knowledge.temporal import worker as temporal_worker
from modules.news import worker as news_worker
from modules.search import indexing as search_indexing
from modules.timeline import worker as timeline_worker

WS = uuid4()
OWNER = SimpleNamespace(user_id=1, membership_revision=1)


class _Factory:
    def __init__(self, session: Any) -> None:
        self.session = session

    def __call__(self) -> "_Factory":
        return self

    async def __aenter__(self) -> Any:
        return self.session

    async def __aexit__(self, *exc: object) -> None:
        return None


def _session() -> MagicMock:
    session = MagicMock()
    for name in ("rollback", "commit", "scalar", "execute"):
        setattr(session, name, AsyncMock(return_value=None))
    session.scalar = AsyncMock(return_value=WS)
    return session


def _ctx(session: Any) -> dict[str, Any]:
    return {"session_factory": _Factory(session), "settings": SimpleNamespace(multi_workspace_enabled=False),
            "redis": MagicMock()}


def _deny(status: int) -> AsyncMock:
    return AsyncMock(side_effect=HTTPException(status_code=status))


def test_denial_code_and_single_reresolve() -> None:
    assert denial_code(HTTPException(status_code=401)) == PERMISSION_LOST
    assert denial_code(HTTPException(status_code=404)) == PERMISSION_LOST
    assert denial_code(HTTPException(status_code=409)) == STALE_SCOPE
    assert denial_code(HTTPException(status_code=500)) is None
    assert denial_code(ValueError()) is None


async def test_stale_is_reresolved_exactly_once() -> None:
    calls = AsyncMock(side_effect=[HTTPException(status_code=409), "ok"])
    assert await admit_retry_stale(calls) == "ok"
    twice = AsyncMock(side_effect=HTTPException(status_code=409))
    with pytest.raises(HTTPException):
        await admit_retry_stale(twice)
    assert twice.await_count == 2


# Each case: (name, code, expected terminal code or None for a no-mutation skip).
CASES: list[tuple[str, int, str | None]] = [
    ("documents", 404, None), ("entities_ready", 404, None), ("news", 401, None), ("dashboard", 404, None),
    ("graph", 404, PERMISSION_LOST), ("graph", 409, STALE_SCOPE),
    ("timeline", 404, PERMISSION_LOST), ("timeline", 409, STALE_SCOPE),
    ("entities_work", 404, PERMISSION_LOST), ("entities_work", 409, STALE_SCOPE),
    ("search", 401, PERMISSION_LOST), ("search", 409, STALE_SCOPE),
]


@pytest.mark.parametrize(("name", "status", "terminal"), CASES)
async def test_denial_never_raises_and_is_terminal(
    monkeypatch: pytest.MonkeyPatch, name: str, status: int, terminal: str | None,
) -> None:
    session = _session()
    ctx = _ctx(session)
    recorded = AsyncMock()
    event = str(uuid4())

    async def scope(*_a: Any, **_k: Any) -> Any:
        return SimpleNamespace(workspace_id=WS)

    owner = AsyncMock(return_value=OWNER)
    deny = _deny(status)
    run: Callable[[], Any]
    if name == "documents":
        monkeypatch.setattr(documents_worker.ingestion, "read_document_cleanup_event_operation_id",
                            AsyncMock(return_value=uuid4()))
        monkeypatch.setattr(documents_worker.ingestion, "resolve_ingestion_event_scope", deny)
        run = lambda: documents_worker.process_document_cleanup(ctx, event)
    elif name == "entities_ready":
        monkeypatch.setattr(entities_worker.ingestion, "resolve_ingestion_event_scope", deny)
        run = lambda: entities_worker.process_document_ready(ctx, event)
    elif name == "news":
        monkeypatch.setattr(news_worker.ingestion, "resolve_ingestion_event_scope", deny)
        run = lambda: news_worker.process_news_document_ready(ctx, event)
    elif name == "dashboard":
        monkeypatch.setattr(dashboard_worker.workspaces, "list_workspace_job_candidate_ids",
                            AsyncMock(return_value=[WS]))
        monkeypatch.setattr(dashboard_worker.workspaces, "resolve_workspace_owner_context", owner)
        monkeypatch.setattr(dashboard_worker.briefs, "_admit", deny)
        monkeypatch.setattr(dashboard_worker, "_read_workspace_cursor", AsyncMock(return_value=None))
        monkeypatch.setattr(dashboard_worker, "_write_workspace_cursor", AsyncMock())
        run = lambda: dashboard_worker.run_scheduled_brief(ctx)
    elif name == "graph":
        row = SimpleNamespace(workspace_id=WS, mapping_id=uuid4(), partition_id=uuid4())
        session.execute = AsyncMock(return_value=SimpleNamespace(one_or_none=lambda: row))
        session.scalar = AsyncMock(return_value=uuid4())
        monkeypatch.setattr(temporal_worker.workspaces, "resolve_workspace_owner_context", owner)
        monkeypatch.setattr(temporal_worker.workspaces, "authorize_internal_job", deny)
        monkeypatch.setattr(temporal_worker, "terminalize", recorded)
        run = lambda: temporal_worker.process_graph_operation(ctx, str(uuid4()))
    elif name in {"timeline", "entities_work"}:
        mod = timeline_worker if name == "timeline" else entities_worker
        monkeypatch.setattr(mod, "_workspace_job_scope", scope)
        monkeypatch.setattr(mod.settings_public, "module_is_enabled", AsyncMock(return_value=True))
        monkeypatch.setattr(mod, "terminalize", recorded)
        target = mod.timeline if name == "timeline" else mod.entities
        monkeypatch.setattr(target, "claim_extraction_work", deny)
        entry = (timeline_worker.process_timeline_extraction_work if name == "timeline"
                 else entities_worker.process_entity_extraction_work)
        entry = getattr(entry, "__wrapped__", entry)  # skip the heavy-slot decorator
        run = lambda: entry(ctx, str(uuid4()))
    else:
        monkeypatch.setattr(search_indexing.workspaces, "resolve_workspace_owner_context", owner)
        monkeypatch.setattr(search_indexing, "capture_authority", deny)
        monkeypatch.setattr(search_indexing, "terminalize", recorded)
        run = lambda: search_indexing._index_generation(
            _Factory(session), MagicMock(), ctx["settings"], uuid4(), WS)

    await run()  # never raises
    if terminal is None:
        recorded.assert_not_awaited()
    else:
        recorded.assert_awaited_once()
        assert terminal in recorded.await_args.args
