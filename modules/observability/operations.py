"""Compose content-free operational projections through their owning module contracts."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from modules.connectors import public as connectors
from modules.ingestion import public as ingestion
from modules.knowledge.documents import public as documents
from modules.knowledge.entities import public as entities
from modules.sources import public as sources


async def quality_summary(session: AsyncSession, *, instance_operator: bool) -> dict[str, Any]:
    """Merge bounded owner-provided quality aggregates without querying private models here.

    ``instance_operator`` must come from an actual ``require_owner`` route, never a client field;
    each owner function re-denies anything but True.
    """
    now = datetime.now(UTC)
    document_data = await documents.observability_quality_summary(session, instance_operator=instance_operator)
    ingestion_data = await ingestion.observability_quality_summary(session, instance_operator=instance_operator)
    entity_data = await entities.observability_quality_summary(session, instance_operator=instance_operator)
    source_data = await sources.observability_quality_summary(session, instance_operator=instance_operator, now=now)
    return {
        **document_data,
        **ingestion_data,
        **entity_data,
        **source_data,
        "graph_sync_lag_seconds": None,
        "generated_at": now,
    }


async def queue_summary(session: AsyncSession, *, instance_operator: bool) -> dict[str, Any]:
    """Merge bounded queue-state projections from ingestion and connector owners (operator only)."""
    ingestion_data = await ingestion.observability_queue_summary(session, instance_operator=instance_operator)
    connector_data = await connectors.observability_queue_summary(session, instance_operator=instance_operator)
    return {**ingestion_data, **connector_data, "payloads_included": False}
