"""Operator command: move stale n8n connector sources onto the current packaged template.

Usage (dry-run by default; ``--apply`` mutates)::

    python -m modules.connectors.upgrade_templates [--apply] [--limit 50] [--after SOURCE_ID]

Each stale source (n8n, idle, workflow installed, applied template older than current) runs the same
backend-transition saga the owner route uses with target ``n8n``, as the workspace owner under a freshly
read access fence (W2 internal-job scope). It never activates native. An unconfirmed stop of the old
workflow ends as ``reconciliation_required`` for the owner to resolve. A source that finishes the stop is
``upgraded``: it stays stopped until its owner presses Activate, which finalizes the new template.
"""

import argparse
import asyncio
import json
import sys
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.database import make_session_factory
from core.realtime import commit_with_replay, make_source_change
from core.workspaces.models import WorkspaceMembership
from core.workspaces.public import read_access_fence
from core.workspaces.schemas import InternalJobScope
from modules.connectors import provider_terms, provisioning, registry
from modules.connectors.backends import CURRENT_TEMPLATE_REVISION
from modules.connectors.models import ConnectorProvisioning
from modules.connectors.n8n import N8nApi
from modules.settings.public import module_is_enabled
from modules.sources import public as sources

_DENIED = frozenset({401, 403, 404, 409})
_STALE = (
    ConnectorProvisioning.execution_backend == "n8n",
    ConnectorProvisioning.transition_phase == "idle",
    ConnectorProvisioning.workflow_id.is_not(None),
    ConnectorProvisioning.applied_template_revision < CURRENT_TEMPLATE_REVISION,
)


async def _upgrade_one(
    factory: async_sessionmaker[AsyncSession], source_id: UUID, api: Any | None, *, apply: bool, flag: bool,
) -> str:
    """Return one of would_upgrade / upgraded / reconciliation_required / skipped for a single source."""
    async with factory() as session:
        try:
            workspace_id = await session.scalar(text("SELECT workspace_id FROM sources WHERE id = :id"), {"id": source_id})
            row = await session.get(ConnectorProvisioning, source_id)
            owner = await session.scalar(select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace_id, WorkspaceMembership.role == "owner",
            )) if workspace_id is not None else None
            if owner is None or row is None:
                await session.rollback()
                return "skipped"
            scope = InternalJobScope(
                workspace_id=workspace_id, actor_user_id=owner.user_id, membership_revision=owner.revision,
                source_id=source_id, source_generation=row.source_generation)
            revision = row.desired_revision
            await session.rollback()
            if not await module_is_enabled(session, "connectors", scope=scope, multi_workspace_enabled=flag):
                await session.rollback()
                return "skipped"
            fence = await read_access_fence(session, scope=scope, multi_workspace_enabled=flag)
            source = await sources.get_connector_source(session, source_id, multi_workspace_enabled=flag, scope=scope)
            if source is None or source.status != "active" or source.type not in registry.SUPPORTED_TYPES:
                await session.rollback()
                return "skipped"
            await provider_terms.require_terms_eligible(session, source)
            await session.rollback()
            if not apply:
                return "would_upgrade"
            begun = await provisioning.begin_backend_transition_in_uow(
                session, source_id, revision, "n8n", scope=scope, multi_workspace_enabled=flag, access_fence=fence)
            await commit_with_replay(session, [
                make_source_change(source.id, source.generation, source.status, connector_state=begun.state, scope=scope),
            ], access_fence=fence, multi_workspace_enabled=flag, scope=scope)
            phase = await provisioning.advance_backend_transition(
                session, source_id, api, scope=scope, multi_workspace_enabled=flag, access_fence=fence)
            return "reconciliation_required" if phase == "reconciliation_required" else "upgraded"
        except HTTPException as exc:
            await session.rollback()
            if exc.status_code not in _DENIED and exc.status_code != 422:
                raise
            return "skipped"


async def run(
    factory: async_sessionmaker[AsyncSession], settings: Settings, *, apply: bool = False, limit: int = 50,
    after: UUID | None = None,
) -> dict[str, Any]:
    """Process at most ``limit`` stale sources after ``after``; idempotent, safe to re-run with ``last_source_id``."""
    key = settings.n8n_api_key.get_secret_value()
    api = N8nApi(str(settings.n8n_service_url), key) if key else None
    statement = select(ConnectorProvisioning.source_id).where(*_STALE).order_by(ConnectorProvisioning.source_id).limit(limit)
    if after is not None:
        statement = statement.where(ConnectorProvisioning.source_id > after)
    async with factory() as session:
        ids = list((await session.scalars(statement)).all())
        await session.rollback()
    summary: dict[str, Any] = {
        "dry_run": not apply, "examined": len(ids), "upgraded": 0, "would_upgrade": 0,
        "reconciliation_required": 0, "skipped": 0,
        "last_source_id": str(ids[-1]) if ids else None, "more": len(ids) >= limit,
    }
    for source_id in ids:
        summary[await _upgrade_one(factory, source_id, api, apply=apply, flag=settings.multi_workspace_enabled)] += 1
    return summary


async def _main(args: argparse.Namespace) -> int:
    settings = Settings()
    engine, factory = make_session_factory(settings.database_url, pool_size=2, max_overflow=0)
    try:
        print(json.dumps(await run(factory, settings, apply=args.apply, limit=args.limit, after=args.after), indent=2))
    finally:
        await engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="mutate; default is a dry run")
    parser.add_argument("--limit", type=int, default=50, help="maximum sources examined per run")
    parser.add_argument("--after", type=UUID, default=None, help="resume after this source id")
    return asyncio.run(_main(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
