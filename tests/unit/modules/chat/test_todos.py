"""Unit tests for conversation todos: service semantics, tool runner, specs and routes (fake session)."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

import core.workspaces.models  # noqa: F401  (registers the workspaces table for FK resolution)
from core.workspaces.schemas import WorkspaceContext
from modules.chat import todos
from modules.chat.generative_ui.prompt import build_generative_ui_system_prompt
from modules.chat.generative_ui.tool_specs import (
    TOOL_GET_TODOS,
    TOOL_MANAGE_TODOS,
    build_chat_tool_specs,
)
from modules.chat.models import ChatTodo

WS = uuid4()
SCOPE = WorkspaceContext(user_id=1, workspace_id=WS, role="owner", membership_revision=1)
CONV = uuid4()


class FakeSession:
    """Minimal AsyncSession stand-in recording adds/deletes and serving canned rows."""

    def __init__(self, existing: list[ChatTodo] | None = None, foreign: set[UUID] | None = None) -> None:
        self.existing = existing or []
        self.foreign = foreign or set()
        self.added: list[ChatTodo] = []
        self.deleted: list[ChatTodo] = []
        self.commits = 0
        self.rollbacks = 0

    def add(self, row: ChatTodo) -> None:
        self.added.append(row)

    async def delete(self, row: ChatTodo) -> None:
        self.deleted.append(row)

    async def flush(self) -> None:
        return None

    async def scalars(self, _stmt: Any) -> list[UUID]:
        return list(self.foreign)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


_REAL_CONVERSATION = todos._conversation  # captured before the autouse fixture swaps it

@pytest.fixture(autouse=True)
def _patch(monkeypatch: pytest.MonkeyPatch) -> None:
    async def conv(session: Any, scope: Any, conversation_id: UUID, *, lock: bool = False) -> Any:
        if scope.workspace_id != WS or conversation_id != CONV:
            raise todos.TodoError("not_found")
        return SimpleNamespace(workspace_id=WS, actor_user_id=1)

    async def rows(session: FakeSession, conversation_id: UUID) -> list[ChatTodo]:
        return list(session.existing)

    monkeypatch.setattr(todos, "_conversation", conv)
    monkeypatch.setattr(todos, "_rows", rows)


def _item(title: str, **kw: Any) -> todos.TodoItem:
    return todos.TodoItem(title=title, **kw)


def _replace(session: FakeSession, items: list[todos.TodoItem], scope: WorkspaceContext = SCOPE) -> list[ChatTodo]:
    return asyncio.run(todos.replace_todos(session, scope, CONV, items))


def test_replace_assigns_ids_and_stable_order() -> None:
    rows = _replace(FakeSession(), [_item("a"), _item("b", status="completed"), _item("c")])
    assert [r.title for r in rows] == ["a", "b", "c"] and [r.position for r in rows] == [0, 1, 2]
    assert len({r.id for r in rows}) == 3 and rows[1].status == "completed"


def test_replace_keeps_valid_existing_id_and_deletes_missing() -> None:
    keep, drop = ChatTodo(id=uuid4(), title="old", position=0), ChatTodo(id=uuid4(), title="gone", position=1)
    session = FakeSession([keep, drop])
    rows = _replace(session, [_item("new", id=str(keep.id))])
    assert rows == [keep] and keep.title == "new" and session.deleted == [drop] and not session.added


def test_replace_regenerates_malformed_duplicate_and_foreign_ids() -> None:
    foreign, dup = uuid4(), uuid4()
    session = FakeSession(foreign={foreign})
    rows = _replace(session, [_item("x", id="not-a-uuid"), _item("y", id=str(foreign)),
                              _item("z", id=str(dup)), _item("w", id=str(dup))])
    ids = [r.id for r in rows]
    assert len(set(ids)) == 4 and foreign not in ids and ids.count(dup) == 1


def test_replace_bounds_and_validation() -> None:
    with pytest.raises(todos.TodoError):
        _replace(FakeSession(), [_item("t")] * 51)
    assert len(_replace(FakeSession(), [_item(f"t{i}") for i in range(50)])) == 50
    for bad in ({"title": ""}, {"title": "x" * 201}, {"title": "a", "description": "d" * 1001},
                {"title": "a", "emoji": "e" * 9}, {"title": "a", "status": "done"}):
        with pytest.raises(ValueError):
            todos.TodoItem(**bad)


def test_foreign_workspace_is_denied() -> None:
    other = WorkspaceContext(user_id=2, workspace_id=uuid4(), role="owner", membership_revision=1)
    with pytest.raises(todos.TodoError) as err:
        _replace(FakeSession(), [_item("a")], other)
    assert err.value.code == "not_found"


def _run_tool(name: str, args: Any, session: FakeSession, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    async def scope(_s: Any, _owner: int) -> WorkspaceContext:
        return SCOPE

    class Sess(FakeSession):
        async def scalar(self, _stmt: Any) -> Any:
            return SimpleNamespace(workspace_id=WS, actor_user_id=1)

    sess = Sess(session.existing)
    monkeypatch.setattr("modules.chat.scope.owner_default_scope", scope)
    raw = args if isinstance(args, str) else json.dumps(args)
    out = json.loads(asyncio.run(todos.run_todo_tool(name, raw, sess, CONV)))
    out["_commits"], out["_rollbacks"] = sess.commits, sess.rollbacks
    return out


def test_tool_runner_manage_and_get(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _run_tool("manage_todos", {"todos": [{"title": "a", "emoji": "x"}]}, FakeSession(), monkeypatch)
    assert out["todos"][0]["title"] == "a" and out["_commits"] == 1
    row = ChatTodo(id=uuid4(), title="t", description="", emoji="", status="pending", position=0)
    assert _run_tool("get_todos", {}, FakeSession([row]), monkeypatch)["todos"][0]["title"] == "t"


@pytest.mark.parametrize("args", ["{bad", {"todos": "no"}, {"todos": [{"title": ""}]}, {}])
def test_tool_runner_errors_are_data(args: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    out = _run_tool("manage_todos", args, FakeSession(), monkeypatch)
    assert "error" in out and out["_rollbacks"] == 1 and out["_commits"] == 0
    assert "error" in _run_tool("nope", {}, FakeSession(), monkeypatch)


def test_specs_and_prompt_are_opt_in() -> None:
    names = lambda **kw: {s["function"]["name"] for s in build_chat_tool_specs(include_read_skill=False, **kw)}
    assert not {TOOL_MANAGE_TODOS, TOOL_GET_TODOS} & names()
    assert {TOOL_MANAGE_TODOS, TOOL_GET_TODOS} <= names(include_todos=True)
    assert "manage_todos" not in build_generative_ui_system_prompt(locale="en-US")
    assert "manage_todos" in build_generative_ui_system_prompt(locale="en-US", todos=True)


def test_routes_registered_with_account_auth() -> None:
    from modules.chat import todos_routes
    from modules.chat.routes import _account_read, _account_write

    def deps(route: Any) -> set[Any]:
        found, stack = set(), [route.dependant]
        while stack:
            d = stack.pop()
            found.add(d.call)
            stack.extend(d.dependencies)
        return found

    by_method = {next(iter(r.methods)): r for r in todos_routes.router.routes}
    assert _account_read in deps(by_method["GET"])
    assert _account_write in deps(by_method["PUT"]) and _account_write in deps(by_method["PATCH"])


@pytest.mark.asyncio
async def test_conversation_lookup_is_owner_scoped() -> None:
    """The real ``_conversation`` query filters by workspace AND owner, so another principal in the
    same workspace cannot reach the todo list (matches templates.py ownership)."""
    captured: list[Any] = []

    class Recorder:
        async def scalar(self, stmt: Any) -> None:
            captured.append(str(stmt.compile(compile_kwargs={"literal_binds": False})))

    with pytest.raises(todos.TodoError):
        await _REAL_CONVERSATION(Recorder(), SCOPE, CONV)  # type: ignore[arg-type]
    # Only the WHERE clause counts: the SELECT list names every column, which proves nothing.
    where = captured[0].split("WHERE", 1)[1]
    assert "actor_user_id" in where and "workspace_id" in where
