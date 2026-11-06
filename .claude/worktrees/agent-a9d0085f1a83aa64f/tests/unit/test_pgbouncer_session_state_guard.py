"""AST guard: no session-level PostgreSQL state outside the allowlisted direct-connection paths.

Behind PgBouncer in transaction-pooling mode a server connection is shared between clients between
transactions, so session advisory locks, non-LOCAL ``SET``, ``LISTEN``, ``WITH HOLD`` cursors and temp
objects that outlive their transaction would leak between requests or silently stop protecting anything.
Transaction-scoped forms (``pg_advisory_xact_lock``, ``SET LOCAL``, ``set_config(.., true)``) are fine.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("apps", "core", "modules", "scripts")

# Files that legitimately hold a session advisory lease. Each obtains its connection from
# ``core.database.direct_engine`` (asserted below); harness.py only re-probes the connection its caller leased.
SESSION_LOCK_ALLOWLIST = {
    "core/auth/deletion_worker.py",
    "core/storage/admission.py",
    "modules/agents/worker.py",
    "modules/agents/harness.py",
    # HNSW CREATE INDEX CONCURRENTLY serialised per index name on an autocommit connection.
    "modules/search/indexing.py",
}
DIRECT_ENGINE_USERS = SESSION_LOCK_ALLOWLIST - {"modules/agents/harness.py"}
# Session ``SET``/``RESET`` is allowed only where the connection never passes through the pooler:
# * indexing.py lifts statement/lock timeouts for the autocommit HNSW build on ``direct_engine`` (asserted by
#   DIRECT_ENGINE_USERS; CREATE INDEX CONCURRENTLY cannot run in a transaction, so SET LOCAL is impossible);
# * migration_support.py runs only inside Alembic, whose env.py always prefers DATABASE_DIRECT_URL (asserted below).
SESSION_SET_ALLOWLIST = {"modules/search/indexing.py", "core/migration_support.py"}
# Cypher (Neo4j) strings start with SET too; they never reach PostgreSQL.
CYPHER_ALLOWLIST = {
    "modules/knowledge/temporal/graph_fence.py",
    "modules/knowledge/temporal/_reconcile.py",
}
# CREATE TEMP VIEW inside a raw SAVEPOINT that is rolled back before the transaction ends (see db_query.py).
TEMP_OBJECT_ALLOWLIST = {"modules/chat/db_query.py"}

SESSION_LOCK = re.compile(
    r"pg_(?:try_)?advisory_(?:unlock_all|unlock|lock)(?:_shared)?\b", re.IGNORECASE
)
NON_LOCAL_SET = re.compile(
    r"^\s*SET\s+(?:SESSION\b|TIME\s+ZONE\b|ROLE\b|[A-Za-z_][\w.]*\s*(?:=|TO\s))", re.IGNORECASE
)
OTHER_SESSION_STATE = re.compile(
    r"^\s*(?:LISTEN\b|UNLISTEN\b|DISCARD\s+(?:ALL|PLANS|SEQUENCES|TEMP))|\bWITH\s+HOLD\b",
    re.IGNORECASE,
)
TEMP_OBJECT = re.compile(r"\bCREATE\s+(?:GLOBAL\s+|LOCAL\s+)?(?:TEMP|TEMPORARY)\b", re.IGNORECASE)
SET_CONFIG_SESSION = re.compile(r"set_config\([^)]*,\s*(?:false|'false')\s*\)", re.IGNORECASE)


def _production_files() -> list[Path]:
    """Return every production Python file (tests excluded) under the scanned roots."""
    return sorted(
        path
        for name in SCAN_DIRS
        for path in (ROOT / name).rglob("*.py")
        if "__pycache__" not in path.parts
    )


def _violations(path: Path, root: Path = ROOT) -> list[str]:
    """Return human-readable session-state findings for one file, honouring the per-category allowlists."""
    rel = path.relative_to(root).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    # Docstrings and bare string statements are prose, never executed SQL.
    prose = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr)}
    for node in ast.walk(tree):
        if id(node) in prose:
            continue
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name == "set_config" and len(node.args) == 3:
                local = node.args[2]
                if not (isinstance(local, ast.Constant) and local.value is True):
                    found.append(f"{rel}:{node.lineno} set_config() without is_local=True")
            continue
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        value, where = node.value, f"{rel}:{node.lineno}"
        if SESSION_LOCK.search(value) and rel not in SESSION_LOCK_ALLOWLIST:
            found.append(f"{where} session advisory lock outside the direct-connection allowlist")
        if NON_LOCAL_SET.match(value) and rel not in CYPHER_ALLOWLIST | SESSION_SET_ALLOWLIST:
            found.append(f"{where} non-LOCAL SET")
        if OTHER_SESSION_STATE.search(value):
            found.append(f"{where} LISTEN/DISCARD/WITH HOLD")
        if TEMP_OBJECT.search(value) and rel not in TEMP_OBJECT_ALLOWLIST:
            found.append(f"{where} temp object")
        if SET_CONFIG_SESSION.search(value):
            found.append(f"{where} session-scoped set_config")
    return found


def test_no_session_level_state_outside_allowlist() -> None:
    """Production code must stay transaction-pool safe: no session locks/SET/LISTEN/HOLD outside allowlists."""
    findings = [item for path in _production_files() for item in _violations(path)]
    assert findings == []


def test_session_lock_sites_use_the_direct_engine() -> None:
    """Every allowlisted session-lock owner must connect through ``direct_engine`` so PgBouncer is bypassed."""
    for rel in sorted(DIRECT_ENGINE_USERS):
        source = (ROOT / rel).read_text(encoding="utf-8")
        assert "direct_engine(" in source, rel
        assert "engine.connect()" not in source.replace("direct_engine(engine).connect()", ""), rel


def test_session_set_allowlist_runs_only_on_direct_connections() -> None:
    """Each session-``SET`` exemption must still use session SET and reach PostgreSQL without the pooler."""
    for rel in sorted(SESSION_SET_ALLOWLIST):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        assert any(
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and NON_LOCAL_SET.match(node.value)
            for node in ast.walk(tree)
        ), f"{rel} no longer issues a session SET; drop it from the allowlist"
    assert "modules/search/indexing.py" in DIRECT_ENGINE_USERS
    env = (ROOT / "infrastructure/postgres/migrations/env.py").read_text(encoding="utf-8")
    assert 'os.environ.get("DATABASE_DIRECT_URL") or os.environ.get("DATABASE_URL")' in env


def test_allowlisted_files_really_use_session_locks() -> None:
    """An allowlist entry that no longer needs the exemption must be removed, keeping the guard tight."""
    for rel in sorted(SESSION_LOCK_ALLOWLIST):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert SESSION_LOCK.search(text), (
            f"{rel} no longer uses a session lock; drop it from the allowlist"
        )


def test_guard_detects_forbidden_patterns(tmp_path: Path) -> None:
    """The guard flags each forbidden construct (self-test so it cannot silently pass on a broken regex)."""
    sample = tmp_path / "bad.py"
    sample.write_text(
        'a = "SELECT pg_advisory_lock(1)"\n'
        'b = "SET statement_timeout = 5"\n'
        'c = "LISTEN events"\n'
        'd = "DECLARE c CURSOR WITH HOLD FOR SELECT 1"\n'
        'e = "CREATE TEMP TABLE t (a int)"\n'
        'f = func.set_config("x", "1", False)\n'
        'ok = "SELECT pg_advisory_xact_lock(1)"\n'
        'ok2 = "SET LOCAL statement_timeout = 5"\n',
        encoding="utf-8",
    )
    found = _violations(sample, tmp_path)
    joined = "\n".join(found)
    assert len(found) == 6, found
    for marker in ("session advisory lock", "non-LOCAL SET", "LISTEN", "temp object", "set_config"):
        assert marker in joined, joined
    assert "bad.py:7" not in joined and "bad.py:8" not in joined
