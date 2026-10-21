"""Authorized, resumable account-data erasure across module-owned persistence."""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core import demo_seed as _demo_seed  # noqa: F401 - register core-owned erasure table
from core import realtime as _realtime  # noqa: F401 - register core-owned erasure tables
from core.auth.deletion_service import authorize_account_erasure
from core.auth.erasure_contracts import AccountErasureScope, ErasureProgress
from core.erasure_sql import AccountErasureOwner, account_erasure_order
from core.workspaces.erasure import erase_account_workspace_references
from modules.agents import erasure as agents
from modules.automations import erasure as automations
from modules.chat import erasure as chat
from modules.connectors import erasure as connectors
from modules.dashboard import erasure as dashboard
from modules.goals import erasure as goals
from modules.ingestion import erasure as ingestion
from modules.knowledge.documents import erasure as documents
from modules.knowledge.entities import erasure as entities
from modules.knowledge.observations import erasure as observations
from modules.knowledge.relationships import erasure as relationships
from modules.knowledge.temporal import erasure as temporal
from modules.memory import erasure as memory
from modules.news import erasure as news
from modules.notifications import erasure as notifications
from modules.observability import erasure as observability
from modules.search import erasure as search
from modules.settings import erasure as settings
from modules.sources import erasure as sources
from modules.tasks import erasure as tasks
from modules.timeline import erasure as timeline
from modules.tools import erasure as tools
from modules.translations import erasure as translations

_OWNERS = (
    agents, automations, chat, connectors, dashboard, goals, ingestion, documents,
    entities, observations, relationships, temporal, memory, news, notifications,
    observability, search, settings, sources, tasks, timeline, tools, translations,
)
_CORE_TABLES = ("demo_seed_receipts", "realtime_replay_head", "realtime_replay_events")
_CORE_OWNER = AccountErasureOwner(_CORE_TABLES)
_SPECIAL_TABLES = frozenset({
    "connector_admission_slots", "translation_admission_slots", "github_webhook_capacity",
    "github_webhook_deliveries", "github_webhook_outbox", "connector_quota_windows",
    "remote_heavy_guards", "maintenance_summary",
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
    if phase == "auxiliary":
        try:
            done, next_cursor = await erase_auxiliary_data(ctx, scope, cursor)
        except Exception as exc:
            raise AccountErasureStageError("auxiliary_cleanup_failed") from exc
        if done:
            return ErasureProgress("remote")
        return ErasureProgress("auxiliary", next_cursor)
    if phase == "remote":
        try:
            done, next_cursor = await connectors.erase_account_remote_page(ctx, scope, cursor)
        except Exception as exc:
            raise AccountErasureStageError("remote_cleanup_failed") from exc
        if done:
            return ErasureProgress("graph")
        return ErasureProgress("remote", next_cursor)
    if phase == "graph":
        try:
            done, next_cursor = await temporal.erase_account_graph_page(ctx, scope, cursor)
        except Exception as exc:
            raise AccountErasureStageError("graph_cleanup_failed") from exc
        if done:
            return ErasureProgress("storage")
        return ErasureProgress("graph", next_cursor)
    if phase == "storage":
        try:
            done, next_cursor = await documents.erase_account_storage_page(ctx, scope, cursor)
        except Exception as exc:
            raise AccountErasureStageError("storage_cleanup_failed") from exc
        if done:
            return ErasureProgress("shared")
        return ErasureProgress("storage", next_cursor)
    if phase == "shared":
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
    if phase == "database":
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
    if phase == "references":
        async with factory() as session:
            await settings.erase_account_settings_references(session, scope)
            await erase_account_workspace_references(session, scope)
            await session.commit()
        return ErasureProgress("verify")
    if phase == "verify":
        async with factory() as session:
            for name in account_erasure_tables():
                await _table_owner(name).verify_empty(session, scope, name)
            await connectors.verify_account_shared_rows(session, scope)
            await translations.verify_account_shared_rows(session, scope)
            await settings.verify_account_settings_references(session, scope)
            await session.rollback()
        return ErasureProgress("verify", complete=True)
    raise ValueError("Unknown account erasure phase")
