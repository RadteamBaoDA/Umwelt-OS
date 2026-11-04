"""Authorized, resumable account-data erasure across module-owned persistence."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core import demo_seed as _demo_seed  # noqa: F401 - register core-owned erasure table
from core import realtime as _realtime  # noqa: F401 - register core-owned erasure tables
from core.auth.deletion_service import authorize_account_erasure
from core.auth.erasure_contracts import AccountErasureScope, ErasureProgress
from core.erasure_sql import AccountErasureOwner, account_erasure_order
from core.modules import erasure_descriptors
from core.workspaces.erasure import erase_account_workspace_references

# Erasure packages are resolved from module descriptors (ascending ``erasure_order``), so core
# names no module. Resolving at import keeps the historical side effect that loading this
# coordinator registers every module's persistence models with the shared metadata (recovery
# relies on it). The module-level aliases below are the stage hooks used by ``erase_account_data``.
_ERASURE_MODULES = {item.id: item.load_erasure() for item in erasure_descriptors()}
_OWNERS = tuple(_ERASURE_MODULES.values())
connectors = _ERASURE_MODULES["connectors"]
documents = _ERASURE_MODULES["knowledge.documents"]
settings = _ERASURE_MODULES["settings"]
temporal = _ERASURE_MODULES["knowledge.temporal"]
tools = _ERASURE_MODULES["tools"]
translations = _ERASURE_MODULES["translations"]
_CORE_TABLES = ("demo_seed_receipts", "realtime_replay_head", "realtime_replay_events")
_CORE_OWNER = AccountErasureOwner(_CORE_TABLES)
_SPECIAL_TABLES = frozenset({
    "connector_admission_slots", "translation_admission_slots", "github_webhook_capacity",
    "github_webhook_deliveries", "github_webhook_outbox", "connector_quota_windows",
    "remote_heavy_guards", "maintenance_summary", "system_health_samples", "system_history_settings",
})


class AccountErasureStageError(RuntimeError):
    """Carry one finite receipt-safe failure code; internal exception text is never persisted."""

    def __init__(self, safe_code: str) -> None:
        """Retain only the fixed stage category for Auth's retry status projection."""
        super().__init__(safe_code)
        self.safe_code = safe_code


def account_erasure_tables() -> tuple[str, ...]:
    """Return every ordinary domain table once, from owner-declared static inventories."""
    names = _CORE_TABLES + tuple(name for owner in _OWNERS for name in owner.ERASURE_TABLES)
    return account_erasure_order(tuple(name for name in names if name not in _SPECIAL_TABLES))


def _table_owner(table: str) -> AccountErasureOwner:
    """Resolve one registered module contract without importing its private models."""
    for owner in (_CORE_OWNER, *(module.ERASURE_OWNER for module in _OWNERS)):
        if table in owner.tables:
            return cast(AccountErasureOwner, owner)
    raise ValueError("Account-erasure table has no owner")


async def _run_paged_stage(
    call: Callable[[], Awaitable[tuple[bool, dict[str, Any]]]], error_code: str, phase: str, next_phase: str,
) -> ErasureProgress:
    """Invoke and await one cursor-paged owner stage, wrapping any failure as ``AccountErasureStageError``.

    Returns the next phase when the owner reports completion, otherwise the same phase with the
    owner's next cursor. No session is opened here; the owner call manages its own transactions.
    """
    try:
        done, next_cursor = await call()
    except Exception as exc:
        raise AccountErasureStageError(error_code) from exc
    if done:
        return ErasureProgress(next_phase)
    return ErasureProgress(phase, next_cursor)


async def _erase_shared_stage(
    factory: async_sessionmaker[AsyncSession], scope: AccountErasureScope, cursor: dict[str, Any],
) -> ErasureProgress:
    """Erase shared connector/tool/translation rows in one session, committing once at the end.

    Later owners run only after the earlier owner reports completion; the single commit happens
    after all executed owner calls. Any failure maps to ``shared_cleanup_failed``.
    """
    try:
        async with factory() as session:
            await authorize_account_erasure(session, scope)
            done, next_cursor = await connectors.erase_account_shared_rows(session, scope, cursor)
            if done:
                done = await tools.erase_account_remote_guards(session, scope)
                if done:
                    await translations.erase_account_shared_rows(session, scope)
            await session.commit()
    except Exception as exc:
        raise AccountErasureStageError("shared_cleanup_failed") from exc
    return ErasureProgress("database") if done else ErasureProgress("shared", next_cursor)


async def _erase_database_stage(
    factory: async_sessionmaker[AsyncSession], scope: AccountErasureScope, cursor: dict[str, Any],
) -> ErasureProgress:
    """Erase up to 16 inventory tables per call, one session and commit per table.

    The checkpoint is the index into ``account_erasure_tables()``; an invalid checkpoint raises
    ``ValueError``. Returns early on the first table that still held rows.
    """
    order = account_erasure_tables()
    index = cursor.get("table", 0)
    if type(index) is not int or not 0 <= index <= len(order):
        raise ValueError("Invalid account erasure table checkpoint")
    if index == len(order):
        return ErasureProgress("references")
    # Bounded empty-table skips avoid one 30-second worker tick per inventory table.
    try:
        for table_index in range(index, min(index + 16, len(order))):
            async with factory() as session:
                count = await _table_owner(order[table_index]).erase_page(session, scope, order[table_index])
                await session.commit()
            if count:
                return ErasureProgress("database", {"table": table_index})
    except Exception as exc:
        raise AccountErasureStageError("data_cleanup_failed") from exc
    next_index = min(index + 16, len(order))
    return ErasureProgress("references") if next_index == len(order) else ErasureProgress(
        "database", {"table": next_index},
    )


async def _erase_references_stage(
    factory: async_sessionmaker[AsyncSession], scope: AccountErasureScope,
) -> ErasureProgress:
    """Anonymize settings/workspace references in one session and commit once."""
    async with factory() as session:
        await settings.erase_account_settings_references(session, scope)
        await erase_account_workspace_references(session, scope)
        await session.commit()
    return ErasureProgress("verify")


async def _verify_erasure_stage(
    factory: async_sessionmaker[AsyncSession], scope: AccountErasureScope,
) -> ErasureProgress:
    """Verify every owner reports absence of account data; read-only, ends with a rollback."""
    async with factory() as session:
        for name in account_erasure_tables():
            await _table_owner(name).verify_empty(session, scope, name)
        await connectors.verify_account_shared_rows(session, scope)
        await translations.verify_account_shared_rows(session, scope)
        await settings.verify_account_settings_references(session, scope)
        await session.rollback()
    return ErasureProgress("verify", complete=True)


async def erase_account_data(
    ctx: dict[str, Any], scope: AccountErasureScope, phase: str, cursor: dict[str, Any],
) -> ErasureProgress:
    """Advance one durable deletion stage; report completion only after all owners verify absence.

    Each invocation validates the persisted operation and disabled account. External effects
    complete before canonical SQL rows are removed; owner stage APIs must be retry-safe and
    bounded. The worker serializes calls with a per-operation advisory lock and persists this
    returned checkpoint only after each stage commits. Personal references in surviving peer
    settings are anonymized explicitly because the bootstrap identity remains a tombstone.
    """
    from core.auth.deletion_auxiliary import erase_auxiliary_data

    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    async with factory() as session:
        await authorize_account_erasure(session, scope)
        await session.rollback()
    if phase == "queued":
        phase = "auxiliary"
    # Owner callables are resolved at call time so module-level patches stay effective.
    if phase == "auxiliary":
        return await _run_paged_stage(
            lambda: erase_auxiliary_data(ctx, scope, cursor), "auxiliary_cleanup_failed", phase, "remote",
        )
    if phase == "remote":
        return await _run_paged_stage(
            lambda: connectors.erase_account_remote_page(ctx, scope, cursor), "remote_cleanup_failed", phase, "graph",
        )
    if phase == "graph":
        return await _run_paged_stage(
            lambda: temporal.erase_account_graph_page(ctx, scope, cursor), "graph_cleanup_failed", phase, "storage",
        )
    if phase == "storage":
        return await _run_paged_stage(
            lambda: documents.erase_account_storage_page(ctx, scope, cursor), "storage_cleanup_failed", phase, "shared",
        )
    if phase == "shared":
        return await _erase_shared_stage(factory, scope, cursor)
    if phase == "database":
        return await _erase_database_stage(factory, scope, cursor)
    if phase == "references":
        return await _erase_references_stage(factory, scope)
    if phase == "verify":
        return await _verify_erasure_stage(factory, scope)
    raise ValueError("Unknown account erasure phase")
