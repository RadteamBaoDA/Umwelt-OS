"""Explicit, fictional Phase 1 demo data."""

import asyncio
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.config import Settings
from modules.knowledge.documents.models import Document, DocumentVersion
from modules.knowledge.documents.public import content_hash
from modules.sources import public as sources
from core.auth.public import get_demo_owner_id
from core.demo_seed import P08_DEMO_NAMESPACE, claim_demo_seed, record_demo_seed_receipt
from modules.goals.public import ensure_demo_goals
from modules.news.public import ensure_demo_topics
from modules.tasks.public import ensure_demo_tasks

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
    """Report fictional seed counts and whether this invocation initialized P08 fixtures."""
    created: int
    existing: int
    p08_seeded: bool


async def seed_demo(session: AsyncSession) -> SeedReport:
    """Create Phase 1 and owner-scoped P08 fixtures transactionally without replaying edits.

    P08 has an independent per-owner receipt and transaction lock, so an existing Phase 1
    sentinel cannot suppress first-time P08 setup. Owner seed helpers flush only; the lock
    remains held until this transaction commits, and the receipt prevents later resurrection
    of hard-deleted goals or detached links.
    """
    document_ids = tuple(uuid5(NAMESPACE_URL, f"{DEMO_NAMESPACE}/{key}") for key, _, _ in NOTES)
    async with session.begin():
        owner_id = await get_demo_owner_id(session)
        p08_seeded = await claim_demo_seed(session, owner_id, P08_DEMO_NAMESPACE)
        p08_created = 0
        p08_existing = 0
        if p08_seeded:
            goals_created, goals_existing = await ensure_demo_goals(session, owner_id)
            tasks_created, tasks_existing = await ensure_demo_tasks(session, owner_id)
            topics_created, topics_existing = await ensure_demo_topics(session, owner_id)
            p08_created = goals_created + tasks_created + topics_created
            p08_existing = goals_existing + tasks_existing + topics_existing
            await record_demo_seed_receipt(session, owner_id, P08_DEMO_NAMESPACE)
        inserted = await sources.ensure_demo_source(session, SOURCE_ID, DEMO_NAMESPACE)
        if not inserted:
            document_count = await session.scalar(
                select(func.count())
                .select_from(Document)
                .where(Document.source_id == SOURCE_ID, Document.id.in_(document_ids))
            )
            return SeedReport(
                created=p08_created,
                existing=1 + (document_count or 0) + p08_existing,
                p08_seeded=p08_seeded,
            )

        for (key, title, content), document_id in zip(NOTES, document_ids, strict=True):
            digest = content_hash(content)
            session.add(
                Document(
                    id=document_id,
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
        created=1 + len(NOTES) + p08_created,
        existing=p08_existing,
        p08_seeded=p08_seeded,
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
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_run_seed())
