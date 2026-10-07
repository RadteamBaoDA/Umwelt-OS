"""Search releases its pooled connection across the embedding call only when opted in."""

from types import SimpleNamespace
from typing import Any

import pytest

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


def test_callers_pass_flag() -> None:
    """Lock-free callers opt in; the tool handler only for a session it created itself."""
    import inspect

    from modules.search import routes

    assert "release_during_embed=True" in inspect.getsource(routes.search)
    assert 'release_during_embed=context.get("session") is None' in inspect.getsource(
        builtins._handle_search_query,
    )
