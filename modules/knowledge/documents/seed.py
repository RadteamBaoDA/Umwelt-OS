"""Explicit, fictional Phase 1 demo data."""

import asyncio
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.auth.public import get_active_account, get_demo_owner_id
from core.config import Settings
from core.demo_seed import (
    P08_DEMO_NAMESPACE,
    P10_DEMO_NAMESPACE,
    P12_DEMO_NAMESPACE,
    claim_demo_seed,
    record_demo_seed_receipt,
)
from core.modules import register_modules
from core.workspaces.public import resolve_workspace_owner_context
from core.workspaces.schemas import InternalJobScope
from modules.automations.seed import ensure_demo_automations
from modules.chat.public import ensure_demo_conversation
from modules.goals.public import ensure_demo_goals
from modules.knowledge.documents import public as documents
from modules.knowledge.documents.models import Document, DocumentVersion
from modules.knowledge.documents.public import content_hash
from modules.knowledge.entities.public import ensure_demo_entities
from modules.knowledge.relationships.public import ensure_demo_relationships
from modules.news.public import ensure_demo_topics
from modules.sources import public as sources
from modules.tasks.public import ensure_demo_tasks
from modules.timeline.public import ensure_demo_events

DEMO_NAMESPACE = "bbd-os.demo.phase-1"
SOURCE_ID = uuid5(NAMESPACE_URL, f"{DEMO_NAMESPACE}/source")
NOTES = (
    (
        "project-note",
        "Orchard lantern project",
        "Fictional project: Mira plans to catalogue the lanterns in a storybook orchard.",
    ),
    (
        "reading-note",
        "Paper boats reading note",
        "Fictional note: Jun read an article about folding paper boats for a village festival.",
    ),
)


@dataclass(frozen=True)
class SeedReport:
    """Report fictional seed counts and whether this invocation initialized owner-scoped fixtures."""
    created: int
    existing: int
    p08_seeded: bool
    p12_seeded: bool
    skipped: int


async def seed_demo(session: AsyncSession) -> SeedReport:
    """Create Phase 1, P08/P10, and P12 fictional fixtures without replaying edits or deletions.

    Each phase has an independent per-workspace receipt and transaction lock, so the original source
    sentinel cannot suppress new fixtures. The P12 owner helpers flush only; its receipt commits
    with every fixture and prevents later resurrection of hard-deleted owner rows. Chat history is
    added only when current Memory consent permits durable storage. The configured demo account's
    active default workspace is resolved explicitly; no workspace epoch is inferred by a module helper.
    """
    document_ids = tuple(uuid5(NAMESPACE_URL, f"{DEMO_NAMESPACE}/{key}") for key, _, _ in NOTES)
    settings = Settings()
    async with session.begin():
        owner_id = await get_demo_owner_id(session)
        account = await get_active_account(
            session, owner_id, multi_workspace_enabled=settings.multi_workspace_enabled,
        )
        if account is None:
            raise RuntimeError("Demo seeding requires an active owner account")
        workspace_owner = await resolve_workspace_owner_context(
            session, account.default_workspace_id,
            multi_workspace_enabled=settings.multi_workspace_enabled,
        )
        if workspace_owner is None or workspace_owner.user_id != owner_id:
            raise RuntimeError("Demo seeding requires the configured owner's default workspace")
        scope = InternalJobScope(
            workspace_id=account.default_workspace_id,
            actor_user_id=workspace_owner.user_id,
            membership_revision=workspace_owner.membership_revision,
        )
        p08_seeded = await claim_demo_seed(
            session, owner_id, P08_DEMO_NAMESPACE, workspace_id=scope.workspace_id,
        )
        p08_created = 0
        p08_existing = 0
        if p08_seeded:
            goals_created, goals_existing = await ensure_demo_goals(
                session, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled,
            )
            tasks_created, tasks_existing = await ensure_demo_tasks(
                session, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled,
            )
            topics_created, topics_existing = await ensure_demo_topics(
                session, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled,
            )
            p08_created = goals_created + tasks_created + topics_created
            p08_existing = goals_existing + tasks_existing + topics_existing
            await record_demo_seed_receipt(
                session, owner_id, P08_DEMO_NAMESPACE, workspace_id=scope.workspace_id,
            )
        if await claim_demo_seed(session, owner_id, P10_DEMO_NAMESPACE, workspace_id=scope.workspace_id):
            # Disabled fictional automation examples; the receipt stops edited/deleted ones returning.
            p08_created += await ensure_demo_automations(
                session, register_modules(), settings,
                scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled,
            )
            await record_demo_seed_receipt(
                session, owner_id, P10_DEMO_NAMESPACE, workspace_id=scope.workspace_id,
            )
        p12_seeded = await claim_demo_seed(
            session, owner_id, P12_DEMO_NAMESPACE, workspace_id=scope.workspace_id,
        )
        p12_created = 0
        p12_existing = 0
        p12_skipped = 0
        if p12_seeded:
            for helper in (
                ensure_demo_entities,
                ensure_demo_relationships,
                ensure_demo_events,
            ):
                created, existing = await helper(
                    session, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled,
                )
                p12_created += created
                p12_existing += existing
            article_created, article_existing, article_skipped = await documents.ensure_demo_article(
                session, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled,
            )
            conversation_created, conversation_existing, conversation_skipped = await ensure_demo_conversation(
                session, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled,
            )
            p12_created += article_created + conversation_created
            p12_existing += article_existing + conversation_existing
            p12_skipped += article_skipped + conversation_skipped
            await record_demo_seed_receipt(
                session, owner_id, P12_DEMO_NAMESPACE, workspace_id=scope.workspace_id,
            )
        inserted = await sources.ensure_demo_source(
            session, SOURCE_ID, DEMO_NAMESPACE, scope=scope,
            multi_workspace_enabled=settings.multi_workspace_enabled,
        )
        if not inserted:
            document_count = await session.scalar(
                select(func.count())
                .select_from(Document)
                .where(Document.source_id == SOURCE_ID, Document.id.in_(document_ids))
            )
            return SeedReport(
                created=p08_created + p12_created,
                existing=1 + (document_count or 0) + p08_existing + p12_existing,
                p08_seeded=p08_seeded,
                p12_seeded=p12_seeded,
                skipped=p12_skipped,
            )

        for (key, title, content), document_id in zip(NOTES, document_ids, strict=True):
            digest = content_hash(content)
            session.add(
                Document(
                    id=document_id,
                    workspace_id=scope.workspace_id,
                    source_id=SOURCE_ID,
                    external_id=f"{DEMO_NAMESPACE}/{key}",
                    title=title,
                    metadata_json={"demo_namespace": DEMO_NAMESPACE},
                    current_version=1,
                    content_hash=digest,
                )
            )
            session.add(
                DocumentVersion(
                    id=uuid5(NAMESPACE_URL, f"{DEMO_NAMESPACE}/{key}/version-1"),
                    document_id=document_id,
                    version_number=1,
                    content=content,
                    content_hash=digest,
                )
            )
    return SeedReport(
        created=1 + len(NOTES) + p08_created + p12_created,
        existing=p08_existing + p12_existing,
        p08_seeded=p08_seeded,
        p12_seeded=p12_seeded,
        skipped=p12_skipped,
    )


async def _run_seed() -> None:
    """Run explicit demo seeding in a database session and dispose the engine."""
    engine = create_async_engine(Settings().database_url, pool_pre_ping=True)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            report = await seed_demo(session)
        print(
            f"Demo seed: created={report.created}, existing={report.existing}, "
            f"p08_seeded={report.p08_seeded}"
            f", p12_seeded={report.p12_seeded}, skipped={report.skipped}"
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_run_seed())
