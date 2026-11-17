"""Connector status and provider/connector Source listing reads."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.schemas import Scope
from modules.sources._fences import _connector_source
from modules.sources._scope import _admit_source_scope, _source_scope
from modules.sources.models import Source
from modules.sources.schemas import ConnectorSource


@dataclass(frozen=True)
class ConnectorStatusRead:
    """Detached read-only source identity and collection-health fields for connector status listings."""

    id: UUID
    name: str
    type: str
    provider: str | None
    last_success_at: datetime | None
    last_error_at: datetime | None
    last_error_code: str | None
    collected_at: datetime | None


async def list_connector_status(
    session: AsyncSession,
    source_ids: Sequence[UUID],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> dict[UUID, ConnectorStatusRead]:
    """Read-only name/type/health projection for at most 200 workspace Sources, keyed by id.

    Owner admission and workspace scoping are enforced here; ids outside the scope are simply absent.
    No configuration, content or write authority is exposed. No locks.
    """
    await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not source_ids:
        return {}
    if len(source_ids) > 200:
        raise ValueError("Connector status listing is limited to 200 sources")
    rows = (
        await session.execute(
            select(
                Source.id,
                Source.name,
                Source.type,
                Source.provider,
                Source.last_success_at,
                Source.last_error_at,
                Source.last_error_code,
                Source.collected_at,
            ).where(Source.id.in_(list(source_ids)), *_source_scope(scope))
        )
    ).all()
    return {row.id: ConnectorStatusRead(*row) for row in rows}


async def _select_provider_sources(
    session: AsyncSession,
    providers: frozenset[str],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[Source]:
    """Admit the scope once, then select non-archived workspace Sources for ``providers``, oldest first.

    Shared by the ORM and DTO listings so both keep the same bound (500 rows), filters and order.
    """
    await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return list(
        (
            await session.scalars(
                select(Source)
                .where(
                    Source.provider.in_(sorted(providers)),
                    Source.status != "archived",
                    *_source_scope(scope),
                )
                .order_by(Source.created_at, Source.id)
                .limit(500)
                .execution_options(populate_existing=True)
            )
        ).all()
    )


async def list_provider_sources(
    session: AsyncSession,
    providers: frozenset[str],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[Source]:
    """Non-archived owner-workspace Sources whose provider is in ``providers``, oldest first.

    Read only, no locks. Bounded by one row per (provider, re-created source) in a single workspace;
    callers pick the oldest per provider so a toggled preset keeps reusing its original Source.
    """
    return await _select_provider_sources(
        session, providers, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )


async def list_provider_connector_sources(
    session: AsyncSession,
    providers: frozenset[str],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[ConnectorSource]:
    """Public connector DTOs for the non-archived workspace Sources of ``providers``, oldest first.

    Same filters, order and 500-row bound as ``list_provider_sources`` but with one access fence and
    one select for the whole provider set; read only, no locks. Rows are fresh (populate_existing).
    """
    return [
        _connector_source(row)
        for row in await _select_provider_sources(
            session, providers, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
    ]


async def get_connector_sources(
    session: AsyncSession,
    source_ids: frozenset[UUID],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> dict[UUID, ConnectorSource]:
    """Batch ``get_connector_source``: DTOs keyed by id for the ids visible in ``scope`` (missing ids absent).

    One access fence and one ``IN`` select, read only, no locks; bounded to 200 ids.
    """
    if len(source_ids) > 200:
        raise ValueError("Connector source batch is limited to 200 sources")
    await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not source_ids:
        return {}
    rows = await session.scalars(
        select(Source)
        .where(Source.id.in_(list(source_ids)), *_source_scope(scope))
        .execution_options(populate_existing=True)
    )
    return {row.id: _connector_source(row) for row in rows}
