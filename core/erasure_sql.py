"""Bounded SQL deletion for explicit owner-declared account-erasure tables."""

from __future__ import annotations

from collections.abc import Collection
from functools import lru_cache
from typing import Any, cast

from sqlalchemy import Column, Table, and_, delete, exists, or_, select, tuple_
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from core.auth.deletion_service import authorize_account_erasure
from core.auth.erasure_contracts import AccountErasureScope
from core.database import Base


@lru_cache(maxsize=512)
def _ancestry(table_name: str, visiting: frozenset[str] = frozenset()) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    """Find only declared FK paths toward scoped parents, excluding cycles and global tables."""
    if table_name in visiting:
        return ()
    table = Base.metadata.tables[table_name]
    paths: list[tuple[str, tuple[tuple[str, str], ...]]] = []
    for constraint in table.foreign_key_constraints:
        parent = constraint.referred_table
        if parent.name in {"owner", "workspaces", "account_deletion_operations"}:
            continue
        if "workspace_id" not in parent.c and "owner_id" not in parent.c and not _ancestry(
            parent.name, visiting | {table_name}
        ):
            continue
        pairs = tuple((element.parent.name, element.column.name) for element in constraint.elements)
        paths.append((parent.name, pairs))
    return tuple(paths)


def account_row_predicate(table: Table, scope: AccountErasureScope, visiting: frozenset[str] = frozenset()) -> ColumnElement[bool]:
    """Bind a table row to the original workspace or owner through registered FK lineage.

    Workspace rows and content authored by the departing identity in a peer workspace are
    scoped independently. Other users' peer content survives. Unscoped global tables raise
    rather than silently deleting deployment-wide state.
    """
    if table.name in visiting:
        raise ValueError(f"Cyclic erasure ownership: {table.name}")
    if "workspace_id" in table.c:
        direct = [table.c.workspace_id == scope.workspace_id]
        for name in ("owner_id", "actor_user_id", "user_id"):
            if name in table.c:
                direct.append(table.c[name] == scope.account_id)
        return or_(*direct)
    predicates: list[ColumnElement[bool]] = []
    if "owner_id" in table.c:
        predicates.append(table.c.owner_id == scope.account_id)
    for parent_name, pairs in _ancestry(table.name):
        parent = Base.metadata.tables[parent_name]
        predicates.append(exists(select(1).select_from(parent).where(
            and_(*(table.c[child] == parent.c[ancestor] for child, ancestor in pairs)),
            account_row_predicate(parent, scope, visiting | {table.name}),
        )))
    if not predicates:
        raise ValueError(f"No scoped account-erasure path for {table.name}")
    return or_(*predicates)


async def delete_account_table_page(
    session: AsyncSession, scope: AccountErasureScope, table_name: str, *, limit: int = 100,
) -> int:
    """Delete at most one FK-safe page for one owner table, returning the removed row count.

    Caller validates the persisted deletion operation and commits each page before advancing
    its durable checkpoint. Primary-key paging retries safely after a crash, including when
    child rows disappear by database cascade. No unscoped table is accepted.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Account erasure page limit must be 1..100")
    table = Base.metadata.tables[table_name]
    predicate = account_row_predicate(table, scope)
    primary_key: tuple[Column[Any], ...] = tuple(table.primary_key.columns)
    if not primary_key:
        raise ValueError(f"Account erasure table has no primary key: {table_name}")
    selected = select(*primary_key).where(predicate).limit(limit)
    identity = primary_key[0] if len(primary_key) == 1 else tuple_(*primary_key)
    result = await session.execute(delete(table).where(identity.in_(selected)))
    return cast(CursorResult[Any], result).rowcount or 0


def account_erasure_order(owned_tables: Collection[str]) -> tuple[str, ...]:
    """Order explicit owner tables child-first, rejecting unknown names and duplicate claims."""
    declared = set(owned_tables)
    if len(declared) != len(owned_tables) or declared - Base.metadata.tables.keys():
        raise ValueError("Duplicate or unregistered account-erasure table")
    return tuple(table.name for table in reversed(Base.metadata.sorted_tables) if table.name in declared)


class AccountErasureOwner:
    """Expose a module's finite table inventory as authorized, bounded write operations.

    This contract stays with each owning module; the coordinator chooses the stage but cannot
    import or mutate another module's private persistence models. Every call revalidates the
    persisted operation and original disabled account before SQL mutation or verification.
    """

    def __init__(self, tables: tuple[str, ...]) -> None:
        """Reject duplicate or unknown owner-table declarations at registration time."""
        if len(set(tables)) != len(tables) or set(tables) - Base.metadata.tables.keys():
            raise ValueError("Invalid account-erasure owner table inventory")
        self.tables = tables

    async def erase_page(self, session: AsyncSession, scope: AccountErasureScope, table: str) -> int:
        """Remove one owner-scoped page after operation authority and table ownership checks."""
        if table not in self.tables:
            raise PermissionError("Table is outside this erasure owner")
        await authorize_account_erasure(session, scope)
        return await delete_account_table_page(session, scope, table)

    async def verify_empty(self, session: AsyncSession, scope: AccountErasureScope, table: str) -> None:
        """Fail completion when one owner-scoped row remains in a declared table."""
        if table not in self.tables:
            raise PermissionError("Table is outside this erasure owner")
        await authorize_account_erasure(session, scope)
        own_table = Base.metadata.tables[table]
        if await session.scalar(select(1).select_from(own_table).where(
            account_row_predicate(own_table, scope)
        ).limit(1)):
            raise RuntimeError("Account erasure domain row remains")
