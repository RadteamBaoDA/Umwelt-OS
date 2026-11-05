"""Shared helpers for Alembic revisions and ``env.py``: migration timeouts and online-safe index DDL.

Revisions import from here instead of re-implementing the safety pattern:

* ``create_index_concurrently`` / ``drop_index_concurrently`` run ``CREATE/DROP INDEX CONCURRENTLY`` inside
  ``autocommit_block`` (a concurrent build cannot run inside a transaction block) with ``IF [NOT] EXISTS``.
  A previous interrupted build leaves an *invalid* index of the same name that ``IF NOT EXISTS`` would silently
  accept, so the create helper drops an invalid leftover first (online mode only; offline SQL cannot inspect).
* ``migration_timeout_settings`` / ``timeout_statements`` define the ``lock_timeout`` and ``statement_timeout``
  that ``env.py`` applies to every migration connection so DDL fails fast instead of queueing behind
  application traffic and blocking it.
* ``destructive_downgrade_allowed`` is the explicit opt-in (``UMWELT_ALLOW_DESTRUCTIVE_DOWNGRADE=1``) for
  downgrades that wipe data.
"""

from __future__ import annotations

import os
import re

from alembic import op
from sqlalchemy import text

DESTRUCTIVE_DOWNGRADE_ENV = "UMWELT_ALLOW_DESTRUCTIVE_DOWNGRADE"
LOCK_TIMEOUT_ENV = "UMWELT_MIGRATION_LOCK_TIMEOUT_MS"
STATEMENT_TIMEOUT_ENV = "UMWELT_MIGRATION_STATEMENT_TIMEOUT_MS"
DEFAULT_LOCK_TIMEOUT_MS = 5_000
# Generous: rewriting document_chunks for the stored tsvector column can take minutes on large libraries.
DEFAULT_STATEMENT_TIMEOUT_MS = 3_600_000
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


def _env_milliseconds(name: str, default: int) -> int:
    """Read a non-negative integer millisecond setting from the environment, falling back on bad values."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def migration_timeout_settings() -> tuple[int, int]:
    """Return ``(lock_timeout_ms, statement_timeout_ms)`` for migration connections (0 disables a timeout)."""
    return (
        _env_milliseconds(LOCK_TIMEOUT_ENV, DEFAULT_LOCK_TIMEOUT_MS),
        _env_milliseconds(STATEMENT_TIMEOUT_ENV, DEFAULT_STATEMENT_TIMEOUT_MS),
    )


def timeout_statements() -> tuple[str, str]:
    """Return the two session-level ``SET`` statements ``env.py`` runs before any migration DDL."""
    lock_ms, statement_ms = migration_timeout_settings()
    return f"SET lock_timeout = {lock_ms}", f"SET statement_timeout = {statement_ms}"


def destructive_downgrade_allowed() -> bool:
    """Return True only when the operator explicitly set ``UMWELT_ALLOW_DESTRUCTIVE_DOWNGRADE=1``."""
    return os.environ.get(DESTRUCTIVE_DOWNGRADE_ENV) == "1"


def _checked_identifier(name: str) -> str:
    """Return ``name`` when it is a plain lower-case SQL identifier; helper input is code, never request data."""
    if not _IDENTIFIER.match(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return name


def _restore_lock_timeout() -> str:
    """Return the ``SET`` statement that puts ``lock_timeout`` back to the configured migration value."""
    return f"SET lock_timeout = {migration_timeout_settings()[0]}"


def create_index_concurrently(name: str, table: str, definition: str) -> None:
    """Create ``name`` on ``table`` with ``CREATE INDEX CONCURRENTLY IF NOT EXISTS`` in an autocommit block.

    ``definition`` is everything after ``ON <table>`` (for example ``(col)`` or ``USING gin (col) WHERE x``).
    The surrounding transaction is committed first (``autocommit_block``), ``lock_timeout`` is lifted for the
    build (the build legitimately waits for older transactions) and restored afterwards. An invalid leftover of
    a failed earlier build is dropped first so the retry actually rebuilds it.
    """
    _checked_identifier(name)
    _checked_identifier(table)
    context = op.get_context()
    with context.autocommit_block():
        op.execute("SET lock_timeout = 0")
        if not context.as_sql:
            invalid = op.get_bind().execute(
                text(
                    "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                    "WHERE c.relname = :name AND NOT i.indisvalid"
                ),
                {"name": name},
            ).first()
            if invalid is not None:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} {definition}")
        op.execute(_restore_lock_timeout())


def drop_index_concurrently(name: str) -> None:
    """Drop ``name`` with ``DROP INDEX CONCURRENTLY IF EXISTS`` inside an autocommit block (no table lock)."""
    _checked_identifier(name)
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = 0")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        op.execute(_restore_lock_timeout())
