"""Flipped route groups admit a non-bootstrap workspace owner and deny members/foreign workspaces.

Account auth, session and module gate are stubbed; the REAL workspace dependencies run on top of a
patched workspace resolver. A request that passes admission but fails body validation (422) proves the
handler chain was reached without any owner_id == 1 check.
"""

from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

import core.workspaces.dependencies as deps
from core.auth.dependencies import require_account, require_account_write
from core.auth.schemas import AccountRead
from core.database import get_session
from core.workspaces.schemas import WorkspaceContext
from modules.connectors.github.routes import router as github_router
from modules.goals.routes import router as goals_router
from modules.knowledge.entities.routes import router as entities_router
from modules.knowledge.observations.routes import router as observations_router
from modules.knowledge.relationships.routes import router as relationships_router
from modules.knowledge.temporal.routes import router as temporal_router
from modules.tasks.routes import router as tasks_router
from modules.timeline.routes import router as timeline_router

WS = uuid4()
ID = "00000000-0000-4000-8000-000000000001"
BAD = "not-a-uuid"  # reads: admission runs first, then path validation answers 422
ACCOUNT = AccountRead(id=7, email=None, default_workspace_id=WS, email_verified_at=None,
                      email_verification_source=None)
# (router, write (method, path), read path). Writes send an empty body; reads send no query.
GROUPS = {
    "tasks": (tasks_router, ("DELETE", f"/api/v1/tasks/{ID}"), f"/api/v1/tasks/{BAD}"),
    "goals": (goals_router, ("DELETE", f"/api/v1/goals/{ID}"), f"/api/v1/goals/{BAD}"),
    "timeline": (timeline_router, ("DELETE", f"/api/v1/events/{ID}"), f"/api/v1/events/{BAD}"),
    "entities": (entities_router, ("POST", "/api/v1/entities"), f"/api/v1/entities/{BAD}"),
    "relationships": (relationships_router, ("DELETE", f"/api/v1/relationships/{ID}"),
                      f"/api/v1/relationships/{BAD}/evidence"),
    "temporal": (temporal_router, ("POST", "/api/v1/system/graph/reconcile"), "/api/v1/system/graph/status"),
    "observations": (observations_router, None, "/api/v1/observations"),
    "github": (github_router, ("POST", f"/api/v1/connectors/{ID}/github/oauth/start"),
               f"/api/v1/connectors/{ID}/github/status"),
}


def _client(router: APIRouter, monkeypatch: pytest.MonkeyPatch, outcome: str) -> TestClient:
    app = FastAPI()
    app.state.settings = SimpleNamespace(multi_workspace_enabled=True)
    app.include_router(router)

    async def account(request: Request) -> None:
        request.state.account = ACCOUNT  # non-bootstrap account: require_owner would have denied it

    async def noop() -> None:
        return None

    async def session() -> Iterator[AsyncMock]:  # type: ignore[misc]
        yield AsyncMock()

    app.dependency_overrides.update({
        require_account: account, require_account_write: account, get_session: session,
    })
    gates = [d.dependency for d in router.dependencies]  # module availability gates are covered elsewhere
    for route in router.routes:
        gates += [d.dependency for d in getattr(route, "dependencies", [])]
    for gate in gates:
        if getattr(gate, "__name__", "") == "require_enabled_module":
            app.dependency_overrides[gate] = noop

    async def selected(_request: Request, _session: object, account_id: int, _wid: object) -> WorkspaceContext:
        if outcome == "foreign":
            raise HTTPException(status_code=404, detail="Workspace not found")
        return WorkspaceContext(user_id=account_id, workspace_id=WS, role=outcome, membership_revision=1)  # type: ignore[arg-type]

    monkeypatch.setattr(deps, "_selected_workspace", selected)
    return TestClient(app, raise_server_exceptions=False, headers={"Origin": "http://testserver"})


def _call(client: TestClient, method: str, path: str) -> int:
    kwargs: dict[str, object] = {"json": {}} if method in {"POST", "PUT", "PATCH"} else {}
    return client.request(method, path, headers={"X-Workspace-ID": str(WS)}, **kwargs).status_code  # type: ignore[arg-type]


_ADMITTED = lambda code: code not in {401, 403, 404}


@pytest.mark.parametrize("name", [n for n, g in GROUPS.items() if g[1] is not None])
def test_member_denied_on_writes(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    router, write, _read = GROUPS[name]
    assert _call(_client(router, monkeypatch, "member"), *write) == 403  # type: ignore[misc]


@pytest.mark.parametrize("name", list(GROUPS))
def test_owner_reaches_handlers(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    router, write, read = GROUPS[name]
    client = _client(router, monkeypatch, "owner")
    if name != "github":  # its read handler has no pre-handler validation to stop on
        assert _ADMITTED(_call(client, "GET", read))
    if write is not None:
        assert _ADMITTED(_call(client, *write))


@pytest.mark.parametrize("name", list(GROUPS))
def test_foreign_workspace_is_404(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    router, write, read = GROUPS[name]
    client = _client(router, monkeypatch, "foreign")
    assert _call(client, "GET", read) == 404
    if write is not None:
        assert _call(client, *write) == 404
