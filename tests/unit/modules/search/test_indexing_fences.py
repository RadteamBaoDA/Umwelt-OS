"""Search workspace-scope fences: original authority, no transaction across embed, fair scan.

Covers W2-D-SRCH findings: transport without open transactions, original access/config
comparison under the generation mutex, fair skipping of denied subjects, Redis-loss cursor
wrap, and the no-share rule (membership alone never reaches content, snippets or counts).
"""

from types import SimpleNamespace
from typing import Any, Self
from uuid import uuid4

import pytest
from fastapi import HTTPException
from redis.exceptions import RedisError

from core.model_gateway.schemas import ModelMapping
from core.workspaces import public as workspaces_public
from core.workspaces.schemas import AccessFence, InternalJobScope, WorkspaceContext
from modules.knowledge.documents import public as documents_public
from modules.search import indexing, public
from modules.settings import public as settings_public

WORKSPACE = uuid4()
FENCE = AccessFence(workspace_id=WORKSPACE, user_id=1, membership_revision=2, configuration_revision=3)
MAPPING = ModelMapping(model="embed", version="1", destination="remote")


def _config(revision: int = 5) -> Any:
    return SimpleNamespace(
        configuration_revision=revision, gateway_identity="g" * 64,
        endpoint_destination_id="dest", endpoint_policy_denied=False, omniroute_api_key="k",
    )


def _authority(revision: int = 5) -> indexing.EmbeddingAuthority:
    return indexing.EmbeddingAuthority(
        FENCE, _config(revision), MAPPING, SimpleNamespace(embeddings_allowed=True),  # type: ignore[arg-type]
    )


# ---- no-share rule -------------------------------------------------------------------------


class _ExplodingSession:
    """Any database touch after a (failed) admission is a leak."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"session.{name} used before owner admission")


@pytest.mark.parametrize("call", ["search", "index_status", "tasks_goals", "news", "fences"])
async def test_member_without_share_gets_nothing(call: str) -> None:
    """A member (membership, no share) is rejected before any query, so no hit/count/snippet exists."""
    member = WorkspaceContext(user_id=9, workspace_id=WORKSPACE, role="member", membership_revision=1)
    session: Any = _ExplodingSession()
    from modules.goals.schemas import GoalFilter
    from modules.search.schemas import SearchRequest
    from modules.tasks.schemas import TaskFilter

    with pytest.raises(HTTPException) as caught:
        if call == "search":
            await public.search(session, None, None, SearchRequest(query="secret"),  # type: ignore[arg-type]
                                scope=member, multi_workspace_enabled=True)
        elif call == "index_status":
            await public.index_status(session, scope=member, multi_workspace_enabled=True,
                                      settings=None, redis=None)  # type: ignore[arg-type]
        elif call == "tasks_goals":
            await public.search_tasks_and_goals(session, TaskFilter(q="x"), GoalFilter(q="x"),
                                                scope=member, multi_workspace_enabled=True)
        elif call == "news":
            await public.compare_news_evidence_embeddings(session, uuid4(), (), scope=member,
                                                          multi_workspace_enabled=True)
        else:
            await public.revalidate_tool_search_fences(session, [], source_ids=frozenset(), scope=member,
                                                       multi_workspace_enabled=True)
    assert caught.value.status_code == 403


async def test_indexing_admit_rejects_member_for_both_modes() -> None:
    member = WorkspaceContext(user_id=9, workspace_id=WORKSPACE, role="member", membership_revision=1)
    for lock in (False, True):
        with pytest.raises(HTTPException) as caught:
            await indexing._admit(_ExplodingSession(), scope=member, multi_workspace_enabled=True,  # type: ignore[arg-type]
                                  lock=lock)
        assert caught.value.status_code == 403


# ---- F5: original authority compared; nonlocking admit honors expected ----------------------


async def test_nonlocking_admit_compares_expected(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = WorkspaceContext(user_id=1, workspace_id=WORKSPACE, role="owner", membership_revision=2)
    changed = AccessFence(workspace_id=WORKSPACE, user_id=1, membership_revision=3, configuration_revision=3)

    async def read(*_a: Any, **_k: Any) -> AccessFence:
        return changed

    monkeypatch.setattr(workspaces_public, "read_access_fence", read)
    with pytest.raises(HTTPException) as caught:
        await indexing._admit(None, scope=scope, multi_workspace_enabled=True, expected=FENCE)  # type: ignore[arg-type]
    assert caught.value.status_code == 409
    assert await indexing._admit(None, scope=scope, multi_workspace_enabled=True) == changed  # type: ignore[arg-type]


def _patch_publication(monkeypatch: pytest.MonkeyPatch, *, config_revision: int, events: list[str]) -> None:
    async def admit(_s: Any, **kwargs: Any) -> AccessFence:
        events.append(f"admit lock={kwargs['lock']} expected={kwargs['expected'] is FENCE}")
        return FENCE

    async def mutex(*_a: Any, **_k: Any) -> None:
        events.append("mutex")

    async def enabled(*_a: Any, **_k: Any) -> bool:
        return True

    async def configured(*_a: Any, **_k: Any) -> Any:
        events.append("config")
        return _config(config_revision), MAPPING, SimpleNamespace(embeddings_allowed=True)

    async def dedupe(*_a: Any, **kwargs: Any) -> Any:
        events.append(f"dedupe retry={kwargs['honor_retry_delay']}")
        return "generation"

    monkeypatch.setattr(indexing, "_admit", admit)
    monkeypatch.setattr(indexing, "_take_generation_mutex", mutex)
    monkeypatch.setattr(settings_public, "module_is_enabled", enabled)
    monkeypatch.setattr(indexing, "configured_embedding", configured)
    monkeypatch.setattr(indexing, "_dedupe_or_create", dedupe)
    monkeypatch.setattr(indexing, "may_send", lambda *_a, **_k: True)


SCOPE = InternalJobScope(workspace_id=WORKSPACE, actor_user_id=1, membership_revision=2)
SETTINGS: Any = SimpleNamespace(multi_workspace_enabled=True)


async def test_publication_compares_original_under_mutex(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    _patch_publication(monkeypatch, config_revision=5, events=events)
    result = await indexing.create_generation_for_authority(
        None, _authority(), SETTINGS, None, scope=SCOPE, honor_retry_delay=True,  # type: ignore[arg-type]
    )
    assert result == "generation"  # type: ignore[comparison-overlap]
    # locked admission against the ORIGINAL fence, then mutex, then config, then retry decision.
    assert events == ["admit lock=True expected=True", "mutex", "config", "dedupe retry=True"]


async def test_publication_aborts_when_config_changed_under_mutex(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    _patch_publication(monkeypatch, config_revision=6, events=events)
    with pytest.raises(HTTPException) as caught:
        await indexing.create_generation_for_authority(
            None, _authority(5), SETTINGS, None, scope=SCOPE,  # type: ignore[arg-type]
        )
    assert caught.value.status_code == 409
    assert not any(event.startswith("dedupe") for event in events)


async def test_retry_delay_decided_inside_dedupe() -> None:
    """The failed-run decision lives in the post-mutex helper (returns None, no row written)."""
    from datetime import UTC, datetime

    failed = SimpleNamespace(status="failed", model_id="embed", model_version="1",
                             gateway_identity="g" * 64, updated_at=datetime.now(UTC))

    class Session:
        rolled = False

        async def scalar(self, _stmt: Any) -> Any:
            return failed

        async def rollback(self) -> None:
            self.rolled = True

    session = Session()
    result = await indexing._dedupe_or_create(
        session, MAPPING, "g" * 64, scope=SCOPE, multi_workspace_enabled=True,  # type: ignore[arg-type]
        access_fence=FENCE, honor_retry_delay=True,
    )
    assert result is None
    assert session.rolled


# ---- F4: no open transaction during embed --------------------------------------------------


class _Factory:
    """Session factory that counts simultaneously open sessions and scripts scalar results."""

    def __init__(self, scripts: list[list[Any]], rows: list[Any]) -> None:
        self.scripts, self.rows, self.open, self.created = scripts, rows, 0, 0

    def __call__(self) -> Any:
        factory = self
        script = self.scripts[self.created]
        self.created += 1

        class Session:
            def __init__(self) -> None:
                self.results = list(script)

            async def __aenter__(self) -> Self:
                factory.open += 1
                return self

            async def __aexit__(self, *_a: object) -> None:
                factory.open -= 1

            async def scalar(self, _stmt: Any) -> Any:
                return self.results.pop(0)

            async def execute(self, _stmt: Any, *_params: Any) -> Any:
                rows = factory.rows
                return SimpleNamespace(first=lambda: rows.pop(0) if rows else None)

            async def rollback(self) -> None:
                return None

            def add(self, _item: Any) -> None:
                return None

            async def flush(self) -> None:
                return None

        return Session()


def _patch_generation_run(monkeypatch: pytest.MonkeyPatch, factory: _Factory, *, stale: bool) -> list[int]:
    seen_open: list[int] = []
    chunk_id, source_id = uuid4(), uuid4()
    factory.rows.append((chunk_id, "chunk text", source_id))

    async def owner(*_a: Any, **_k: Any) -> Any:
        return SimpleNamespace(user_id=1, membership_revision=2)

    async def capture(*_a: Any, **_k: Any) -> indexing.EmbeddingAuthority:
        return _authority()

    async def enabled(*_a: Any, **_k: Any) -> bool:
        return True

    async def admit(*_a: Any, **_k: Any) -> AccessFence:
        return FENCE

    async def projection(*_a: Any, **_k: Any) -> None:
        return None

    async def commit(*_a: Any, **_k: Any) -> None:
        seen_open.append(-1)  # marks a commit; asserted only for ordering below

    calls = {"recheck": 0}

    async def recheck(*_a: Any, **kwargs: Any) -> tuple[AccessFence, bool, int | None]:
        calls["recheck"] += 1
        if stale and kwargs["lock"]:
            raise indexing._AuthorityChanged
        return FENCE, True, 3

    class FakeGateway:
        async def embed(self, *_a: Any, **_k: Any) -> dict[str, Any]:
            seen_open.append(factory.open)
            return {"data": [{"embedding": [1.0, 2.0]}], "model": "embed"}

    monkeypatch.setattr(workspaces_public, "resolve_workspace_owner_context", owner)
    monkeypatch.setattr(indexing, "capture_authority", capture)
    monkeypatch.setattr(settings_public, "module_is_enabled", enabled)
    monkeypatch.setattr(indexing, "_admit", admit)
    monkeypatch.setattr(indexing, "_index_projection", projection)
    monkeypatch.setattr(indexing, "_commit_index_change", commit)
    monkeypatch.setattr(indexing, "_recheck_prepared", recheck)
    monkeypatch.setattr(indexing, "gateway", lambda *_a, **_k: FakeGateway())
    return seen_open


def _generation() -> Any:
    return SimpleNamespace(
        model_id="embed", model_version="1", gateway_identity="g" * 64, status="active",
        dimensions=None, response_model_id=None, error_code=None,
    )


async def test_embed_runs_with_no_open_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    generation = _generation()
    item = SimpleNamespace(id=uuid4(), generation_id=uuid4(), status="pending", error_code=None)
    scripts = [
        [generation],                 # preparation snapshot
        [generation, None],           # claim: locked generation, no item yet
        [],                           # short pre-send preparation (patched recheck)
        [generation, item],           # publication
        [generation],                 # second claim iteration: no rows left
    ]
    factory = _Factory(scripts, [])
    seen = _patch_generation_run(monkeypatch, factory, stale=False)

    class Sess:  # execute() for the UPDATE statement returns nothing useful
        pass

    done = await indexing._index_generation(factory, None, SETTINGS, uuid4(), WORKSPACE)  # type: ignore[arg-type]
    assert done == 1
    assert [n for n in seen if n >= 0] == [0], "embed ran while a database session was open"


async def test_stale_authority_after_embed_discards_output(monkeypatch: pytest.MonkeyPatch) -> None:
    generation = _generation()
    scripts = [[generation], [generation, None], [], []]
    factory = _Factory(scripts, [])
    seen = _patch_generation_run(monkeypatch, factory, stale=True)
    done = await indexing._index_generation(factory, None, SETTINGS, uuid4(), WORKSPACE)  # type: ignore[arg-type]
    assert done == 0
    assert 0 in seen and seen.count(-1) == 1  # embed ran closed; only the claim committed


async def test_recheck_prepared_rejects_changed_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    other = AccessFence(workspace_id=WORKSPACE, user_id=1, membership_revision=9, configuration_revision=3)

    async def admit(*_a: Any, **_k: Any) -> AccessFence:
        return other

    monkeypatch.setattr(indexing, "_admit", admit)
    with pytest.raises(indexing._AuthorityChanged):
        await indexing._recheck_prepared(
            None, scope=SCOPE, settings=SETTINGS, redis=None, original=_authority(),  # type: ignore[arg-type]
            source_id=uuid4(), source_generation=None, chunk_id=uuid4(), content="x",
            workspace_id=WORKSPACE, lock=False,
        )


# ---- F6/F7: fair scan and Redis-loss cursor --------------------------------------------------


class _DownRedis:
    async def get(self, *_a: Any) -> None:
        raise RedisError("down")

    async def set(self, *_a: Any) -> None:
        raise RedisError("down")

    async def delete(self, *_a: Any) -> None:
        raise RedisError("down")


async def test_cursor_falls_back_to_local_state_when_redis_down() -> None:
    state: dict[str, str] = {}
    first = uuid4()
    await indexing._write_cursor(_DownRedis(), state, "search_discovery", "k", first)  # type: ignore[arg-type]
    assert await indexing._read_cursor(_DownRedis(), state, "search_discovery", "k") == first  # type: ignore[arg-type]
    await indexing._write_cursor(_DownRedis(), state, "search_discovery", "k", None)  # type: ignore[arg-type]
    assert await indexing._read_cursor(_DownRedis(), state, "search_discovery", "k") is None  # type: ignore[arg-type]


async def test_denied_oldest_row_does_not_block_next(monkeypatch: pytest.MonkeyPatch) -> None:
    denied, eligible = (uuid4(), uuid4()), (uuid4(), uuid4())
    attempts: list[Any] = []

    async def noop(*_a: Any, **_k: Any) -> None:
        return None

    async def page(_f: Any, statuses: tuple[str, ...], _after: Any) -> list[tuple[Any, Any]]:
        return [denied, eligible] if statuses == ("queued", "running") else []

    async def index(_f: Any, _r: Any, _s: Any, generation_id: Any, _w: Any) -> int | None:
        attempts.append(generation_id)
        return None if generation_id == denied[0] else 4

    class Session:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

    monkeypatch.setattr(documents_public, "backfill_current_chunks", noop)
    monkeypatch.setattr(indexing, "_reconcile_automatic_generations", noop)
    monkeypatch.setattr(indexing, "_candidate_page", page)
    monkeypatch.setattr(indexing, "_index_generation", index)
    ctx: dict[str, object] = {"session_factory": Session, "redis": _DownRedis(), "settings": SETTINGS}
    done = await indexing.index_pending_chunks.__wrapped__(ctx) if hasattr(
        indexing.index_pending_chunks, "__wrapped__") else await indexing.index_pending_chunks(ctx)
    assert done == 4
    assert attempts == [denied[0], eligible[0]]
    # cursor advanced to the processed subject and survives Redis loss in the shared state.
    assert ctx["w2_cursor_state"] == {"search_queued": str(eligible[1])}
