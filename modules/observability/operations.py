"""Compose content-free operational projections through their owning module contracts."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from modules.connectors import public as connectors
from modules.ingestion import public as ingestion
from modules.knowledge.documents import public as documents
from modules.knowledge.entities import public as entities
from modules.sources import public as sources


async def quality_summary(session: AsyncSession) -> dict[str, Any]:
    """Merge bounded owner-provided quality aggregates without querying private models here."""
    now = datetime.now(UTC)
    document_data = await documents.observability_quality_summary(session)
    ingestion_data = await ingestion.observability_quality_summary(session)
    entity_data = await entities.observability_quality_summary(session)
    source_data = await sources.observability_quality_summary(session, now=now)
    return {
        **document_data,
        **ingestion_data,
        **entity_data,
        **source_data,
        "graph_sync_lag_seconds": None,
        "generated_at": now,
    }


async def queue_summary(session: AsyncSession) -> dict[str, Any]:
    """Merge bounded queue-state projections from ingestion and connector owners."""
    ingestion_data = await ingestion.observability_queue_summary(session)
    connector_data = await connectors.observability_queue_summary(session)
    return {**ingestion_data, **connector_data, "payloads_included": False}
