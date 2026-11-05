"""Exact-ID graph read helpers shared by the delete and reconcile step modules.

Each helper performs bounded reads through the partition-scoped driver the caller
passes in and never mutates the graph.
"""

from __future__ import annotations

from typing import Any

from modules.knowledge.temporal._fingerprints import _require_group, _safe_error
from modules.knowledge.temporal._types import (
    MAX_SUPPORT,
    ExactGraphLink,
    GraphOperationError,
    OperationAuthorization,
)

_LINKS_QUERY = (
    "MATCH (source)-[edge]-(entity:Entity {uuid: $entity_id, group_id: $group_id}) "
    "RETURN edge.uuid AS edge_id, type(edge) AS relationship_type, "
    "startNode(edge).uuid AS source_node_id, endNode(edge).uuid AS target_node_id, "
    "edge.episodes AS episode_ids ORDER BY edge.uuid LIMIT $limit"
)
_ENDPOINTS_QUERY = (
    "MATCH (node:Entity {group_id: $group_id}) WHERE node.uuid IN $node_ids "
    "RETURN node.uuid AS node_id, node.group_id AS group_id "
    "ORDER BY node.uuid LIMIT $limit"
)


def _node_absent(exc: Exception) -> bool:
    """Report whether a Graphiti lookup failure is the benign NodeNotFoundError (matched by name)."""
    return type(exc).__name__ == "NodeNotFoundError"


async def _read_exact_links(
    driver: Any,
    context: OperationAuthorization,
    entity_id: str,
    error: str,
) -> tuple[ExactGraphLink, ...]:
    """Read and validate the complete bounded incident-link set of one entity node.

    ``error`` is the stable category raised for an over-bound, malformed or
    duplicated inventory, so callers keep their distinct error codes.
    """
    rows, _, _ = await driver.execute_query(
        _LINKS_QUERY,
        entity_id=entity_id,
        group_id=context.group_id,
        limit=MAX_SUPPORT + 1,
    )
    if not isinstance(rows, list) or len(rows) > MAX_SUPPORT:
        raise GraphOperationError(error)
    links: list[ExactGraphLink] = []
    for row in rows:
        support = row.get("episode_ids") or () if isinstance(row, dict) else ()
        if (
            not isinstance(row, dict)
            or not isinstance(support, list | tuple)
            or not isinstance(row.get("relationship_type"), str)
        ):
            raise GraphOperationError(error)
        links.append(
            ExactGraphLink(
                edge_id=str(row.get("edge_id")),
                relationship_type=row["relationship_type"],
                source_node_id=str(row.get("source_node_id")),
                target_node_id=str(row.get("target_node_id")),
                episode_ids=tuple(sorted(str(value) for value in support)),
            )
        )
    if len({item.edge_id for item in links}) != len(links):
        raise GraphOperationError(error)
    return tuple(links)


async def _load_action_entity(
    driver: Any,
    context: OperationAuthorization,
    entity_id: str,
) -> Any:
    """Load a recovery action's entity with its embedding; None only when Graphiti reports it absent.

    Any other failure is re-mapped to a sanitized ``GraphOperationError``; a found
    entity must belong to the authorized group.
    """
    from graphiti_core.nodes import EntityNode

    entity = None
    try:
        entity = await EntityNode.get_by_uuid(driver, entity_id)
        await entity.load_name_embedding(driver)
    except Exception as exc:  # noqa: BLE001  # error boundary: re-mapped to a sanitized error
        if not _node_absent(exc):
            raise _safe_error(exc) from None
    if entity is not None:
        _require_group([entity], context.group_id)
    return entity


async def _endpoints_present(
    driver: Any,
    context: OperationAuthorization,
    endpoint_ids: list[str],
) -> bool:
    """Report whether every exact endpoint node exists once, inside the authorized group."""
    rows, _, _ = await driver.execute_query(
        _ENDPOINTS_QUERY,
        node_ids=endpoint_ids,
        group_id=context.group_id,
        limit=len(endpoint_ids) + 1,
    )
    return not (
        not isinstance(rows, list)
        or len(rows) != len(endpoint_ids)
        or len({row.get("node_id") for row in rows if isinstance(row, dict)}) != len(endpoint_ids)
        or any(not isinstance(row, dict) or row.get("group_id") != context.group_id for row in rows)
    )


def _replacement_edge(snapshot: Any) -> Any:
    """Rebuild the owner-approved replacement fact as a Graphiti edge (for fingerprinting or saving)."""
    from graphiti_core.edges import EntityEdge

    return EntityEdge(
        uuid=snapshot.fact_id,
        group_id=snapshot.group_id,
        source_node_uuid=snapshot.source_node_id,
        target_node_uuid=snapshot.target_node_id,
        name=snapshot.name,
        fact=snapshot.fact,
        episodes=list(snapshot.episode_ids),
        created_at=snapshot.created_at,
        reference_time=snapshot.reference_time,
        valid_at=snapshot.valid_at,
        invalid_at=snapshot.invalid_at,
        expired_at=snapshot.expired_at,
        fact_embedding=list(snapshot.fact_embedding),
        attributes=dict(snapshot.attributes),
    )
