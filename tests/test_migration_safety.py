"""Migration safety: offline-SQL linter, 0014 shape, guarded destructive downgrade, timeouts, FK coverage."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from core import migration_support

ROOT = Path(__file__).resolve().parents[1]
REVISION = "0014_perf_indexes_and_safety"
PREVIOUS = "0013_chat_sql_reader_role"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


lint_migrations = _load("lint_migrations")
list_unindexed_fks = _load("list_unindexed_fks")


def _alembic(*args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DATABASE_URL": "postgresql+asyncpg://u:build-placeholder@postgres:5432/db", **(extra_env or {})}
    if extra_env is None:
        env.pop(migration_support.DESTRUCTIVE_DOWNGRADE_ENV, None)
    return subprocess.run(  # noqa: S603  # test-only subprocess with fixed arguments
        [sys.executable, "-m", "alembic", *args], check=False, capture_output=True, text=True, env=env, cwd=ROOT,
    )


@pytest.fixture(scope="module")
def full_sql() -> str:
    result = _alembic("upgrade", "base:head", "--sql")
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture(scope="module")
def perf_sql() -> str:
    # Only revision 0014's own DDL: later revisions (e.g. 0015 creating a new table) may index their new tables plainly.
    result = _alembic("upgrade", f"{PREVIOUS}:{REVISION}", "--sql")
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_shipped_migrations_pass_the_linter(full_sql: str) -> None:
    assert [str(v) for v in lint_migrations.lint(full_sql)] == []


def test_linter_actually_fires_on_historical_revisions(full_sql: str) -> None:
    """With the grandfather list emptied the rules find real violations, proving they are not vacuous."""
    assert lint_migrations.lint(full_sql, grandfathered=frozenset({"0001_init"}))


def _bad(statement: str) -> list[str]:
    sql = f"-- Running upgrade 0013 -> 9999_new\n\n{statement};\n"
    return [v.rule for v in lint_migrations.lint(sql, grandfathered=frozenset())]


@pytest.mark.parametrize(("statement", "rule"), [
    ("CREATE INDEX ix_a ON big_table (a)", "index-not-concurrent"),
    ("CREATE UNIQUE INDEX ix_a ON big_table (a)", "index-not-concurrent"),
    ("DROP INDEX ix_a", "index-not-concurrent"),
    ("ALTER TABLE big_table ADD COLUMN token UUID DEFAULT gen_random_uuid()", "volatile-default"),
    ("ALTER TABLE big_table ADD COLUMN r DOUBLE PRECISION DEFAULT random()", "volatile-default"),
    ("ALTER TABLE big_table ADD COLUMN t tsvector GENERATED ALWAYS AS (to_tsvector('simple', c)) STORED",
     "stored-generated-column"),
    ("ALTER TABLE big_table ADD COLUMN n INTEGER NOT NULL", "not-null-without-default"),
    ("ALTER TABLE big_table ALTER COLUMN c TYPE BIGINT", "column-type-change"),
    ("ALTER TABLE big_table ALTER COLUMN c SET NOT NULL", "set-not-null"),
    ("ALTER TABLE big_table ADD CONSTRAINT fk FOREIGN KEY (a) REFERENCES other (id)", "constraint-without-not-valid"),
    ("ALTER TABLE big_table ADD CONSTRAINT ck CHECK (a > 0)", "constraint-without-not-valid"),
])
def test_linter_flags_each_blocking_operation(statement: str, rule: str) -> None:
    assert rule in _bad(statement)


@pytest.mark.parametrize("statement", [
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_a ON big_table (a)",
    "DROP INDEX CONCURRENTLY IF EXISTS ix_a",
    "ALTER TABLE big_table ADD COLUMN flag BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE big_table ADD COLUMN created TIMESTAMPTZ DEFAULT now()",
    "ALTER TABLE big_table ADD CONSTRAINT fk FOREIGN KEY (a) REFERENCES other (id) NOT VALID",
    "ALTER TABLE big_table VALIDATE CONSTRAINT fk",
])
def test_linter_accepts_online_safe_operations(statement: str) -> None:
    sql = f"-- Running upgrade 0013 -> 9999_new\n\nCOMMIT;\n\n{statement};\n"
    assert lint_migrations.lint(sql, grandfathered=frozenset()) == []


def test_linter_allows_plain_index_on_a_table_created_in_the_same_revision() -> None:
    sql = (
        "-- Running upgrade 0013 -> 9999_new\n\nCREATE TABLE fresh (id INTEGER NOT NULL);\n\n"
        "CREATE INDEX ix_fresh ON fresh (id);\n\nALTER TABLE fresh ADD COLUMN n INTEGER NOT NULL;\n"
    )
    assert lint_migrations.lint(sql, grandfathered=frozenset()) == []


def test_linter_flags_concurrent_statement_inside_a_transaction() -> None:
    sql = "-- Running upgrade 0013 -> 9999_new\n\nBEGIN;\n\nCREATE INDEX CONCURRENTLY ix_a ON t (a);\n\nCOMMIT;\n"
    assert [v.rule for v in lint_migrations.lint(sql, grandfathered=frozenset())] == ["concurrent-in-transaction"]


def test_revision_0014_renders_online_safe_index_ddl(perf_sql: str) -> None:
    assert "ADD COLUMN IF NOT EXISTS content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED" in perf_sql
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_document_chunks_content_tsv_gin ON document_chunks USING gin (content_tsv)" in perf_sql
    for dropped in ("ix_document_chunks_simple_fts", "ix_document_chunks_content_tsv", "ix_relationship_evidence_document"):
        assert f"DROP INDEX CONCURRENTLY IF EXISTS {dropped};" in perf_sql
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_fk_entity_aliases_source_id ON entity_aliases (source_id)" in perf_sql
    assert "ON memories (superseded_by_id) WHERE superseded_by_id IS NOT NULL" in perf_sql
    assert "ix_chat_response_runs_active_status ON chat_response_runs (status) WHERE status IN ('pending', 'streaming', 'awaiting_input')" in perf_sql
    assert "DROP INDEX CONCURRENTLY IF EXISTS ix_chat_response_runs_status;" in perf_sql
    assert "ALTER TABLE event_outbox SET (autovacuum_vacuum_scale_factor = 0.02" in perf_sql
    for table in ("chat_stream_events", "realtime_replay_events", "source_observations", "system_health_samples"):
        assert f"ALTER TABLE {table} SET (autovacuum_" in perf_sql
    # Every non-transactional statement is preceded by COMMIT (autocommit_block) and no plain CREATE INDEX remains.
    assert "\nCREATE INDEX ix_" not in perf_sql and "\nCREATE INDEX IF NOT EXISTS" not in perf_sql


def test_revision_0014_follows_head_and_keeps_ancestry() -> None:
    result = _alembic("history", "-r", f"{PREVIOUS}:{REVISION}")
    assert result.returncode == 0, result.stderr
    assert REVISION in result.stdout and PREVIOUS in result.stdout


def test_0014_downgrade_renders_inverse_without_destruction() -> None:
    result = _alembic("downgrade", f"{REVISION}:{PREVIOUS}", "--sql")
    assert result.returncode == 0, result.stderr
    assert "DROP COLUMN IF EXISTS content_tsv" in result.stdout
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_document_chunks_simple_fts" in result.stdout
    assert "DROP SCHEMA" not in result.stdout


def test_0001_downgrade_refuses_without_explicit_flag() -> None:
    refused = _alembic("downgrade", "0001_init:base", "--sql")
    assert refused.returncode != 0
    assert "UMWELT_ALLOW_DESTRUCTIVE_DOWNGRADE" in refused.stderr and "DROP SCHEMA" not in refused.stdout
    allowed = _alembic("downgrade", "0001_init:base", "--sql", extra_env={"UMWELT_ALLOW_DESTRUCTIVE_DOWNGRADE": "1"})
    assert allowed.returncode == 0, allowed.stderr
    assert "DROP SCHEMA public CASCADE" in allowed.stdout


def test_destructive_downgrade_flag_requires_exactly_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(migration_support.DESTRUCTIVE_DOWNGRADE_ENV, raising=False)
    assert migration_support.destructive_downgrade_allowed() is False
    for value in ("0", "true", "yes", ""):
        monkeypatch.setenv(migration_support.DESTRUCTIVE_DOWNGRADE_ENV, value)
        assert migration_support.destructive_downgrade_allowed() is False
    monkeypatch.setenv(migration_support.DESTRUCTIVE_DOWNGRADE_ENV, "1")
    assert migration_support.destructive_downgrade_allowed() is True


def test_migration_timeouts_default_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(migration_support.LOCK_TIMEOUT_ENV, raising=False)
    monkeypatch.delenv(migration_support.STATEMENT_TIMEOUT_ENV, raising=False)
    assert migration_support.timeout_statements() == ("SET lock_timeout = 5000", "SET statement_timeout = 3600000")
    monkeypatch.setenv(migration_support.LOCK_TIMEOUT_ENV, "250")
    monkeypatch.setenv(migration_support.STATEMENT_TIMEOUT_ENV, "0")
    assert migration_support.timeout_statements() == ("SET lock_timeout = 250", "SET statement_timeout = 0")
    monkeypatch.setenv(migration_support.LOCK_TIMEOUT_ENV, "-5")
    monkeypatch.setenv(migration_support.STATEMENT_TIMEOUT_ENV, "abc")
    assert migration_support.timeout_statements() == ("SET lock_timeout = 5000", "SET statement_timeout = 3600000")


def test_env_py_applies_timeouts_to_the_migration_connection() -> None:
    source = (ROOT / "infrastructure/postgres/migrations/env.py").read_text(encoding="utf-8")
    body = source.split("def do_run_migrations", 1)[1].split("async def run_async_migrations", 1)[0]
    assert "timeout_statements()" in body
    assert body.index("timeout_statements()") < body.index("context.configure")


def test_helper_identifier_validation_rejects_injection() -> None:
    with pytest.raises(ValueError):
        migration_support.create_index_concurrently("ix; DROP TABLE x", "t", "(a)")
    with pytest.raises(ValueError):
        migration_support.drop_index_concurrently("Bad-Name")


def test_every_0014_fk_index_is_new_valid_and_within_identifier_limits(full_sql: str) -> None:
    module = _load_revision()
    names = [module._fk_index_name(table, column) for table, column, _ in module._FK_INDEXES]
    assert len(names) == len(set(names)) and all(len(name) <= 63 for name in names)
    state = list_unindexed_fks.replay(full_sql)
    for table, column, _predicate in module._FK_INDEXES:
        assert table in state, table
        assert any(column in fk[0] for fk in state[table].foreign_keys.values()), f"{table}.{column} is not a FK column"


def _load_revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "rev0014", ROOT / "infrastructure/postgres/migrations/versions/0014_perf_indexes_and_safety.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_remaining_unindexed_foreign_keys_are_only_documented_exclusions(full_sql: str) -> None:
    """After 0014 only FKs documented in docs/operations.md lack a leading index (workspace-share membership)."""
    remaining = {(table, columns) for table, columns, _ in list_unindexed_fks.unindexed_foreign_keys(full_sql)}
    assert remaining == {("workspace_shares", ("workspace_id", "member_user_id"))}


def test_no_exact_duplicate_indexes_remain_after_0014(full_sql: str) -> None:
    """Same table + identical column list + same predicate/method is a duplicate (excluding unique-backed pairs)."""
    import re
    from collections import defaultdict

    indexes: dict[str, dict[str, str]] = defaultdict(dict)
    for statement in list_unindexed_fks.split_statements(full_sql):
        created = re.match(r"CREATE (?:UNIQUE )?INDEX (?:CONCURRENTLY )?(?:IF NOT EXISTS )?(\S+) ON (\S+) (.*)$", statement, re.DOTALL)
        if created:
            indexes[created.group(2)][created.group(1)] = " ".join(created.group(3).split())
        dropped = re.match(r"DROP INDEX (?:CONCURRENTLY )?(?:IF EXISTS )?(\S+)", statement)
        if dropped:
            for table_indexes in indexes.values():
                table_indexes.pop(dropped.group(1), None)
    duplicates = []
    for table, table_indexes in indexes.items():
        seen: dict[str, str] = {}
        for name, definition in table_indexes.items():
            if definition in seen:
                duplicates.append((table, seen[definition], name))
            seen[definition] = name
    assert duplicates == []
