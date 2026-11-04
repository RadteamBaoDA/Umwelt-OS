"""List foreign-key column sets that have no leading index, from the migration DDL alone.

Usage: ``python scripts/list_unindexed_fks.py [--sql FILE]``. Without ``--sql`` the script renders
``alembic upgrade base:head --sql`` (offline, no database) and analyses the final schema state.

The parser is deliberately small: it replays ``CREATE TABLE``/``ALTER TABLE ... ADD CONSTRAINT``/
``DROP CONSTRAINT``/``CREATE INDEX``/``DROP INDEX``/``DROP TABLE`` in order, so the answer reflects
the post-migration schema. A foreign key is *covered* when some non-partial index, primary key or
unique constraint on the same table starts with an FK column (see ``unindexed_foreign_keys``). Output is one
``table (cols) -> referenced_table`` line per uncovered FK; exit status is always 0 (informational).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field


@dataclass
class TableState:
    """Replayed state of one table: FK column tuples and the column tuples of usable indexes."""

    foreign_keys: dict[str, tuple[tuple[str, ...], str]] = field(default_factory=dict)
    indexes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    constraint_indexes: dict[str, tuple[str, ...]] = field(default_factory=dict)


_IDENT = r'"?([A-Za-z_][A-Za-z0-9_]*)"?'
_CREATE_TABLE = re.compile(r"CREATE TABLE (?:IF NOT EXISTS )?" + _IDENT + r"\s*\((.*)\)\s*$", re.DOTALL)
_ALTER = re.compile(r"ALTER TABLE (?:ONLY )?(?:IF EXISTS )?" + _IDENT + r"\s+(.*)$", re.DOTALL)
_ADD_CONSTRAINT = re.compile(r"ADD CONSTRAINT " + _IDENT + r"\s+(.*)$", re.DOTALL)
_DROP_CONSTRAINT = re.compile(r"DROP CONSTRAINT (?:IF EXISTS )?" + _IDENT, re.DOTALL)
_CREATE_INDEX = re.compile(
    r"CREATE (?:UNIQUE )?INDEX (?:CONCURRENTLY )?(?:IF NOT EXISTS )?" + _IDENT + r" ON (?:ONLY )?" + _IDENT
    + r"(?: USING \w+)?\s*\((.*?)\)(\s+WHERE\s+.*)?$", re.DOTALL)
_DROP_INDEX = re.compile(r"DROP INDEX (?:CONCURRENTLY )?(?:IF EXISTS )?" + _IDENT, re.DOTALL)
_DROP_TABLE = re.compile(r"DROP TABLE (?:IF EXISTS )?" + _IDENT, re.DOTALL)
_FK = re.compile(r"FOREIGN KEY\s*\(([^)]*)\)\s*REFERENCES\s+" + _IDENT, re.DOTALL)
_KEY = re.compile(r"^(PRIMARY KEY|UNIQUE)\s*\(([^)]*)\)", re.DOTALL)


def _columns(raw: str) -> tuple[str, ...]:
    """Return plain column names from a comma list, dropping quoting, ordering and opclass words."""
    names = []
    for part in raw.split(","):
        token = part.strip().split()[0] if part.strip() else ""
        if token:
            names.append(token.strip('"'))
    return tuple(names)


def split_statements(sql: str) -> list[str]:
    """Split rendered Alembic SQL on top-level semicolons, honouring ``$$`` bodies and quotes."""
    statements: list[str] = []
    buffer: list[str] = []
    in_dollar = False
    in_quote = False
    index = 0
    while index < len(sql):
        char = sql[index]
        if not in_quote and sql.startswith("$$", index):
            in_dollar = not in_dollar
            buffer.append("$$")
            index += 2
            continue
        if not in_dollar and char == "'":
            in_quote = not in_quote
        if char == ";" and not in_dollar and not in_quote:
            statements.append("".join(buffer).strip())
            buffer = []
        else:
            buffer.append(char)
        index += 1
    statements.append("".join(buffer).strip())
    cleaned = []
    for statement in statements:
        # Drop Alembic's leading "-- Running upgrade" comment lines.
        lines = [line for line in statement.splitlines() if not line.lstrip().startswith("--")]
        text = "\n".join(lines).strip()
        if text:
            cleaned.append(text)
    return cleaned


def _top_level_items(body: str) -> list[str]:
    """Split a CREATE TABLE body on commas that are not nested in parentheses."""
    items: list[str] = []
    depth = 0
    current: list[str] = []
    for char in body:
        depth += char == "("
        depth -= char == ")"
        if char == "," and depth == 0:
            items.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    items.append("".join(current).strip())
    return [item for item in items if item]


def _apply_constraint(table: TableState, name: str, text: str) -> None:
    """Record a FK / PK / UNIQUE constraint definition under ``name``."""
    fk = _FK.search(text)
    if fk and text.lstrip().upper().startswith("FOREIGN KEY"):
        table.foreign_keys[name] = (_columns(fk.group(1)), fk.group(2))
        return
    key = _KEY.match(text.strip())
    if key:
        table.constraint_indexes[name] = _columns(key.group(2))


def _replay_create_table(tables: dict[str, TableState], match: re.Match[str], position: int) -> None:
    """Apply one ``CREATE TABLE`` statement (named/anonymous constraints and inline PK/UNIQUE)."""
    table = tables.setdefault(match.group(1), TableState())
    for item in _top_level_items(match.group(2)):
        named = re.match(r"CONSTRAINT " + _IDENT + r"\s+(.*)$", item, re.DOTALL)
        if named:
            _apply_constraint(table, named.group(1), named.group(2))
        elif re.match(r"(PRIMARY KEY|UNIQUE|FOREIGN KEY)\b", item):
            # PostgreSQL names an anonymous primary key ``<table>_pkey``; later migrations drop it by that name.
            anonymous = (
                f"{match.group(1)}_pkey" if item.startswith("PRIMARY KEY")
                else f"anon_{position}_{len(table.constraint_indexes)}_{len(table.foreign_keys)}"
            )
            _apply_constraint(table, anonymous, item)
        else:
            column = re.match(_IDENT + r"\s", item)
            if column and re.search(r"\bPRIMARY KEY\b|\bUNIQUE\b", item):
                key_name = f"{match.group(1)}_pkey" if "PRIMARY KEY" in item else f"col_{column.group(1)}"
                table.constraint_indexes[key_name] = (column.group(1),)


def _replay_alter(tables: dict[str, TableState], match: re.Match[str], position: int) -> None:
    """Apply one ``ALTER TABLE`` statement that adds or drops a constraint."""
    table = tables.setdefault(match.group(1), TableState())
    rest = match.group(2)
    if add := _ADD_CONSTRAINT.match(rest):
        _apply_constraint(table, add.group(1), add.group(2))
    elif drop := _DROP_CONSTRAINT.match(rest):
        table.foreign_keys.pop(drop.group(1), None)
        table.constraint_indexes.pop(drop.group(1), None)
    elif re.match(r"ADD (PRIMARY KEY|UNIQUE)", rest):
        _apply_constraint(table, f"alter_{position}", rest[4:])


def replay(sql: str) -> dict[str, TableState]:
    """Replay DDL statements in order and return the final per-table FK/index state."""
    tables: dict[str, TableState] = {}
    index_owner: dict[str, str] = {}
    for position, statement in enumerate(split_statements(sql)):
        if match := _CREATE_TABLE.match(statement):
            _replay_create_table(tables, match, position)
        elif match := _ALTER.match(statement):
            _replay_alter(tables, match, position)
        elif match := _CREATE_INDEX.match(statement):
            owner = tables.setdefault(match.group(2), TableState())
            index_owner[match.group(1)] = match.group(2)
            # A partial index only serves FK lookups when its predicate is just "<indexed column> IS NOT NULL"
            # (an equality lookup implies it); any other predicate cannot serve an unrestricted FK lookup.
            not_null = re.fullmatch(r"\s*WHERE\s+\"?(\w+)\"?\s+IS\s+NOT\s+NULL\s*", match.group(4) or "", re.IGNORECASE)
            if match.group(4) and not (not_null and (not_null.group(1),) == _columns(match.group(3))):
                owner.indexes.pop(match.group(1), None)
            else:
                owner.indexes[match.group(1)] = _columns(match.group(3))
        elif match := _DROP_INDEX.match(statement):
            owner_name = index_owner.pop(match.group(1), None)
            if owner_name:
                tables[owner_name].indexes.pop(match.group(1), None)
        elif match := _DROP_TABLE.match(statement):
            tables.pop(match.group(1), None)
    return tables


def unindexed_foreign_keys(
    sql: str, *, ignore_referenced: frozenset[str] = frozenset({"workspaces", "owner"}),
) -> list[tuple[str, tuple[str, ...], str]]:
    """Return ``(table, fk_columns, referenced_table)`` for every FK without a usable leading index.

    ``workspace_id`` is dropped from composite FK column sets first (a single-owner deployment has one
    workspace, so it never narrows a lookup); the remaining FK is covered when some index or key starts
    with one of its columns (or with exactly all of them). FKs to ``ignore_referenced`` parents (the one-row ``workspaces``/``owner``
    tables, deleted only by account erasure) are skipped by default; pass an empty set to list them too.
    """
    missing = []
    for name, table in sorted(replay(sql).items()):
        usable = [*table.indexes.values(), *table.constraint_indexes.values()]
        for columns, referenced in table.foreign_keys.values():
            if referenced in ignore_referenced:
                continue
            reduced = tuple(c for c in columns if c != "workspace_id") or columns
            full = set(columns)
            if not any(index and (index[0] in reduced or set(index[:len(columns)]) == full) for index in usable):
                missing.append((name, columns, referenced))
    return sorted(set(missing))


def render_upgrade_sql() -> str:
    """Render ``alembic upgrade base:head --sql`` offline (placeholder URL; no connection is made)."""
    # Settings validation runs even for offline rendering, so supply placeholder values for the required ones.
    env = {
        **os.environ,
        "DATABASE_URL": "postgresql+asyncpg://u:build-placeholder@postgres:5432/db",
        "REDIS_URL": "redis://:build-placeholder@redis:6379/0",
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "base:head", "--sql"],
        check=True, capture_output=True, text=True, env=env,
    )
    return result.stdout


def main() -> int:
    """CLI entry point: print uncovered foreign keys; always exits 0 (informational report)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sql", help="Analyse this rendered SQL file instead of rendering migrations")
    args = parser.parse_args()
    if args.sql:
        with open(args.sql, encoding="utf-8") as handle:
            sql = handle.read()
    else:
        sql = render_upgrade_sql()
    for table, columns, referenced in unindexed_foreign_keys(sql):
        print(f"{table} ({', '.join(columns)}) -> {referenced}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
