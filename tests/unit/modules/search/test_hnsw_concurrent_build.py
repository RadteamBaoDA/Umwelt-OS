"""HNSW activation builds its index CONCURRENTLY outside the activating transaction."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Self
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from core.errors import ConflictError
from modules.search import indexing

pytestmark = pytest.mark.asyncio


class _Result:
    def __init__(self, row: Any) -> None:
        self._row = row

    def first(self) -> Any:
        return self._row


class _Connection:
    """Autocommit connection fake recording statement text in order."""

    def __init__(self, log: list[str], *, invalid_leftover: bool = False, fail_create: bool = False) -> None:
        self.log, self.invalid_leftover, self.fail_create = log, invalid_leftover, fail_create

    async def execution_options(self, **options: Any) -> _Connection:
        self.log.append(f"options:{options['isolation_level']}")
        return self

    async def execute(self, statement: Any, params: Any = None) -> _Result:
        sql = str(statement)
        self.log.append(sql.split(" ON ")[0] if sql.startswith("CREATE") else sql.split(" WHERE ")[0])
        if sql.startswith("CREATE") and self.fail_create:
            raise RuntimeError("deadlock detected")
        return _Result(1 if self.invalid_leftover and sql.startswith("SELECT 1 FROM pg_index") else None)

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
    assert create.startswith(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON search_index_items USING hnsw")
    assert "embedding::vector(768)" in create and f"generation_id = '{generation_id}'" in create
    with pytest.raises(ValueError):
        indexing.vector_index_statements(generation_id, indexing.MAX_VECTOR_DIMENSIONS + 1)


async def test_build_runs_on_autocommit_with_timeouts_lifted_and_reset() -> None:
    log: list[str] = []
    generation_id = uuid4()
    await indexing.build_vector_index_concurrently(_Engine(_Connection(log)), generation_id, 3)  # type: ignore[arg-type]
    name = indexing.vector_index_name(generation_id)
    assert log == [
        "options:AUTOCOMMIT", f"SET statement_timeout = {indexing.HNSW_BUILD_STATEMENT_TIMEOUT_MS}", "SET lock_timeout = 0",
        "SELECT pg_advisory_lock(hashtext(:name))",
        "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid",  # invalid-leftover probe
        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name}",
        "SELECT pg_advisory_unlock(hashtext(:name))", "RESET statement_timeout", "RESET lock_timeout",
    ]


async def test_invalid_leftover_from_an_interrupted_build_is_dropped_before_the_retry() -> None:
    log: list[str] = []
    generation_id = uuid4()
    await indexing.build_vector_index_concurrently(  # type: ignore[arg-type]
        _Engine(_Connection(log, invalid_leftover=True)), generation_id, 3)
    name = indexing.vector_index_name(generation_id)
    assert log.index(f"DROP INDEX CONCURRENTLY IF EXISTS {name}") < log.index(
        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name}")


async def test_failed_build_drops_its_invalid_index_and_reraises() -> None:
    log: list[str] = []
    generation_id = uuid4()
    with pytest.raises(RuntimeError, match="deadlock"):
        await indexing.build_vector_index_concurrently(  # type: ignore[arg-type]
            _Engine(_Connection(log, fail_create=True)), generation_id, 3)
    name = indexing.vector_index_name(generation_id)
    assert log[-4:] == [f"DROP INDEX CONCURRENTLY IF EXISTS {name}", "SELECT pg_advisory_unlock(hashtext(:name))", "RESET statement_timeout", "RESET lock_timeout"]


_ENGINE = create_async_engine("postgresql+asyncpg://u:p@localhost/db")  # never connects; type is what matters


class _Session:
    """Session fake: records commit/rollback order and answers the re-lock selects from a script."""

    def __init__(self, log: list[str], generation: Any, unfinished: int = 0) -> None:
        self.log, self.generation, self.unfinished = log, generation, unfinished
        self.bind = _ENGINE

    async def commit(self) -> None:
        self.log.append("commit")

    async def rollback(self) -> None:
        self.log.append("rollback")

    async def scalar(self, statement: Any) -> Any:
        sql = str(statement)
        if "count(" in sql:
            self.log.append("count-unfinished")
            return self.unfinished
        self.log.append("lock-generation")
        return self.generation


def _ctx(generation_id: Any) -> Any:
    return SimpleNamespace(
        generation_id=generation_id, workspace_id=uuid4(), scope="scope", multi_workspace_enabled=False,
        original=SimpleNamespace(fence="FENCE"),
    )


async def test_commit_precedes_build_and_relock_follows(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    generation = SimpleNamespace(id=uuid4(), status="running", dimensions=3)
    ctx = _ctx(generation.id)

    async def build(engine: Any, generation_id: Any, dimensions: int) -> None:
        log.append(f"build:{type(engine).__name__}:{dimensions}")

    async def admit(session: Any, **kwargs: Any) -> str:
        log.append(f"admit:lock={kwargs['lock']}:expected={kwargs['expected']}")
        return "NEW-FENCE"

    monkeypatch.setattr(indexing, "build_vector_index_concurrently", build)
    monkeypatch.setattr(indexing, "_admit", admit)
    session = _Session(log, generation)

    result = await indexing._build_index_outside_transaction(session, ctx, generation)  # type: ignore[arg-type]

    assert result == (generation, "NEW-FENCE")
    # The activating transaction is committed before the concurrent build; the access-fence then the
    # generation row are locked again afterwards (same order as a claim).
    assert log == ["commit", "build:AsyncEngine:3", "admit:lock=True:expected=FENCE", "lock-generation", "count-unfinished"]


@pytest.mark.parametrize(("status", "dimensions", "unfinished"), [("cancelled", 3, 0), ("running", 4, 0), ("running", 3, 2)])
async def test_state_change_during_build_rolls_back_without_activating(
    monkeypatch: pytest.MonkeyPatch, status: str, dimensions: int, unfinished: int,
) -> None:
    log: list[str] = []
    generation = SimpleNamespace(id=uuid4(), status="running", dimensions=3)
    changed = SimpleNamespace(id=generation.id, status=status, dimensions=dimensions)

    async def build(*_a: Any) -> None:
        return None

    async def admit(*_a: Any, **_k: Any) -> str:
        return "F"

    monkeypatch.setattr(indexing, "build_vector_index_concurrently", build)
    monkeypatch.setattr(indexing, "_admit", admit)
    session = _Session(log, changed, unfinished)
    assert await indexing._build_index_outside_transaction(session, _ctx(generation.id), generation) is None  # type: ignore[arg-type]
    assert log[-1] == "rollback"


async def test_denied_admission_after_build_rolls_back_and_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    generation = SimpleNamespace(id=uuid4(), status="running", dimensions=3)

    async def build(*_a: Any) -> None:
        return None

    async def admit(*_a: Any, **_k: Any) -> str:
        raise ConflictError(detail="Workspace access changed")

    monkeypatch.setattr(indexing, "build_vector_index_concurrently", build)
    monkeypatch.setattr(indexing, "_admit", admit)
    session = _Session(log, generation)
    assert await indexing._build_index_outside_transaction(session, _ctx(generation.id), generation) is None  # type: ignore[arg-type]
    assert log == ["commit", "rollback"]


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
    await indexing.activate_generation(_Active(), generation, scope=SimpleNamespace(workspace_id=uuid4()))  # type: ignore[arg-type]
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
