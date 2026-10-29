"""Unit tests for server templates: service bounds/ownership/cap, tool runner, specs, theme tool and routes."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

import core.workspaces.models  # noqa: F401  (registers the workspaces table for FK resolution)
from core.workspaces.schemas import WorkspaceContext
from modules.chat import templates, ui_theme
from modules.chat.generative_ui.prompt import build_generative_ui_system_prompt
from modules.chat.generative_ui.tool_specs import (
    TEMPLATE_TOOLS,
    TOOL_SET_THEME,
    build_chat_tool_specs,
)
from modules.chat.models import ChatTemplate

WS = uuid4()
SCOPE = WorkspaceContext(user_id=1, workspace_id=WS, role="owner", membership_revision=1)
CONV = uuid4()


class FakeSession:
    """AsyncSession stand-in: ``scalars`` serves the owner's rows, ``scalar`` serves a canned value."""

    def __init__(self, rows: list[ChatTemplate] | None = None, scalar: Any = None) -> None:
        self.rows = rows or []
        self.scalar_value = scalar
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.executed: list[tuple[str, Any]] = []
        self.commits = self.rollbacks = 0

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def delete(self, row: Any) -> None:
        self.deleted.append(row)

    async def flush(self) -> None:
        return None

    async def execute(self, stmt: Any, params: Any = None) -> None:
        self.executed.append((str(stmt), params))

    async def scalars(self, _stmt: Any) -> list[Any]:
        return list(self.rows)

    async def scalar(self, _stmt: Any) -> Any:
        return self.scalar_value

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


def _row(name: str = "t", **kw: Any) -> ChatTemplate:
    return ChatTemplate(id=uuid4(), workspace_id=WS, actor_user_id=1, name=name, description="", html="<p/>",
                        data_description="", version=kw.pop("version", 1), **kw)


def _save(session: FakeSession, **kw: Any) -> ChatTemplate:
    payload = templates.TemplateIn(**{"name": "n", "html": "<b>x</b>", **kw})
    return asyncio.run(templates.save_template(session, SCOPE, payload))


def test_save_creates_version_one_for_the_owner() -> None:
    session = FakeSession()
    row = _save(session)
    assert session.added == [row] and row.version == 1 and row.workspace_id == WS and row.actor_user_id == 1


def test_save_updates_own_row_and_bumps_version() -> None:
    mine = _row(version=3)
    row = _save(FakeSession([mine]), id=str(mine.id), name="renamed")
    assert row is mine and mine.name == "renamed" and mine.version == 4


def test_save_unknown_or_foreign_id_is_not_found() -> None:
    with pytest.raises(templates.TemplateError) as err:
        _save(FakeSession([_row()]), id=str(uuid4()))
    assert err.value.code == "not_found"


def test_cap_of_thirty_per_owner() -> None:
    assert _save(FakeSession([_row(f"t{i}") for i in range(29)])).version == 1
    with pytest.raises(templates.TemplateError) as err:
        _save(FakeSession([_row(f"t{i}") for i in range(30)]))
    assert err.value.code == "limit"


@pytest.mark.parametrize("bad", [
    {"name": ""}, {"name": "x" * 121}, {"description": "d" * 501}, {"data_description": "d" * 501},
    {"html": ""}, {"html": "h" * 60_001},
])
def test_field_bounds(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        templates.TemplateIn(**{"name": "n", "html": "<p/>", **bad})
    assert templates.TemplateIn(name="x" * 120, html="h" * 60_000, description="d" * 500).name == "x" * 120


def test_delete_seed_is_rejected_and_foreign_is_not_found() -> None:
    with pytest.raises(templates.TemplateError) as err:
        asyncio.run(templates.delete_template(FakeSession(), SCOPE, "seed-kpi-q1"))
    assert err.value.code == "invalid"
    with pytest.raises(templates.TemplateError) as err2:
        asyncio.run(templates.delete_template(FakeSession(), SCOPE, str(uuid4())))  # scalar() -> None
    assert err2.value.code == "not_found"


def test_delete_removes_row_and_clears_pending() -> None:
    mine = _row()
    conv = SimpleNamespace(pending_template=str(mine.id))
    session = FakeSession([conv], scalar=mine)  # scalars() serves the pending conversations
    asyncio.run(templates.delete_template(session, SCOPE, str(mine.id)))
    assert session.deleted == [mine] and conv.pending_template is None


def test_pending_requires_owned_conversation_and_template() -> None:
    with pytest.raises(templates.TemplateError):
        asyncio.run(templates.set_pending(FakeSession(), SCOPE, CONV, "seed-kpi-q1"))  # no conversation
    conv = SimpleNamespace(pending_template=None)

    class Sess(FakeSession):
        async def scalar(self, stmt: Any) -> Any:
            return conv if "chat_conversations" in str(stmt) else None

    with pytest.raises(templates.TemplateError):
        asyncio.run(templates.set_pending(Sess(), SCOPE, CONV, str(uuid4())))  # template not owned
    assert asyncio.run(templates.set_pending(Sess(), SCOPE, CONV, "seed-kpi-q1")) == "seed-kpi-q1"
    assert conv.pending_template == "seed-kpi-q1"
    asyncio.run(templates.clear_pending(Sess(), SCOPE, CONV))
    assert conv.pending_template is None


def test_three_read_only_seeds_with_bounded_html() -> None:
    assert [s["id"] for s in templates.SEED_TEMPLATES] == ["seed-weather-ny", "seed-invoice-northwind", "seed-kpi-q1"]
    assert all(len(s["html"]) <= templates.MAX_HTML_CHARS and "<script" not in s["html"]
               for s in templates.SEED_TEMPLATES)


def _run_tool(name: str, args: Any, conv: Any, rows: list[ChatTemplate], monkeypatch: pytest.MonkeyPatch,
              item: Any = None) -> tuple[dict[str, Any], FakeSession]:
    async def scope(_s: Any, _owner: int) -> WorkspaceContext:
        return SCOPE

    class Sess(FakeSession):
        async def scalar(self, stmt: Any) -> Any:
            text = str(stmt)
            if "chat_conversations" in text:
                return conv
            return item

    sess = Sess(rows)
    monkeypatch.setattr("modules.chat.scope.owner_default_scope", scope)
    raw = args if isinstance(args, str) else json.dumps(args)
    return json.loads(asyncio.run(templates.run_template_tool(name, raw, sess, CONV))), sess


def _conv(pending: str | None = None) -> Any:
    return SimpleNamespace(workspace_id=WS, actor_user_id=1, pending_template=pending)


def test_tool_list_includes_seeds_without_html(monkeypatch: pytest.MonkeyPatch) -> None:
    mine = _row("mine")
    out, _ = _run_tool("list_templates", {}, _conv("seed-kpi-q1"), [mine], monkeypatch)
    assert [t["id"] for t in out["templates"]][:3] == [s["id"] for s in templates.SEED_TEMPLATES]
    assert out["templates"][-1]["name"] == "mine" and out["pending_template"] == "seed-kpi-q1"
    assert all("html" not in t for t in out["templates"])


def test_model_cannot_save_templates(monkeypatch: pytest.MonkeyPatch) -> None:
    """save_template is no longer a model tool: injected text cannot persist widget HTML."""
    out, sess = _run_tool("save_template", {"name": "a", "html": "<p/>"}, _conv(), [], monkeypatch)
    assert "Unknown template tool" in out["error"] and sess.added == []
    assert "save_template" not in TEMPLATE_TOOLS
    names = {s["function"]["name"] for s in build_chat_tool_specs(include_read_skill=False, include_templates=True)}
    assert "save_template" not in names
    assert "save_template" not in build_generative_ui_system_prompt(locale="en-US", templates=True)


def test_tool_apply_consumes_pending_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    conv = _conv("seed-kpi-q1")
    out, sess = _run_tool("apply_template", {}, conv, [], monkeypatch)
    assert out["template"]["id"] == "seed-kpi-q1" and "Revenue" in out["template"]["html"]
    assert conv.pending_template is None and sess.commits == 1


def test_tool_apply_without_id_or_pending_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    out, _ = _run_tool("apply_template", {}, _conv(), [], monkeypatch)
    assert "error" in out
    out, _ = _run_tool("apply_template", {"template_id": str(uuid4())}, _conv(), [], monkeypatch)
    assert out["error"] == "Template not found"  # unknown/foreign id


def test_model_cannot_delete_templates(monkeypatch: pytest.MonkeyPatch) -> None:
    mine = _row()
    out, sess = _run_tool("delete_template", {"template_id": str(mine.id)}, _conv(), [], monkeypatch, item=mine)
    assert "Unknown template tool" in out["error"] and sess.deleted == []
    assert "delete_template" not in TEMPLATE_TOOLS
    names = {s["function"]["name"] for s in build_chat_tool_specs(include_read_skill=False, include_templates=True)}
    assert "delete_template" not in names


def test_owner_save_has_no_provenance() -> None:
    assert _save(FakeSession()).source_response_id is None


def test_save_takes_the_per_owner_advisory_lock_before_counting() -> None:
    session = FakeSession()
    _save(session)
    (sql, params), = session.executed
    assert "pg_advisory_xact_lock" in sql and params == {"key": f"chat.templates:{WS}:1"}


def test_apply_by_name_picks_most_recent_match_then_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    newest, older = _row("Plan"), _row("plan")
    out, _ = _run_tool("apply_template", {"name": "PLAN"}, _conv(), [newest, older], monkeypatch)  # list is newest first
    assert out["template"]["id"] == str(newest.id)
    out, _ = _run_tool("apply_template", {"name": "q1 kpi dashboard"}, _conv(), [], monkeypatch)
    assert out["template"]["id"] == "seed-kpi-q1"
    out, _ = _run_tool("apply_template", {"name": "nothing"}, _conv(), [], monkeypatch)
    assert out["error"] == "Template not found"


def test_tool_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    conv = _conv("x")
    assert _run_tool("clear_pending_template", {}, conv, [], monkeypatch)[0] == {"cleared": True}
    assert conv.pending_template is None


def test_specs_and_prompt_are_opt_in() -> None:
    names = lambda **kw: {s["function"]["name"] for s in build_chat_tool_specs(include_read_skill=False, **kw)}
    assert not (TEMPLATE_TOOLS | {TOOL_SET_THEME}) & names()
    assert TEMPLATE_TOOLS <= names(include_templates=True) and TOOL_SET_THEME not in names(include_templates=True)
    assert TOOL_SET_THEME in names(include_theme=True)
    base = build_generative_ui_system_prompt(locale="en-US")
    assert "apply_template" not in base and "set_theme" not in base
    assert "apply_template" in build_generative_ui_system_prompt(locale="en-US", templates=True)
    assert "set_theme" in build_generative_ui_system_prompt(locale="en-US", theme=True)


class ThemeSession(FakeSession):
    """Records the appended stream event."""

    async def scalar(self, _stmt: Any) -> int:
        return 4  # current max seq


def test_set_theme_appends_ui_theme_event() -> None:
    session = ThemeSession()
    run = uuid4()
    out = json.loads(asyncio.run(ui_theme.publish_theme(session, run, '{"mode": "dark"}')))
    assert out["status"] == "requested" and out["mode"] == "dark"
    (event,) = session.added
    assert event.event_type == "ui.theme" and event.data == {"mode": "dark"} and event.seq == 5


@pytest.mark.parametrize("raw", ['{"mode": "blue"}', "{bad", '{"mode": "dark", "x": 1}', ""])
def test_set_theme_invalid_args_append_nothing(raw: str) -> None:
    session = ThemeSession()
    assert "error" in json.loads(asyncio.run(ui_theme.publish_theme(session, uuid4(), raw)))
    assert session.added == []


def _deps(route: Any) -> set[Any]:
    found, stack = set(), [route.dependant]
    while stack:
        d = stack.pop()
        found.add(d.call)
        stack.extend(d.dependencies)
    return found


def test_routes_registered_with_account_auth() -> None:
    from modules.chat import templates_routes
    from modules.chat.routes import _account_read, _account_write

    routes = {(m, r.path): r for r in templates_routes.router.routes for m in r.methods}
    assert _account_read in _deps(routes[("GET", "/api/v1/chat/templates")])
    for key in (("POST", "/api/v1/chat/templates"), ("DELETE", "/api/v1/chat/templates/{template_id}"),
                ("PUT", "/api/v1/conversations/{conversation_id}/pending-template"),
                ("DELETE", "/api/v1/conversations/{conversation_id}/pending-template")):
        assert _account_write in _deps(routes[key]), key


def test_routes_mounted_in_app_and_error_mapping() -> None:
    from modules.chat import templates_routes

    assert templates_routes._http(templates.TemplateError("not_found")).status_code == 404
    assert templates_routes._http(templates.TemplateError("limit")).status_code == 409
    assert templates_routes._http(templates.TemplateError("invalid")).status_code == 422
    assert isinstance(UUID(str(uuid4())), UUID)
