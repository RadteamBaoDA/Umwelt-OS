"""Security tests for the chat SQL tool: parser allowlist and least-privilege execution order."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from modules.chat import db_query
from modules.chat.db_query import CHAT_SQL_READER_ROLE, validate_readonly_sql

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


class _RecordingSession:
    """Async session double that records every executed SQL string in order."""

    def __init__(self) -> None:
        """Start with an empty statement log."""
        self.statements: list[str] = []

    async def execute(self, statement: Any, params: Any = None) -> _FakeResult:
        """Record the rendered statement text and return a canned result."""
        self.statements.append(str(statement))
        return _FakeResult()


async def test_user_sql_runs_after_read_only_and_role_switch_and_state_is_rolled_back() -> None:
    """SET LOCAL transaction_read_only and SET LOCAL ROLE precede the model SQL; savepoint rolls both back."""
    session = _RecordingSession()
    user_sql = "SELECT symbol, AVG(value) AS avg_val FROM scoped_observations GROUP BY symbol"
    out = await db_query._execute_sql_mode(
        session,  # type: ignore[arg-type]
        user_sql,
        workspace_id=uuid4(),
        allowed_source_ids=[uuid4()],
        limit=10,
        evidence_list=[],
    )
    stmts = session.statements
    user_idx = next(i for i, s in enumerate(stmts) if user_sql in s)
    ro_idx = next(i for i, s in enumerate(stmts) if "transaction_read_only = on" in s)
    role_idx = next(i for i, s in enumerate(stmts) if f"SET LOCAL ROLE {CHAT_SQL_READER_ROLE}" in s)
    grant_idx = next(i for i, s in enumerate(stmts) if s.startswith("GRANT SELECT"))
    assert grant_idx < ro_idx < role_idx < user_idx
    assert any(s.startswith("ROLLBACK TO SAVEPOINT") for s in stmts[user_idx + 1 :])
    assert '"avg_val": 1' in out and '"mode": "sql"' in out


async def test_invalid_sql_is_rejected_before_any_statement_executes() -> None:
    """A rejected query never reaches the database (no role switch, no temp views)."""
    session = _RecordingSession()
    with pytest.raises(ValueError):
        await db_query._execute_sql_mode(
            session,  # type: ignore[arg-type]
            "SELECT * FROM chat_messages",
            workspace_id=uuid4(),
            allowed_source_ids=[uuid4()],
            limit=10,
            evidence_list=[],
        )
    assert session.statements == []
