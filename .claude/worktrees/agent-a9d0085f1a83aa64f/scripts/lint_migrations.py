"""Lint the offline SQL rendered from Alembic revisions for operations that block production traffic.

Usage: ``python scripts/lint_migrations.py [--sql FILE]`` (default: render ``alembic upgrade 0001_init:head
--sql``). Exit status 1 when a non-grandfathered revision violates a rule.

Rules (applied per revision section, to tables that already exist before the revision):

* ``index-not-concurrent``: ``CREATE INDEX`` / ``DROP INDEX`` without ``CONCURRENTLY``.
* ``concurrent-in-transaction``: ``CONCURRENTLY`` statement issued inside ``BEGIN`` ... ``COMMIT`` (PostgreSQL
  rejects it; revisions must use ``core.migration_support`` helpers, i.e. ``autocommit_block``).
* ``volatile-default``: ``ADD COLUMN ... DEFAULT <volatile function>`` (``random``, ``gen_random_uuid``,
  ``uuid_generate_v7``, ``clock_timestamp``, ``nextval`` ...) rewrites the whole table.
* ``stored-generated-column``: ``ADD COLUMN ... GENERATED ... STORED`` rewrites the whole table.
* ``not-null-without-default``: ``ADD COLUMN ... NOT NULL`` without ``DEFAULT`` fails on non-empty tables.
* ``column-type-change``: ``ALTER COLUMN ... TYPE`` usually rewrites the table.
* ``constraint-without-not-valid``: ``ADD CONSTRAINT ... FOREIGN KEY|CHECK`` without ``NOT VALID`` scans the
  table under a strong lock (validate separately with ``VALIDATE CONSTRAINT``).
* ``set-not-null``: ``ALTER COLUMN ... SET NOT NULL`` scans the table under ``ACCESS EXCLUSIVE``.

``GRANDFATHERED`` revisions were shipped before the linter existed and are skipped. A new revision that
must break a rule (for example the one-off ``content_tsv`` rewrite) lists the rule in ``ALLOWED`` with the
reason; the module docstring of that revision must explain the cost.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from list_unindexed_fks import split_statements  # sibling script, shared SQL splitter

GRANDFATHERED = frozenset(
    {
        "0001_init",
        "0002_observations_provider_ids",
        "0003_model_test_results",
        "0004_connector_oauth",
        "0005_reddit_oauth",
        "0006_web_search_fallbacks",
        "0007_collection_receipt_stats",
        "0008_live_map_config",
        "0008_owner_display_name_accent",
        "0009_system_health_history",
        "0010_observations_vn_stock",
        "0011_merge_live_map",
        "0011_model_capability_evidence",
        "0012_merge_live_and_capability",
        "0013_chat_sql_reader_role",
    }
)

# revision -> {rule: reason}. Keep this list tiny and justified.
ALLOWED: dict[str, dict[str, str]] = {
    "0014_perf_indexes_and_safety": {
        "stored-generated-column": "one-off document_chunks.content_tsv rewrite, documented in the revision "
        "docstring and docs/operations.md (maintenance window)",
    },
}

VOLATILE_FUNCTIONS = (
    "random",
    "gen_random_uuid",
    "uuid_generate_v1",
    "uuid_generate_v4",
    "uuid_generate_v7",
    "clock_timestamp",
    "timeofday",
    "nextval",
    "setseed",
)
_SECTION = re.compile(r"^-- Running upgrade (?P<old>.+?) -> (?P<new>[A-Za-z0-9_]+)", re.MULTILINE)
_IDENT = r'"?([A-Za-z_][A-Za-z0-9_]*)"?'


@dataclass(frozen=True)
class Violation:
    """One lint finding: the revision, the rule id and the offending statement (truncated)."""

    revision: str
    rule: str
    statement: str

    def __str__(self) -> str:
        """Render as ``revision: rule: statement`` for CLI and assertion output."""
        return f"{self.revision}: {self.rule}: {self.statement}"


def split_sections(sql: str) -> list[tuple[str, str]]:
    """Split rendered SQL into ``(new_revision, body)`` sections on Alembic's ``-- Running upgrade`` markers."""
    matches = list(_SECTION.finditer(sql))
    sections = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(sql)
        sections.append((match.group("new"), sql[match.end() : end]))
    return sections


def _statement_violations(statement: str, new_tables: set[str], in_transaction: bool) -> list[str]:
    """Return the rule ids a single statement breaks (table-existence aware via ``new_tables``)."""
    upper = " ".join(statement.split()).upper()
    rules: list[str] = []
    if "CONCURRENTLY" in upper and (upper.startswith(("CREATE", "DROP"))) and in_transaction:
        rules.append("concurrent-in-transaction")
    index = re.match(
        r"CREATE (?:UNIQUE )?INDEX (?!CONCURRENTLY)(?:IF NOT EXISTS )?\S+ ON (?:ONLY )?" + _IDENT,
        statement,
        re.IGNORECASE,
    )
    if index and index.group(1) not in new_tables:
        rules.append("index-not-concurrent")
    if re.match(r"DROP INDEX (?!CONCURRENTLY)", statement, re.IGNORECASE):
        rules.append("index-not-concurrent")
    alter = re.match(
        r"ALTER TABLE (?:ONLY )?(?:IF EXISTS )?" + _IDENT + r"\s+(.*)$",
        statement,
        re.IGNORECASE | re.DOTALL,
    )
    if alter and alter.group(1) not in new_tables:
        rules.extend(_alter_violations(" ".join(alter.group(2).split())))
    return rules


def _alter_violations(clause: str) -> list[str]:
    """Return rule ids for one ``ALTER TABLE`` clause on an existing table."""
    upper = clause.upper()
    rules: list[str] = []
    if re.match(r"ADD COLUMN\b", upper):
        if re.search(r"GENERATED ALWAYS AS \(.*\) STORED", upper):
            rules.append("stored-generated-column")
        default = re.search(r"\bDEFAULT\s+(.*)", upper)
        if default and any(f"{name.upper()}(" in default.group(1) for name in VOLATILE_FUNCTIONS):
            rules.append("volatile-default")
        if "NOT NULL" in upper and not default and "GENERATED" not in upper:
            rules.append("not-null-without-default")
    if re.search(r"\bALTER COLUMN \S+ (?:SET DATA )?TYPE\b", upper):
        rules.append("column-type-change")
    if re.search(r"\bALTER COLUMN \S+ SET NOT NULL\b", upper):
        rules.append("set-not-null")
    if (
        re.match(r"ADD CONSTRAINT\b", upper)
        and re.search(r"\b(FOREIGN KEY|CHECK)\b", upper)
        and "NOT VALID" not in upper
    ):
        rules.append("constraint-without-not-valid")
    return rules


def lint(sql: str, grandfathered: frozenset[str] = GRANDFATHERED) -> list[Violation]:
    """Return violations for every revision section in ``sql`` outside ``grandfathered`` and not in ``ALLOWED``."""
    violations: list[Violation] = []
    for revision, body in split_sections(sql):
        if revision in grandfathered:
            continue
        allowed = ALLOWED.get(revision, {})
        new_tables: set[str] = set()
        in_transaction = False
        for statement in split_statements(body):
            keyword = statement.strip().upper()
            if keyword == "BEGIN":
                in_transaction = True
                continue
            if keyword == "COMMIT":
                in_transaction = False
                continue
            created = re.match(
                r"CREATE TABLE (?:IF NOT EXISTS )?" + _IDENT, statement, re.IGNORECASE
            )
            if created:
                new_tables.add(created.group(1))
            for rule in _statement_violations(statement, new_tables, in_transaction):
                if rule not in allowed:
                    violations.append(Violation(revision, rule, " ".join(statement.split())[:160]))
    return violations


def render_upgrade_sql() -> str:
    """Render ``alembic upgrade 0001_init:head --sql`` offline (placeholder URL; no connection)."""
    env = {
        **os.environ,
        "DATABASE_URL": "postgresql+asyncpg://u:build-placeholder@postgres:5432/db",
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "0001_init:head", "--sql"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout


def main() -> int:
    """CLI entry point: print violations and return 1 when any exist, otherwise 0."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sql", help="Lint this rendered SQL file instead of rendering migrations")
    args = parser.parse_args()
    sql = Path(args.sql).read_text(encoding="utf-8") if args.sql else render_upgrade_sql()
    found = lint(sql)
    for violation in found:
        print(violation)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
