"""Regression: the graph dependency fingerprint reads the source fence, not the connector projection.

``_dependency_fingerprint`` once read ``local_only`` from ``get_connector_source`` (whose projection
has no such field), crashing every graph operation that carried a partition.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from modules.knowledge.temporal import worker
from modules.sources.schemas import SourceFence


def _config() -> SimpleNamespace:
    dump = SimpleNamespace(model_dump=lambda mode="json": {})
    return SimpleNamespace(
        configuration_revision=1, gateway_identity="g", aliases={}, privacy=dump,
        endpoint_destination_id=None,
    )


async def test_fingerprint_with_partition_uses_source_fence_and_changes_with_local_only() -> None:
    partition_id, source_id = uuid4(), uuid4()
    session = MagicMock()
    session.scalars = AsyncMock(return_value=MagicMock(all=list))
    session.get = AsyncMock(return_value=SimpleNamespace(source_id=source_id))
    ctx = {"settings": SimpleNamespace(
        graph_enabled=False, graph_host="h", graph_port=1, graph_database="d",
        graph_embedding_dimensions=1,
    ), "redis": None}

    async def fingerprint(local_only: bool) -> str:
        fence = SourceFence(id=source_id, status="active", generation=1, local_only=local_only)
        with patch.object(worker.settings_public, "get_ai_execution_config", AsyncMock(return_value=_config())), \
                patch.object(worker.sources, "get_source_fence", AsyncMock(return_value=fence)), \
                patch.object(worker.sources, "get_connector_source", AsyncMock(side_effect=AssertionError)):
            return await worker._dependency_fingerprint(session, ctx, "disabled", partition_id)  # type: ignore[arg-type]

    assert await fingerprint(False) != await fingerprint(True)
