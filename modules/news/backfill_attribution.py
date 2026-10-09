"""Backfill ``match_evidence.attribution`` on News observations created before it was recorded.

Run ``python -m modules.news.backfill_attribution [--limit N] [--apply]``; the default is a dry run.
Values come only from the document version's ``ProviderRecordMetadata.source_fields`` via the same
``_attribution`` helper used at observation time; a source with no publisher/license stays absent.
Rows are paged by keyset (500 per page) with the workspace predicate on every statement, and the
write is guarded so a row that already has an attribution is never touched (idempotent).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any, cast
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import cast as sql_cast
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.database import make_session_factory
from modules.knowledge.documents.models import NormalizedVersionProvenance as Provenance
from modules.knowledge.documents.schemas import ProviderRecordMetadata
from modules.news.models import NewsObservation
from modules.news.stories import _attribution

PAGE_SIZE = 500
_record = Provenance.provenance_json["provider_record"]
# Only rows whose source reports a non-blank publisher or a license label can gain a value, so
# unresolvable rows are never rescanned and a finished backfill is a no-op.
_HAS_SOURCE_VALUE = or_(
    func.btrim(func.coalesce(_record["source_fields"]["publisher"].astext, "")) != "",
    func.coalesce(_record["license_label"].astext, "") != "",
)
_MISSING = ~NewsObservation.match_evidence.has_key("attribution")


def _pending(workspace_id: UUID) -> Any:
    """Observations of one workspace that lack attribution and have a provider value to copy."""
    return (
        select(NewsObservation.id, Provenance.provenance_json)
        .join(Provenance, (Provenance.document_version_id == NewsObservation.document_version_id)
              & (Provenance.document_id == NewsObservation.document_id))
        .where(NewsObservation.workspace_id == workspace_id, _MISSING, _HAS_SOURCE_VALUE)
    )


async def backfill_workspace(
    session: AsyncSession, workspace_id: UUID, *, apply: bool = False, limit: int | None = None,
) -> dict[str, int]:
    """Page one workspace's missing rows; return counts of rows examined and written (or writable)."""
    after: UUID | None = None
    examined = filled = 0
    while limit is None or examined < limit:
        page = min(PAGE_SIZE, PAGE_SIZE if limit is None else limit - examined)
        query = _pending(workspace_id).order_by(NewsObservation.id).limit(page)
        if after is not None:
            query = query.where(NewsObservation.id > after)
        rows = (await session.execute(query)).all()
        if not rows:
            break
        for observation_id, provenance in rows:
            raw = cast(dict[str, Any], provenance).get("provider_record")
            try:
                attribution = _attribution(ProviderRecordMetadata.model_validate(raw)) if raw else {}
            except ValidationError:
                attribution = {}
            if not attribution:
                continue
            filled += 1
            if apply:
                await session.execute(
                    update(NewsObservation)
                    .where(NewsObservation.id == observation_id, NewsObservation.workspace_id == workspace_id, _MISSING)
                    .values(match_evidence=func.jsonb_set(
                        NewsObservation.match_evidence, "{attribution}", sql_cast(json.dumps(attribution), JSONB), True,
                    ))
                )
        examined += len(rows)
        after = rows[-1][0]
        if apply:
            await session.commit()
    return {"examined": examined, "filled": filled}


async def backfill_attribution(
    factory: async_sessionmaker[AsyncSession], *, apply: bool = False, limit: int | None = None,
    workspace_limit: int = 20,
) -> dict[str, int]:
    """Backfill every workspace that still has fillable rows (bounded per call)."""
    async with factory() as session:
        workspace_ids = list((await session.scalars(
            select(NewsObservation.workspace_id).join(
                Provenance, (Provenance.document_version_id == NewsObservation.document_version_id)
                & (Provenance.document_id == NewsObservation.document_id),
            ).where(_MISSING, _HAS_SOURCE_VALUE).distinct().limit(workspace_limit)
        )).all())
    total = {"workspaces": len(workspace_ids), "examined": 0, "filled": 0}
    for workspace_id in workspace_ids:
        async with factory() as session:
            result = await backfill_workspace(session, workspace_id, apply=apply, limit=limit)
        total["examined"] += result["examined"]
        total["filled"] += result["filled"]
    return total


async def backfill_news_attribution(ctx: dict[str, object]) -> int:
    """Worker cron: write at most a bounded batch per run; a no-op once every row is filled."""
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    return (await backfill_attribution(factory, apply=True, limit=PAGE_SIZE * 4))["filled"]


async def _main(args: argparse.Namespace) -> int:
    engine, factory = make_session_factory(Settings().database_url, pool_size=2, max_overflow=0)
    try:
        print(json.dumps(await backfill_attribution(factory, apply=args.apply, limit=args.limit), indent=2))
    finally:
        await engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Operator entrypoint; dry run unless ``--apply``."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write; default is a dry run")
    parser.add_argument("--limit", type=int, default=None, help="maximum rows examined per workspace")
    return asyncio.run(_main(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
