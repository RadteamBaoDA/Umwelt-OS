"""Search releases its pooled connection across the embedding call only when opted in."""

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from core.tools.schemas import ToolExecutionPrincipal
from modules.search import public
from modules.search.schemas import SearchRequest
from modules.tools import builtins


class _Session:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    async def scalar(self, *_a: Any, **_k: Any) -> Any:
        return SimpleNamespace(
            dimensions=2, model_id="m", model_version="1", gateway_identity="g", response_model_id="m",
        )

    async def commit(self) -> None:
        self.log.append("commit")

    async def rollback(self) -> None:
        self.log.append("rollback")


def _setup(monkeypatch: pytest.MonkeyPatch, log: list[str], *, revision_changes: bool = False) -> None:
    config = SimpleNamespace(
        endpoint_policy_denied=False, gateway_identity="g", configuration_revision=1,
        endpoint_destination_id=None, omniroute_api_key="k",
    )
    mapping = SimpleNamespace(model="m", version="1")
    policy = SimpleNamespace(embeddings_allowed=True)
    calls = {"n": 0}

    async def configured(*_a: Any) -> Any:
        calls["n"] += 1
        log.append("config")
        cfg = config
        if revision_changes and calls["n"] > 1:
            cfg = SimpleNamespace(**{**vars(config), "configuration_revision": 2})
        return cfg, mapping, policy

    class _Gateway:
        def __init__(self, recheck: Any) -> None:
            self.recheck = recheck

        async def embed(self, *_a: Any) -> Any:
            await self.recheck()
            log.append("embed")
            return object()

    async def lexical(*_a: Any) -> list[Any]:
        return []

    async def vector(*_a: Any) -> list[Any]:
        log.append("vector")
        return []

    async def revalidate(*_a: Any) -> set[Any]:
        return set()

    monkeypatch.setattr(public, "configured_embedding", configured)
    monkeypatch.setattr(public, "may_send", lambda *_a: True)
    monkeypatch.setattr(public, "gateway", lambda _c, _r, recheck: _Gateway(recheck))
    monkeypatch.setattr(public, "embedding_values", lambda *_a: ([0.1, 0.2], "m"))
    monkeypatch.setattr(public, "_lexical_ids", lexical)
    monkeypatch.setattr(public, "_vector_ids", vector)
    monkeypatch.setattr(public, "_revalidate_tool_result_fences", revalidate)


async def _run(log: list[str], **kwargs: Any) -> Any:
    request = SearchRequest(query="q", mode="hybrid")
    return await public.search(_Session(log), None, None, request, **kwargs)  # type: ignore[arg-type]


async def test_flag_off_keeps_old_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    _setup(monkeypatch, log)
    await _run(log)
    assert log == ["config", "config", "embed", "vector"]


async def test_flag_on_commits_before_embed_and_after_recheck(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    _setup(monkeypatch, log)
    await _run(log, release_during_embed=True)
    # commit before gateway, commit at the end of recheck_send, nothing open at "embed"
    assert log == ["config", "commit", "config", "commit", "embed", "vector"]


async def test_recheck_still_runs_after_commit_and_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    _setup(monkeypatch, log, revision_changes=True)
    result = await _run(log, release_during_embed=True)
    assert result.effective_mode == "lexical" and result.warnings  # denied -> lexical fallback
    assert "embed" not in log
    assert log == ["config", "commit", "config"]


async def test_before_embedding_send_runs_before_final_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    _setup(monkeypatch, log)

    async def before(*_a: Any) -> None:
        log.append("before_send")

    await _run(log, release_during_embed=True, before_embedding_send=before)
    assert log == ["config", "commit", "config", "before_send", "commit", "embed", "vector"]


async def test_hydration_and_revalidation_still_filter_after_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hit is dropped when post-commit revalidation no longer finds it, kept when it does."""
    chunk_id = uuid4()
    row = (
        chunk_id, "content", uuid4(), 1, None, uuid4(), "t", None, None, "text/plain", None,
        uuid4(), "s", "note", 1,
    )

    class _Result:
        def all(self) -> list[Any]:
            return [row]

    class _HydratingSession(_Session):
        async def execute(self, *_a: Any, **_k: Any) -> Any:
            return _Result()

    async def vector(*_a: Any) -> list[Any]:
        return [chunk_id]

    for revalidated, expected in ((set(), 0), ({chunk_id}, 1)):
        log: list[str] = []
        _setup(monkeypatch, log)
        monkeypatch.setattr(public, "_vector_ids", vector)

        async def revalidate(*_a: Any, keep: set[Any] = revalidated) -> set[Any]:
            return keep

        monkeypatch.setattr(public, "_revalidate_tool_result_fences", revalidate)
        request = SearchRequest(query="q", mode="hybrid")
        result = await public.search(
            _HydratingSession(log), None, None, request, release_during_embed=True,  # type: ignore[arg-type]
        )
        assert len(result.items) == expected
        assert "embed" in log


async def test_route_passes_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from modules.search import routes

    seen: dict[str, Any] = {}

    async def recorder(*_a: Any, **kwargs: Any) -> str:
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(public, "search", recorder)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None, settings=None)))
    out = await routes.search(SearchRequest(query="q"), request, object(), object())  # type: ignore[arg-type]
    assert out == "ok" and seen["release_during_embed"] is True


@pytest.mark.parametrize("own_session", [True, False])
async def test_tool_handler_flag_follows_session_ownership(
    monkeypatch: pytest.MonkeyPatch, own_session: bool,
) -> None:
    """Handler opts in only for a session it created itself."""
    from modules.sources import public as sources

    source_id = uuid4()
    seen: dict[str, Any] = {}

    class _Factory:
        def __call__(self) -> "_Factory":
            return self

        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_a: object) -> None:
            return None

    async def get_tool_source(*_a: Any, **_k: Any) -> Any:
        return SimpleNamespace(generation=1)

    async def recorder(*_a: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return SimpleNamespace(items=[], model_dump=lambda **_k: {})

    monkeypatch.setattr(sources, "get_tool_source", get_tool_source)
    monkeypatch.setattr(public, "search", recorder)
    context: dict[str, Any] = {
        "principal": ToolExecutionPrincipal(
            actor_id="a", is_owner=True, source_ids=frozenset({str(source_id)}),
        ),
        "destination_kind": "local", "session_factory": _Factory(), "redis": None, "settings": None,
    }
    if not own_session:
        context["session"] = object()
    await builtins._handle_search_query({"query": "q"}, context)
    assert seen["release_during_embed"] is own_session
