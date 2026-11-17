"""Security tests for the chat SQL tool: parser allowlist and dedicated read-only connection execution."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from modules.chat import db_query
from modules.chat.db_query import ChatSqlReaderUnavailableError, validate_readonly_sql

BYPASSES = [
    "SELECT * FROM chat_messages",
    "SELECT * FROM memories",
    "SELECT * FROM ai_settings",
    "SELECT * FROM github_oauth_grants",
    "SELECT * FROM scoped_observations, chat_messages",
    "SELECT query_to_xml('sel'||'ect * from users', true, false, '') FROM scoped_observations",
    "SELECT lo_from_bytea(0, 'x'::bytea) FROM scoped_observations",
    "SELECT set_config('role', 'postgres', false) FROM scoped_observations",
    "SELECT setval('some_seq', 1) FROM scoped_observations",
    "SELECT nextval('some_seq') FROM scoped_observations",
    "SELECT pg_sleep(5) FROM scoped_observations",
    "SELECT pg_read_file('/etc/passwd') FROM scoped_observations",
    "SELECT current_setting('is_superuser') FROM scoped_observations",
    "SELECT * FROM scoped_observations, generate_series(1, 100000000)",
    "SELECT generate_series(1, 100000000) FROM scoped_observations",
    "SELECT * FROM pg_catalog.pg_authid",
    "SELECT * FROM public.observations",
    "SELECT * FROM information_schema.tables",
    "SELECT * FROM scoped_observations FOR UPDATE",
    "SELECT * INTO newtab FROM scoped_observations",
    "SELECT 'pg_authid'::regclass FROM scoped_observations",
    "SELECT current_user FROM scoped_observations",
    "SELECT id OPERATOR(pg_catalog.+) 1 FROM scoped_observations",
    "SELECT pg_catalog.pg_sleep(1) FROM scoped_observations",
    "WITH x AS (DELETE FROM observations RETURNING *) SELECT * FROM scoped_observations",
    "WITH a AS (SELECT * FROM users), users AS (SELECT 1) SELECT * FROM scoped_observations",
    "WITH RECURSIVE r AS (SELECT 1 UNION ALL SELECT 1 FROM r) SELECT * FROM r, scoped_observations",
    "SELECT * FROM scoped_observations, (WITH users AS (SELECT 1) SELECT 1) z, users",
    "INSERT INTO scoped_observations VALUES (1)",
    "SELECT 1",
    "SELECT * FROM scoped_observations; SELECT 1",
    "SELECT * FROM scoped_observations -- c",
    "SELECT * FROM scoped_observations /* c */",
    "SELECT * FROM scoped_observations WHERE id = $1",
    "SELECT * FROM scoped_observations ORDER BY value USING <",
    "SELECT * FROM xmltable('/a' PASSING '<a/>' COLUMNS x text), scoped_observations",
    "SELEC nonsense",
]

LEGITIMATE = [
    "SELECT symbol, AVG(value) AS avg_val FROM scoped_observations GROUP BY symbol;",
    "SELECT provider, metric, MAX(value), MIN(value), COUNT(*), SUM(value) FROM scoped_observations GROUP BY 1, 2",
    (
        "SELECT date_trunc('day', observed_at) AS d, avg(value) FROM scoped_observations "
        "WHERE metric = 'price' AND observed_at >= now() - interval '7 days' GROUP BY 1 ORDER BY 1 DESC"
    ),
    (
        "SELECT lower(symbol), upper(region), length(unit), round(value::numeric, 2), abs(value) "
        "FROM scoped_observations WHERE symbol ILIKE '%set%' OR symbol = 'users'"
    ),
    (
        "SELECT coalesce(currency, 'USD'), nullif(unit, ''), greatest(value, 0), least(value, 100) "
        "FROM scoped_observations"
    ),
    (
        "SELECT extract(year FROM observed_at), to_char(observed_at, 'YYYY-MM'), substring(title FROM 1 FOR 20), "
        "trim(both ' ' FROM title) FROM scoped_documents"
    ),
    "SELECT title FROM scoped_documents WHERE title ILIKE '%bitcoin%' ORDER BY published_at DESC LIMIT 10",
    "WITH recent AS (SELECT title, published_at FROM scoped_documents LIMIT 5) SELECT * FROM recent",
    (
        "WITH a AS (SELECT symbol, value FROM scoped_observations), b AS (SELECT symbol, max(value) AS m FROM a "
        "GROUP BY symbol) SELECT a.symbol, a.value, b.m FROM a JOIN b ON a.symbol = b.symbol"
    ),
    (
        "SELECT symbol, value, row_number() OVER (PARTITION BY symbol ORDER BY observed_at DESC) AS rn "
        "FROM scoped_observations"
    ),
    (
        "SELECT o.symbol, count(*) FILTER (WHERE o.value > 10) FROM scoped_observations o "
        "WHERE o.source_id IN (SELECT source_id FROM scoped_documents) GROUP BY o.symbol HAVING count(*) > 1"
    ),
    "SELECT symbol FROM scoped_observations UNION SELECT title FROM scoped_documents",
    "SELECT CASE WHEN value > 1 THEN 'hi' ELSE 'lo' END AS bucket, count(*) FROM scoped_observations GROUP BY 1",
    "SELECT symbol, value BETWEEN 1 AND 10 FROM scoped_observations WHERE symbol IS NOT NULL",
    "SELECT quality ->> 'k' FROM scoped_observations WHERE document_id IS NULL",
    "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY value) FROM scoped_observations",
    "SELECT symbol FROM scoped_observations WHERE symbol SIMILAR TO '(BTC|ETH)%'",
    "SELECT symbol FROM scoped_observations WHERE symbol NOT SIMILAR TO 'B#%%' ESCAPE '#'",
]


@pytest.mark.parametrize("sql", BYPASSES)
def test_known_bypasses_and_unsafe_sql_are_rejected(sql: str) -> None:
    """Every documented bypass (and plain unsafe SQL) fails validation with ValueError."""
    with pytest.raises(ValueError):
        validate_readonly_sql(sql)


@pytest.mark.parametrize("sql", LEGITIMATE)
def test_legitimate_analytic_queries_pass(sql: str) -> None:
    """Typical analytic SELECTs over the scoped views still validate and return no trailing semicolon."""
    cleaned = validate_readonly_sql(sql)
    assert not cleaned.endswith(";")


class _FakeResult:
    """Minimal SQLAlchemy result stand-in exposing ``mappings().all()``."""

    def mappings(self) -> _FakeResult:
        """Return self so ``.all()`` can be chained."""
        return self

    def all(self) -> list[dict[str, Any]]:
        """Return one deterministic row."""
        return [{"symbol": "BTC", "avg_val": 1}]

    def scalar_one(self) -> int:
        """Return the fake reader backend pid."""
        return 4242


class _RecordingConn:
    """Async connection double recording executed SQL, bind params and rollbacks; optionally failing the model query."""

    def __init__(self, *, fail: bool = False, events: list[str] | None = None) -> None:
        """Start with empty logs; ``events`` (optional) is a log shared with the app-side double for ordering."""
        self.events = events if events is not None else []
        self.statements: list[str] = []
        self.params: list[Any] = []
        self.rollbacks = 0
        self.fail = fail

    async def __aenter__(self) -> _RecordingConn:
        """Enter the ``engine.connect()`` context."""
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Leave the context without suppressing errors."""

    async def execute(self, statement: Any, params: Any = None) -> _FakeResult:
        """Record the statement; raise on the bounded model query when ``fail`` is set."""
        self.statements.append(str(statement))
        self.events.append("reader:" + str(statement))
        self.params.append(params)
        if self.fail and "_bounded_query" in str(statement):
            raise RuntimeError("division by zero")
        return _FakeResult()

    async def rollback(self) -> None:
        """Count rollbacks."""
        self.events.append("reader:rollback")
        self.rollbacks += 1


class _FakeEngine:
    """Engine double whose ``connect()`` yields the shared recording connection."""

    def __init__(self, conn: _RecordingConn) -> None:
        """Remember the connection."""
        self.conn = conn

    def connect(self) -> _RecordingConn:
        """Return the recording connection."""
        return self.conn


class _FakeAppConn:
    """App-role connection double: records fence statements and commit into the shared event log."""

    def __init__(self, events: list[str]) -> None:
        """Share the ordering log."""
        self.events = events

    async def __aenter__(self) -> _FakeAppConn:
        """Begin the short app transaction."""
        self.events.append("app:begin")
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Commit (or abort) the app transaction."""
        self.events.append("app:commit")

    async def execute(self, statement: Any, params: Any = None) -> None:
        """Record the fence statement."""
        self.events.append("app:" + str(statement).split()[0])
        self.last_params = params


class _FakeAppEngine:
    """App engine double: ``begin()`` yields a recording connection."""

    def __init__(self, events: list[str]) -> None:
        """Share the ordering log."""
        self.events = events

    def begin(self) -> _FakeAppConn:
        """Return a recording app connection."""
        return _FakeAppConn(self.events)


class _NoQuerySession:
    """Session double: the SQL path must not touch the app session for the model query."""

    def __init__(self, bind: Any = None) -> None:
        """Expose ``bind`` like ``AsyncSession.bind`` (the app engine used for the fence)."""
        self.bind = bind

    async def execute(self, *_: Any, **__: Any) -> Any:
        """Fail loudly if the app connection is used."""
        raise AssertionError("model SQL must not run on the application session")


def _patch_engine(monkeypatch: pytest.MonkeyPatch, conn: _RecordingConn) -> _FakeAppEngine:
    """Route ``_reader_engine`` to the recording double; return an app engine sharing its event log."""
    monkeypatch.setattr(db_query, "_reader_engine", lambda: _FakeEngine(conn))
    return _FakeAppEngine(conn.events)


async def test_user_sql_runs_on_reader_after_app_fence_and_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Order: reader pid, app upsert+commit, READ ONLY, model SQL, reader rollback, app delete; no set_config."""
    conn = _RecordingConn()
    app = _patch_engine(monkeypatch, conn)
    user_sql = "SELECT symbol, AVG(value) AS avg_val FROM scoped_observations GROUP BY symbol"
    out = await db_query._execute_sql_mode(
        _NoQuerySession(app),  # type: ignore[arg-type]
        user_sql,
        workspace_id=uuid4(),
        allowed_source_ids=[uuid4()],
        limit=10,
        evidence_list=[],
    )
    ev = conn.events
    assert not any("set_config" in e for e in ev)
    steps = [
        "reader:SELECT pg_backend_pid()",
        "app:begin",
        "app:INSERT",
        "app:commit",
        "reader:SET TRANSACTION READ ONLY",
    ]
    assert ev[: len(steps)] == steps
    assert (
        user_sql in ev[len(steps) + 1]
    )  # statement_timeout sits between READ ONLY and the model query
    assert ev[-4:] == ["reader:rollback", "app:begin", "app:DELETE", "app:commit"]
    assert not any("ROLE" in s for s in conn.statements)
    assert conn.rollbacks == 1
    assert '"avg_val": 1' in out and '"mode": "sql"' in out


async def test_invalid_sql_is_rejected_before_any_statement_executes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected query never reaches the database."""
    conn = _RecordingConn()
    _patch_engine(monkeypatch, conn)
    with pytest.raises(ValueError):
        await db_query._execute_sql_mode(
            _NoQuerySession(),  # type: ignore[arg-type]
            "SELECT * FROM chat_messages",
            workspace_id=uuid4(),
            allowed_source_ids=[uuid4()],
            limit=10,
            evidence_list=[],
        )
    assert conn.statements == []


async def test_model_query_error_still_rolls_back_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A query that raises still rolls the reader transaction back and surfaces the original error."""
    conn = _RecordingConn(fail=True)
    app = _patch_engine(monkeypatch, conn)
    with pytest.raises(RuntimeError, match="division by zero"):
        await db_query._run_sql_as_reader(
            "SELECT 1 / 0 FROM scoped_observations",
            app_engine=app,  # type: ignore[arg-type]
            workspace_id=uuid4(),
            allowed_source_ids=[uuid4()],
            max_rows=5,
        )
    assert conn.rollbacks == 1
    assert conn.events[-3:] == [
        "app:begin",
        "app:DELETE",
        "app:commit",
    ]  # fence removed even on error


async def test_sql_mode_fails_closed_without_reader_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """With CHAT_SQL_READER_DATABASE_URL unset the tool is disabled; it never falls back to the app connection."""
    monkeypatch.setattr(
        db_query, "Settings", lambda: type("S", (), {"chat_sql_reader_database_url": None})()
    )
    with pytest.raises(ChatSqlReaderUnavailableError, match="CHAT_SQL_READER_DATABASE_URL"):
        await db_query._execute_sql_mode(
            _NoQuerySession(),  # type: ignore[arg-type]
            "SELECT 1 FROM scoped_observations",
            workspace_id=uuid4(),
            allowed_source_ids=[uuid4()],
            limit=10,
            evidence_list=[],
        )


@pytest.mark.parametrize(
    ("workspace_id", "source_ids"),
    [
        ("11111111-1111-1111-1111-111111111111", [uuid4()]),
        (uuid4(), ["22222222-2222-2222-2222-222222222222"]),
        (uuid4(), [uuid4(), "x'); DROP TABLE users; --"]),
    ],
)
async def test_non_uuid_ids_fail_before_any_statement_executes(
    monkeypatch: pytest.MonkeyPatch,
    workspace_id: Any,
    source_ids: list[Any],
) -> None:
    """Anything that is not a uuid.UUID instance (even a valid-looking str) is refused before any I/O."""
    conn = _RecordingConn()
    app = _patch_engine(monkeypatch, conn)
    with pytest.raises(TypeError):
        await db_query._run_sql_as_reader(
            "SELECT 1 FROM scoped_observations",
            app_engine=app,  # type: ignore[arg-type]
            workspace_id=workspace_id,
            allowed_source_ids=source_ids,
            max_rows=5,
        )
    assert conn.statements == []
