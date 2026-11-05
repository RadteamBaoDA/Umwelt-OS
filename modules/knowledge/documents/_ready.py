"""Document-ready event publication after extraction or content writes."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from core.events import DomainEvent
from core.ids import uuid7
from core.workspaces.schemas import (
    AccessFence,
    Scope,
)
from modules.knowledge.documents._identity import _normalized_source_proof
from modules.knowledge.documents.models import (
    Document,
    DocumentVersion,
)
from modules.sources.public import SourceFence


async def _publish_extraction_ready(
    session: AsyncSession, document: Document, version: DocumentVersion, *,
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
) -> None:
    """Publish the two Documents-owned parser ready events under original held proof.

    Use only exact local Document/version writes and captured Source generation.
    Ingestion public revalidates scope nonlockingly and inserts new outbox rows;
    no old claim is acquired, no Source ORM escapes and no commit or I/O occurs.
    """
    from modules.ingestion import public as ingestion

    await _normalized_source_proof(
        session, document.source_id, source_fence.generation, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence,
    )
    payload = {"source_id": str(document.source_id), "document_id": str(document.id),
               "document_version_id": str(version.id), "source_generation": source_fence.generation,
               "version_number": version.version_number}
    for event_type in ("document.version.ready", "news.document.ready"):
        await ingestion.publish_event(session, DomainEvent(
            id=uuid7(), type=event_type, version=1, occurred_at=datetime.now(UTC),
            producer="modules.knowledge.documents", payload=payload,
        ), scope=scope, multi_workspace_enabled=multi_workspace_enabled)


async def _publish_document_ready(
    session: AsyncSession, document: Document, version: DocumentVersion, *,
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
) -> None:
    """Publish separate Documents ready events through the accepted held D0 publisher.

    Manual writers hold exact Source/Document and original access proof; current pointer
    already selects this immutable version. Ingestion owns the scoped eight-field event
    enrichment. No foreign Source ORM, parent lock reacquisition, commit or external I/O.
    The legacy backfill caller prepares the same mandatory proof per version.
    """
    await _publish_extraction_ready(session, document, version, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence)
