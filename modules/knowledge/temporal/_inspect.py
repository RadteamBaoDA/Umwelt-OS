"""Step helpers for ``TemporalGraph.inspect_write_receipt``.

The adapter method keeps the fence, timeout, owner authorization and exception
mapping. These helpers hold request validation, the exact-ID reads and the
inspection assembly, in the original order. Everything is read-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from modules.knowledge.temporal._fingerprints import (
    _entity_state_fingerprint,
    _episode_state_fingerprint,
    _fact_recovery_fingerprint,
    _fact_state,
)
from modules.knowledge.temporal._types import (
    _UUID,
    MAX_RECOVERY_WITNESSES,
    MAX_SUPPORT,
    CanonicalNodeRecoveryAction,
    ExactGraphLink,
    GraphOperationError,
    GraphReceiptInspection,
    GraphWriteReceipt,
    OperationAuthorization,
)

_OUTSIDE = "graph_receipt_outside_authorized_partition"


@dataclass(frozen=True)
class _InspectScope:
    """Validated receipts plus the exact ids to read."""

    receipts: tuple[GraphWriteReceipt, ...]
    node_recovery: bool
    episode_id: str
    entity_ids: tuple[str, ...]
    mention_ids: tuple[str, ...]
    fact_ids: tuple[str, ...]


@dataclass(frozen=True)
class _InspectRead:
    """Raw exact-ID reads for one inspection."""

    episodes: list[Any]
    entities: list[Any]
    mentions: list[Any]
    facts: list[Any]
    incident_links: list[ExactGraphLink]


def _receipts_in_partition(
    receipt: GraphWriteReceipt | tuple[GraphWriteReceipt, ...], context: OperationAuthorization,
    actions: tuple[CanonicalNodeRecoveryAction, ...],
) -> tuple[tuple[GraphWriteReceipt, ...], bool, str]:
    """Return (receipts, node_recovery, episode_id) after partition/lease/owner-callback checks."""
    receipts = (receipt,) if isinstance(receipt, GraphWriteReceipt) else receipt
    if not 1 <= len(receipts) <= MAX_RECOVERY_WITNESSES:
        raise GraphOperationError(_OUTSIDE)
    node_recovery = bool(actions) or any(
        item.phase == "canonical_node_write_intent"
        or (item.phase == "cleanup_write_intent" and not item.prior_episode_state_fingerprint)
        for item in receipts
    )
    receipt_episode_ids = {str(item.episode_id) for item in receipts}
    if (len(receipt_episode_ids) != 1
            or any(item.group_id != context.group_id or item.operation_id != context.operation_id
                   or item.lease_token != context.lease_token
                   for item in receipts)
            or not receipt_episode_ids <= set(context.partition_episode_ids)
            or (node_recovery and (context.authorize_node_recovery is None or not actions))
            or (not node_recovery and len(receipts) != 1 and context.receipt_aggregate is None)):
        raise GraphOperationError(_OUTSIDE)
    return receipts, node_recovery, next(iter(receipt_episode_ids))


def _inspection_scope(
    receipt: GraphWriteReceipt | tuple[GraphWriteReceipt, ...], context: OperationAuthorization,
    actions: tuple[CanonicalNodeRecoveryAction, ...],
) -> _InspectScope:
    """Validate the receipt(s) against the partition, lease and recovery actions and collect exact ids.

    Raises ``GraphOperationError("graph_receipt_outside_authorized_partition")`` for
    any receipt outside the authorized closure, over-bound or malformed ids, node
    actions no receipt authorizes, or a single receipt that is neither this
    operation's nor the context's episode receipt.
    """
    receipts, node_recovery, episode_id = _receipts_in_partition(receipt, context, actions)
    entity_ids = tuple(sorted({value for item in receipts for value in item.entity_ids} | {
        binding.graph_entity_uuid for item in receipts for binding in item.canonical_bindings
    }))
    recoverable_bulk_ids = {
        entity_id for item in receipts if item.phase == "bulk_write_intent"
        for entity_id, _, state in item.prior_entity_state_fingerprints
        if any(candidate_id == entity_id and candidate_state is not None
               for candidate_id, candidate_state in item.intended_entity_state_fingerprints)
    }
    canonical_receipt_ids = {
        binding.graph_entity_uuid for item in receipts for binding in item.canonical_bindings
    }
    mention_ids = tuple(sorted({value for item in receipts for value in item.mention_ids}))
    fact_ids = tuple(sorted({value for item in receipts for value in item.fact_ids}))
    ids = (*entity_ids, *mention_ids, *fact_ids)
    if (len(ids) > MAX_SUPPORT * 3 or any(not _UUID.fullmatch(value) for value in ids)
            or (node_recovery and not {item.graph_entity_uuid for item in actions}
                <= (canonical_receipt_ids | recoverable_bulk_ids))):
        raise GraphOperationError(_OUTSIDE)
    if not node_recovery and not (
        (receipts[0].operation_id == context.operation_id
         and receipts[0].lease_token == context.lease_token)
        or context.episode_receipt == receipts[0]
    ):
        raise GraphOperationError(_OUTSIDE)
    return _InspectScope(receipts, node_recovery, episode_id, entity_ids, mention_ids, fact_ids)


def _parse_incident_links(link_records: Any) -> list[ExactGraphLink]:
    """Validate the bounded incident-link rows and convert them to exact link values."""
    if not isinstance(link_records, list) or len(link_records) > MAX_SUPPORT:
        raise GraphOperationError("graph_receipt_link_inventory_invalid")
    incident_links: list[ExactGraphLink] = []
    for row in link_records:
        support_ids = row.get("episode_ids") or () if isinstance(row, dict) else ()
        if (not isinstance(row, dict) or not isinstance(support_ids, list | tuple)
                or not isinstance(row.get("relationship_type"), str)):
            raise GraphOperationError("graph_receipt_link_inventory_invalid")
        incident_links.append(ExactGraphLink(
            edge_id=str(row.get("edge_id")),
            relationship_type=row["relationship_type"],
            source_node_id=str(row.get("source_node_id")),
            target_node_id=str(row.get("target_node_id")),
            episode_ids=tuple(sorted(str(value) for value in support_ids)),
        ))
    return incident_links


async def _read_inspection(
    driver: Any, context: OperationAuthorization, scope: _InspectScope,
) -> _InspectRead:
    """Read the exact episode, nodes (with vectors), mentions, facts (with vectors) and node links."""
    from graphiti_core.edges import EntityEdge, EpisodicEdge
    from graphiti_core.nodes import EntityNode, EpisodicNode

    entity_ids, mention_ids, fact_ids = scope.entity_ids, scope.mention_ids, scope.fact_ids
    episodes = await EpisodicNode.get_by_uuids(driver, [scope.episode_id])
    entities = await EntityNode.get_by_uuids(driver, list(entity_ids), context.group_id) if entity_ids else []
    for entity in entities:
        await entity.load_name_embedding(driver)
    mentions = await EpisodicEdge.get_by_uuids(driver, list(mention_ids)) if mention_ids else []
    facts = await EntityEdge.get_by_uuids(driver, list(fact_ids)) if fact_ids else []
    for fact in facts:
        await fact.load_fact_embedding(driver)
    link_records, _, _ = await driver.execute_query(
        "MATCH (source)-[edge]-(entity:Entity {group_id: $group_id}) WHERE entity.uuid IN $entity_ids "
        "RETURN DISTINCT edge.uuid AS edge_id, type(edge) AS relationship_type, "
        "startNode(edge).uuid AS source_node_id, endNode(edge).uuid AS target_node_id, "
        "edge.episodes AS episode_ids ORDER BY edge.uuid LIMIT $limit",
        entity_ids=list(entity_ids), group_id=context.group_id, limit=MAX_SUPPORT + 1,
    ) if entity_ids else ([], [], None)
    return _InspectRead(episodes, entities, mentions, facts, _parse_incident_links(link_records))


def _check_inspection_partition(
    context: OperationAuthorization, scope: _InspectScope, read: _InspectRead,
) -> None:
    """Require every read object to be unique, journaled, in-group and supported only inside the partition."""
    episodes, entities, mentions, facts = read.episodes, read.entities, read.mentions, read.facts
    entity_ids, mention_ids, fact_ids = scope.entity_ids, scope.mention_ids, scope.fact_ids
    if ((episodes and episodes[0].group_id != context.group_id)
            or len(episodes) > 1
            or len({str(item.uuid) for item in entities}) != len(entities)
            or len({str(item.uuid) for item in mentions}) != len(mentions)
            or len({str(item.uuid) for item in facts}) != len(facts)
            or {str(item.uuid) for item in entities} - set(entity_ids)
            or {str(item.uuid) for item in mentions} - set(mention_ids)
            or {str(item.uuid) for item in facts} - set(fact_ids)
            or any(str(item.source_node_uuid) != scope.episode_id
                   or str(item.target_node_uuid) not in set(entity_ids) for item in mentions)
            or any(not {str(value) for value in item.episodes}
                   <= set(context.partition_episode_ids) for item in facts)
            or any(not set(item.episode_ids) <= set(context.partition_episode_ids)
                   for item in read.incident_links)
            or len({item.edge_id for item in read.incident_links}) != len(read.incident_links)
            or any(item.group_id != context.group_id for item in (*entities, *mentions, *facts))):
        raise GraphOperationError("graph_receipt_partition_mismatch")


def _build_inspection(context: OperationAuthorization, read: _InspectRead) -> GraphReceiptInspection:
    """Fingerprint the verified reads into the exact-ID inspection result (no graph text)."""
    episodes, entities, mentions, facts = read.episodes, read.entities, read.mentions, read.facts
    dims = context.embedding.dimensions
    mention_links = tuple(sorted((ExactGraphLink(
        edge_id=str(item.uuid), relationship_type="MENTIONS",
        source_node_id=str(item.source_node_uuid),
        target_node_id=str(item.target_node_uuid), episode_ids=(),
    ) for item in mentions), key=lambda item: item.edge_id))
    return GraphReceiptInspection(
        episode_present=bool(episodes),
        episode_state_fingerprint=_episode_state_fingerprint(episodes[0]) if episodes else None,
        entity_ids_present=tuple(sorted(str(item.uuid) for item in entities)),
        current_entity_state_fingerprints=tuple(sorted(
            (str(item.uuid), _entity_state_fingerprint(item, dims)) for item in entities
        )),
        incident_links=tuple(read.incident_links),
        mention_ids_present=tuple(sorted(str(item.uuid) for item in mentions)),
        current_mention_links=mention_links,
        fact_ids_present=tuple(sorted(str(item.uuid) for item in facts)),
        current_fact_states=tuple(sorted(
            (_fact_state(item, dims) for item in facts), key=lambda item: item.fact_id,
        )),
        current_fact_recovery_fingerprints=tuple(sorted(
            (str(item.uuid), _fact_recovery_fingerprint(item, dims)) for item in facts
        )),
    )
