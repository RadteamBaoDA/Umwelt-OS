"""Widget MCP server: auth, default-off, resources/prompts, assemble_document, traversal."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.routing import Mount

from apps.api.main import create_app
from core.config import Settings
from modules.mcp_widgets.assemble import CSP, MAX_HTML_BYTES, assemble_document
from modules.mcp_widgets.server import create_http_app, load_skill

TOKEN = "t" * 40
ORIGIN = "http://localhost:3000"
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _settings() -> Settings:
    """Settings with the widget server enabled and a token."""
    return Settings(mcp_widgets_enabled=True, mcp_widgets_token=TOKEN)


@asynccontextmanager
async def _client() -> AsyncIterator[AsyncClient]:
    """Client over the guarded MCP app mounted at /mcp with the SDK session manager running."""
    server, guarded = create_http_app(_settings())

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with server.session_manager.run():
            yield

    app = Starlette(routes=[Mount("/mcp", app=guarded)], lifespan=lifespan)
    async with (
        lifespan(app),
        AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client,
    ):
        yield client


async def _rpc(
    client: AsyncClient, method: str, params: dict | None = None, token: str | None = TOKEN
) -> object:
    """POST one JSON-RPC request to /mcp/ and return the response."""
    headers = dict(HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return await client.post("/mcp/", content=json.dumps(body), headers=headers)


@pytest.mark.asyncio
async def test_disabled_by_default_returns_404() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=create_app(Settings())), base_url=ORIGIN
    ) as c:
        assert (await c.post("/mcp/")).status_code == 404


def test_enabled_without_token_refuses() -> None:
    with pytest.raises(RuntimeError):
        create_http_app(Settings(mcp_widgets_enabled=True))


@pytest.mark.asyncio
async def test_auth_missing_wrong_and_valid() -> None:
    async with _client() as c:
        assert (await _rpc(c, "tools/list", token=None)).status_code == 401
        assert (await _rpc(c, "tools/list", token="x" * 40)).status_code == 401
        assert (await _rpc(c, "tools/list")).status_code == 200


@pytest.mark.asyncio
async def test_resources_prompts_and_tool_listing() -> None:
    async with _client() as c:
        res = (await _rpc(c, "resources/read", {"uri": "skills://list"})).json()
        assert json.loads(res["result"]["contents"][0]["text"]) == [
            "advanced-visualization",
            "master-playbook",
            "svg-diagrams",
        ]
        prompts = (await _rpc(c, "prompts/list")).json()["result"]["prompts"]
        assert {p["name"] for p in prompts} == {
            "create_widget",
            "create_svg_diagram",
            "create_visualization",
        }
        tools = (await _rpc(c, "tools/list")).json()["result"]["tools"]
        assert [t["name"] for t in tools] == ["assemble_document"]
        skill = (await _rpc(c, "resources/read", {"uri": "skills://master-playbook"})).json()
        assert "playbook" in skill["result"]["contents"][0]["text"].lower()


@pytest.mark.parametrize(
    "name", ["../x", "..", "a/b", "master-agent-playbook", "", "master-playbook/../x"]
)
def test_load_skill_rejects_unknown_and_traversal(name: str) -> None:
    with pytest.raises(ValueError):
        load_skill(name)


def test_assemble_document_csp_and_explicit_origin() -> None:
    doc = assemble_document("T <b>", "d", "<p>hi</p>", parent_origin=ORIGIN)
    assert CSP in doc and "http" not in CSP
    assert "postMessage(m,TARGET)" in doc and '"http://localhost:3000"' in doc
    assert "'*'" not in doc and '"*"' not in doc
    assert (
        "&lt;b&gt;" in doc
        and "send-prompt" in doc
        and "widget-resize" in doc
        and "open-link" in doc
    )


@pytest.mark.parametrize("origin", ["", "*", "http://*", "ftp://x"])
def test_assemble_rejects_bad_origin(origin: str) -> None:
    with pytest.raises(ValueError):
        assemble_document("t", "d", "<p/>", parent_origin=origin)


def test_assemble_size_limit() -> None:
    assemble_document("t", "d", "a" * MAX_HTML_BYTES, parent_origin=ORIGIN)
    with pytest.raises(ValueError):
        assemble_document("t", "d", "a" * (MAX_HTML_BYTES + 1), parent_origin=ORIGIN)
    with pytest.raises(ValueError):
        assemble_document("t", "d", "  ", parent_origin=ORIGIN)
