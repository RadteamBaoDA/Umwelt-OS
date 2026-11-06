"""Widget MCP server: auth, default-off, resources/prompts, assemble_document, traversal."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.routing import Mount, Route

from apps.api.main import create_app
from core.config import Settings
from modules.mcp_widgets.assemble import CSP, MAX_HTML_BYTES, assemble_document
from modules.mcp_widgets.server import RootPath, create_http_app, load_skill

TOKEN = "t" * 40
ORIGIN = "http://localhost:3000"
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _settings(**extra: object) -> Settings:
    """Settings with the widget server enabled and a token."""
    return Settings(mcp_widgets_enabled=True, mcp_widgets_token=TOKEN, **extra)


@asynccontextmanager
async def _client(**extra: object) -> AsyncIterator[AsyncClient]:
    """Client over the guarded MCP app mounted at /mcp with the SDK session manager running."""
    server, guarded = create_http_app(_settings(**extra))

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with server.session_manager.run():
            yield

    app = Starlette(
        routes=[Route("/mcp", RootPath(guarded)), Mount("/mcp", app=guarded)], lifespan=lifespan
    )
    async with (
        lifespan(app),
        AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client,
    ):
        yield client


async def _rpc(
    client: AsyncClient,
    method: str,
    params: dict | None = None,
    token: str | None = TOKEN,
    path: str = "/mcp/",
) -> object:
    """POST one JSON-RPC request to path (default /mcp/) and return the response."""
    headers = dict(HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return await client.post(path, content=json.dumps(body), headers=headers)


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
            "agent-skills-vol2",
            "master-agent-playbook",
            "svg-diagram-skill",
        ]
        prompts = (await _rpc(c, "prompts/list")).json()["result"]["prompts"]
        assert {p["name"] for p in prompts} == {
            "create_widget",
            "create_svg_diagram",
            "create_visualization",
        }
        tools = (await _rpc(c, "tools/list")).json()["result"]["tools"]
        assert [t["name"] for t in tools] == ["assemble_document"]
        texts = []
        for name in ("master-agent-playbook", "master-playbook"):  # upstream name and our alias
            skill = (await _rpc(c, "resources/read", {"uri": f"skills://{name}"})).json()
            texts.append(skill["result"]["contents"][0]["text"])
        assert "playbook" in texts[0].lower() and texts[0] == texts[1]


@pytest.mark.parametrize(
    "name", ["../x", "..", "a/b", "", "master-playbook/../x", "master-agent-playbook/"]
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
        and "text:t,prompt:t" in doc
        and "a[href]" in doc
        and "startsWith('https://')" in doc
        and '<div id="content">' in doc
        and "setInterval(report,200)" in doc
        and "--color-text-primary" in doc
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


@pytest.mark.asyncio
async def test_mcp_without_trailing_slash_and_health() -> None:
    async with _client() as c:
        for path in ("/mcp", "/mcp/"):
            assert (await _rpc(c, "tools/list", token=None, path=path)).status_code == 401
            ok = await _rpc(c, "tools/list", path=path)
            assert ok.status_code == 200 and not ok.history
        health = await c.get("/mcp/health")
        assert health.status_code == 200 and health.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_create_app_serves_mcp_without_redirect() -> None:
    app = create_app(_settings())
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as c:
        assert (await c.post("/mcp")).status_code == 401
        assert (await c.post("/mcp/")).status_code == 401


@pytest.mark.asyncio
async def test_cors_preflight_only_for_configured_origin() -> None:
    preflight = {
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    }
    async with _client(mcp_widgets_allowed_origins="https://app.example") as c:
        ok = await c.options("/mcp/", headers={"Origin": "https://app.example", **preflight})
        assert ok.status_code == 200
        assert ok.headers["access-control-allow-origin"] == "https://app.example"
        bad = await c.options("/mcp/", headers={"Origin": "https://evil.example", **preflight})
        assert bad.status_code == 400 and "access-control-allow-origin" not in bad.headers
        assert (await _rpc(c, "tools/list", token=None)).status_code == 401  # bearer still required
    async with _client() as c:  # default: no CORS, preflight hits the bearer guard
        denied = await c.options("/mcp/", headers={"Origin": "https://app.example", **preflight})
        assert denied.status_code == 401


def test_wildcard_allowed_origin_rejected() -> None:
    with pytest.raises(ValueError):
        Settings(mcp_widgets_allowed_origins="*")


def test_design_css_matches_typescript_source() -> None:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[4] / "scripts" / "sync_design_css.py"
    spec = importlib.util.spec_from_file_location("sync_design_css", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.TARGET.read_text(encoding="utf-8") == mod.render(), "run scripts/sync_design_css.py"
