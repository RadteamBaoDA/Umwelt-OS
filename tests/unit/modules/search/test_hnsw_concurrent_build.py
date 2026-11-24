"""HNSW build is CONCURRENT and runs after the heavy slot is released; activation only flips status."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Self
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from modules.search import indexing
from tests.unit.split_patching import patch_split

pytestmark = pytest.mark.asyncio


class _Result:
    def __init__(self, row: Any) -> None:
        self._row = row

    def first(self) -> Any:
        return self._row


class _Connection:
    """Autocommit connection fake recording statement text in order."""

    def __init__(
        self, log: list[str], *, invalid_leftover: bool = False, fail_create: bool = False
    ) -> None:
        self.log, self.invalid_leftover, self.fail_create = log, invalid_leftover, fail_create

    async def execution_options(self, **options: Any) -> _Connection:
        self.log.append(f"options:{options['isolation_level']}")
        return self

    async def execute(self, statement: Any, params: Any = None) -> _Result:
        sql = str(statement)
        self.log.append(
            sql.split(" ON ")[0] if sql.startswith("CREATE") else sql.split(" WHERE ")[0]
        )
        if sql.startswith("CREATE") and self.fail_create:
            raise RuntimeError("deadlock detected")
        return _Result(
            1 if self.invalid_leftover and sql.startswith("SELECT 1 FROM pg_index") else None
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _Engine:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    def connect(self) -> _Connection:
        return self._connection


async def test_statements_are_concurrent_idempotent_and_partial() -> None:
    generation_id = uuid4()
    drop, create = indexing.vector_index_statements(generation_id, 768)
    name = indexing.vector_index_name(generation_id)
    assert drop == f"DROP INDEX CONCURRENTLY IF EXISTS {name}"
    assert create.startswith(
        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON search_index_items USING hnsw"
    )
    assert "embedding::vector(768)" in create and f"generation_id = '{generation_id}'" in create
    with pytest.raises(ValueError):
        indexing.vector_index_statements(generation_id, indexing.MAX_VECTOR_DIMENSIONS + 1)


async def test_build_runs_on_autocommit_with_timeouts_lifted_and_reset() -> None:
    log: list[str] = []
    generation_id = uuid4()
    await indexing.build_vector_index_concurrently(_Engine(_Connection(log)), generation_id, 3)  # type: ignore[arg-type]
    name = indexing.vector_index_name(generation_id)
    assert log == [
        "options:AUTOCOMMIT",
        f"SET statement_timeout = {indexing.HNSW_BUILD_STATEMENT_TIMEOUT_MS}",
        "SET lock_timeout = 0",
        "SELECT pg_advisory_lock(hashtext(:name))",
        "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid",  # invalid-leftover probe
        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name}",
        "SELECT pg_advisory_unlock(hashtext(:name))",
        "RESET statement_timeout",
        "RESET lock_timeout",
    ]


async def test_invalid_leftover_from_an_interrupted_build_is_dropped_before_the_retry() -> None:
    log: list[str] = []
    generation_id = uuid4()
    await indexing.build_vector_index_concurrently(  # type: ignore[arg-type]
        _Engine(_Connection(log, invalid_leftover=True)), generation_id, 3
    )
    name = indexing.vector_index_name(generation_id)
    assert log.index(f"DROP INDEX CONCURRENTLY IF EXISTS {name}") < log.index(
        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name}"
    )


async def test_failed_build_drops_its_invalid_index_and_reraises() -> None:
    log: list[str] = []
    generation_id = uuid4()
    with pytest.raises(RuntimeError, match="deadlock"):
        await indexing.build_vector_index_concurrently(  # type: ignore[arg-type]
            _Engine(_Connection(log, fail_create=True)), generation_id, 3
        )
    name = indexing.vector_index_name(generation_id)
    assert log[-4:] == [
        f"DROP INDEX CONCURRENTLY IF EXISTS {name}",
        "SELECT pg_advisory_unlock(hashtext(:name))",
        "RESET statement_timeout",
        "RESET lock_timeout",
    ]


_ENGINE = create_async_engine(
    "postgresql+asyncpg://u:p@localhost/db"
)  # never connects; type is what matters


class _FinalizeSession:
    """Session fake for finalize: the prune DELETE is a no-op and the failed-item count is zero."""

    def __init__(self) -> None:
        self.bind = _ENGINE

    async def execute(self, statement: Any, params: Any = None) -> None:
        return None

    async def scalar(self, statement: Any) -> int:
        return 0


class _EngineSession:
    """Worker session factory product: an async context manager whose ``bind`` is an AsyncEngine."""

    bind = _ENGINE

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _ctx(generation_id: Any) -> Any:
    return SimpleNamespace(
        generation_id=generation_id,
        workspace_id=uuid4(),
        scope="scope",
        multi_workspace_enabled=False,
        original=SimpleNamespace(fence="FENCE"),
    )


async def test_finalize_without_a_valid_index_defers_the_build_and_stays_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(a) No valid index yet: commit the prune, keep the generation running, hand dimensions back."""
    log: list[str] = []
    generation = SimpleNamespace(id=uuid4(), status="running", dimensions=3, error_code=None)

    async def not_valid(*_a: Any, **_k: Any) -> bool:
        return False

    async def commit(*_a: Any, **_k: Any) -> None:
        log.append("commit")

    async def activate(*_a: Any, **_k: Any) -> None:
        log.append("activate")

    patch_split(monkeypatch, "modules.search.indexing", "vector_index_is_valid", not_valid)
    patch_split(monkeypatch, "modules.search.indexing", "_commit_index_change", commit)
    patch_split(monkeypatch, "modules.search.indexing", "activate_generation", activate)
    result = await indexing._finalize_exhausted_generation(
        _FinalizeSession(),  # type: ignore[arg-type]
        _ctx(generation.id),  # type: ignore[arg-type]
        generation,  # type: ignore[arg-type]
        None,
        "FENCE",  # type: ignore[arg-type]
    )
    assert result == 3
    assert generation.status == "running" and log == ["commit"]


async def test_finalize_with_a_valid_index_activates_in_the_same_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) A valid index already exists (built after an earlier slot release): activate now, build nothing."""
    log: list[str] = []
    generation = SimpleNamespace(id=uuid4(), status="running", dimensions=3, error_code=None)

    async def valid(*_a: Any, **_k: Any) -> bool:
        return True

    async def commit(*_a: Any, **_k: Any) -> None:
        log.append("commit")

    async def activate(*_a: Any, **_k: Any) -> None:
        log.append("activate")

    async def build(*_a: Any) -> None:
        log.append("build")

    patch_split(monkeypatch, "modules.search.indexing", "vector_index_is_valid", valid)
    patch_split(monkeypatch, "modules.search.indexing", "_commit_index_change", commit)
    patch_split(monkeypatch, "modules.search.indexing", "activate_generation", activate)
    patch_split(monkeypatch, "modules.search.indexing", "build_vector_index_concurrently", build)
    result = await indexing._finalize_exhausted_generation(
        _FinalizeSession(),  # type: ignore[arg-type]
        _ctx(generation.id),  # type: ignore[arg-type]
        generation,  # type: ignore[arg-type]
        None,
        "FENCE",  # type: ignore[arg-type]
    )
    assert result is None
    assert log == ["activate", "commit"]


class _Queue:
    """arq-style redis double: duplicate job ids return None, failures can be injected."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.fail, self.seen = fail, set()

    async def enqueue_job(self, *args: Any, **kwargs: Any) -> object | None:
        self.calls.append((args, kwargs))
        if self.fail:
            raise ConnectionError("redis down")
        job_id = kwargs["_job_id"]
        if job_id in self.seen:
            return None
        self.seen.add(job_id)
        return object()


async def test_pass_enqueues_one_deduplicated_build_job_per_generation_and_never_builds_inline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(c) The cron pass only enqueues: one job per generation under a dedupe id, no inline build."""
    first, second = uuid4(), uuid4()
    log: list[str] = []

    async def inner(_ctx: Any, builds: list[tuple[UUID, int]]) -> int:
        builds.extend([(first, 3), (second, 4)])
        return 0

    async def build(*_a: Any) -> None:
        log.append("build")

    patch_split(monkeypatch, "modules.search.indexing", "_index_pending_chunks_in_slot", inner)
    patch_split(monkeypatch, "modules.search.indexing", "build_vector_index_concurrently", build)
    queue = _Queue()
    ctx: dict[str, object] = {"session_factory": _EngineSession, "redis": queue}
    assert await indexing.index_pending_chunks(ctx) == 0  # type: ignore[arg-type]
    assert log == []
    assert [(c[0], c[1]["_job_id"], c[1]["_queue_name"]) for c in queue.calls] == [
        (("build_search_vector_index", str(first), 3), f"search-hnsw-build:{first}", "arq:queue"),
        (("build_search_vector_index", str(second), 4), f"search-hnsw-build:{second}", "arq:queue"),
    ]
    # A second tick while the first job is queued/running re-enqueues under the same id: arq says None.
    assert await indexing.index_pending_chunks(ctx) == 0  # type: ignore[arg-type]
    assert len(queue.calls) == 4 and queue.seen == {
        f"search-hnsw-build:{first}",
        f"search-hnsw-build:{second}",
    }


async def test_enqueue_failure_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """(c2) A failing enqueue must not fail the pass: the generation stays running, next tick retries."""
    generation_id = uuid4()

    async def inner(_ctx: Any, builds: list[tuple[UUID, int]]) -> int:
        builds.append((generation_id, 3))
        return 5

    patch_split(monkeypatch, "modules.search.indexing", "_index_pending_chunks_in_slot", inner)
    ctx: dict[str, object] = {"session_factory": _EngineSession, "redis": _Queue(fail=True)}
    with caplog.at_level("WARNING", logger="modules.search.indexing"):
        assert await indexing.index_pending_chunks(ctx) == 5  # type: ignore[arg-type]
    assert any(str(generation_id) in r.getMessage() for r in caplog.records)


async def test_pass_without_a_pending_build_enqueues_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(d) No generation finalized to a deferred build: nothing is enqueued."""

    async def inner(_ctx: Any, builds: list[tuple[UUID, int]]) -> int:
        return 2

    patch_split(monkeypatch, "modules.search.indexing", "_index_pending_chunks_in_slot", inner)
    queue = _Queue()
    ctx: dict[str, object] = {"session_factory": _EngineSession, "redis": queue}
    assert await indexing.index_pending_chunks(ctx) == 2  # type: ignore[arg-type]
    assert queue.calls == []


async def test_build_job_builds_then_wakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """(e1) The job builds on the session's engine, then wakes the poller so the next pass activates."""
    generation_id = uuid4()
    log: list[str] = []

    async def build(_engine: Any, gid: UUID, dimensions: int) -> None:
        log.append(f"build:{gid == generation_id}:{dimensions}")

    async def wake(_redis: Any, name: str, **_k: Any) -> bool:
        log.append(f"wake:{name}")
        return True

    patch_split(monkeypatch, "modules.search.indexing", "build_vector_index_concurrently", build)
    patch_split(monkeypatch, "modules.search.indexing", "wake", wake)
    ctx: dict[str, object] = {"session_factory": _EngineSession, "redis": object()}
    await indexing.build_search_vector_index(ctx, str(generation_id), 3)  # type: ignore[arg-type]
    assert log == ["build:True:3", "wake:index_pending_chunks"]


async def test_activate_generation_no_longer_creates_an_index_in_the_transaction() -> None:
    executed: list[str] = []

    class _Active:
        async def scalar(self, _statement: Any) -> Any:
            return SimpleNamespace(status="active")

        async def execute(self, statement: Any) -> None:
            executed.append(str(statement))

        async def flush(self) -> None:
            return None

    generation = SimpleNamespace(id=uuid4(), dimensions=3, status="running", error_code="x")
    await indexing.activate_generation(
        _Active(), generation, scope=SimpleNamespace(workspace_id=uuid4())
    )  # type: ignore[arg-type]
    assert executed == [] and generation.status == "active" and generation.error_code is None


async def test_engine_from_real_async_sessionmaker_is_an_async_engine() -> None:
    """Regression: ``AsyncEngine.engine`` is the sync Engine; the helper must hand back the AsyncEngine itself."""
    engine = create_async_engine("postgresql+asyncpg://u:p@localhost/db")
    async with async_sessionmaker(engine)() as session:
        resolved = indexing.async_engine_for_session(session)
    assert isinstance(resolved, AsyncEngine) and resolved is engine


async def test_engine_for_unbound_session_is_rejected() -> None:
    with pytest.raises(TypeError):
        indexing.async_engine_for_session(SimpleNamespace(bind=None))  # type: ignore[arg-type]


async def test_builders_of_one_generation_serialize_on_an_advisory_lock() -> None:
    """The lock is taken before the invalid-leftover probe and released after the create, so a second finalize
    cannot drop the first builder's in-progress index."""
    log: list[str] = []
    await indexing.build_vector_index_concurrently(_Engine(_Connection(log)), uuid4(), 3)  # type: ignore[arg-type]
    lock = log.index("SELECT pg_advisory_lock(hashtext(:name))")
    unlock = log.index("SELECT pg_advisory_unlock(hashtext(:name))")
    probe = next(i for i, e in enumerate(log) if e.startswith("SELECT 1 FROM pg_index"))
    create = next(i for i, e in enumerate(log) if e.startswith("CREATE INDEX"))
    assert lock < probe < create < unlock


async def test_reset_failure_does_not_mask_the_build_error() -> None:
    log: list[str] = []
    conn = _Connection(log, fail_create=True)
    original = conn.execute

    async def execute(statement: Any, params: Any = None) -> _Result:
        if str(statement).startswith("RESET"):
            raise ConnectionError("connection closed")
        return await original(statement, params)

    conn.execute = execute  # type: ignore[method-assign]
    conn.invalidate = lambda: _noop()  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="deadlock"):
        await indexing.build_vector_index_concurrently(_Engine(conn), uuid4(), 3)  # type: ignore[arg-type]


async def _noop() -> None:
    return None


async def test_failed_build_job_logs_the_generation_and_propagates_without_waking(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """(e) A build that raises (incl. cancellation/timeout) leaves the generation running: it must be
    logged with its id, the error must propagate (arq retry), and the poller must not be woken."""
    generation_id = uuid4()
    log: list[str] = []

    async def build(*_a: Any) -> None:
        raise TimeoutError("statement timeout")

    async def wake(*_a: Any, **_k: Any) -> bool:
        log.append("wake")
        return True

    patch_split(monkeypatch, "modules.search.indexing", "build_vector_index_concurrently", build)
    patch_split(monkeypatch, "modules.search.indexing", "wake", wake)
    ctx: dict[str, object] = {"session_factory": _EngineSession, "redis": object()}
    with caplog.at_level("WARNING", logger="modules.search.indexing"):
        with pytest.raises(TimeoutError, match="statement timeout"):
            await indexing.build_search_vector_index(ctx, str(generation_id), 3)  # type: ignore[arg-type]
    assert log == []
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any(str(generation_id) in r.getMessage() for r in warnings)
