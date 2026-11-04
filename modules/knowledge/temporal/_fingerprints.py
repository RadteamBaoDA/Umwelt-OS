"""Exact-state fingerprints, normalization and receipt-witness helpers for the temporal graph adapter."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import struct
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from modules.knowledge.temporal._types import (
    _ABSENT_FACT_FINGERPRINT,
    MAX_SUPPORT,
    CanonicalEntityBinding,
    CanonicalNodeRecoveryAction,
    ExactFactState,
    ExactFactSupport,
    GraphOperationError,
    GraphOperationUnknown,
    GraphWriteReceipt,
    OperationAuthorization,
)

logger = logging.getLogger(__name__)


def _safe_error(exc: BaseException) -> GraphOperationError:
    """Map dependency failures to stable redacted graph error categories."""
    if "EquivalentSchemaRuleAlreadyExists" in str(exc) or "already indexed" in str(exc).lower():
        return GraphOperationError("graph_index_exists")
    return GraphOperationUnknown("graph_transport_outcome_unknown")


def _fact_state(edge: Any, dimensions: int | None) -> ExactFactState:
    """Fingerprint complete exact edge state, including its persisted float32 vector, without retaining text."""
    episode_ids = tuple(sorted(str(value) for value in edge.episodes))
    fingerprint = _fact_recovery_fingerprint(edge, dimensions)
    return ExactFactState(
        fact_id=str(edge.uuid), source_node_id=str(edge.source_node_uuid),
        target_node_id=str(edge.target_node_uuid), episode_ids=episode_ids,
        state_fingerprint=fingerprint, existed=True,
        valid_at=edge.valid_at, invalid_at=edge.invalid_at, expired_at=edge.expired_at,
    )


def _episode_state_fingerprint(episode: Any) -> str:
    """Hash exact episode shell fields and attached fact IDs without retaining source content."""
    source = getattr(episode.source, "value", episode.source)
    payload = {
        "uuid": str(episode.uuid),
        "group_id": str(episode.group_id),
        "name": episode.name,
        "source": str(source),
        "source_description": episode.source_description,
        "content": episode.content,
        "valid_at": str(episode.valid_at),
        "created_at": str(episode.created_at),
        "entity_edges": sorted(str(value) for value in episode.entity_edges),
    }
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()


def _float32_vector(vector: Any, dimensions: int | None, error: str) -> list[float]:
    """Return the vector as finite persisted-float32 values of exactly ``dimensions`` entries.

    Raises ``GraphOperationError(error)`` for unconfigured dimensions, a non-list vector, a
    length mismatch, a bool/non-number/non-finite entry, or a value that overflows float32.
    """
    if (dimensions is None or not isinstance(vector, list | tuple)
            or len(vector) != dimensions):
        raise GraphOperationError(error)
    normalized_vector: list[float] = []
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
            raise GraphOperationError(error)
        try:
            normalized = struct.unpack("!f", struct.pack("!f", float(value)))[0]
        except (OverflowError, struct.error):
            raise GraphOperationError(error) from None
        if not math.isfinite(normalized):
            raise GraphOperationError(error)
        normalized_vector.append(normalized)
    return normalized_vector


def _entity_fields(entity: Any) -> tuple[dict[str, Any], Any, Any, Any, Any]:
    """Split a persisted node (dict row or object) into (identity, attributes, labels, created_at, embedding)."""
    if isinstance(entity, dict):
        properties = entity
        reserved = {"uuid", "group_id", "name", "summary", "created_at", "name_embedding", "labels"}
        attributes = {key: value for key, value in properties.items() if key not in reserved}
        identity = {key: properties.get(key) for key in ("uuid", "group_id", "name", "summary")}
        return (identity, attributes, properties.get("labels", ()), properties.get("created_at"),
                properties.get("name_embedding"))
    identity = {
        "uuid": str(entity.uuid), "group_id": str(entity.group_id),
        "name": entity.name, "summary": entity.summary,
    }
    return (identity, getattr(entity, "attributes", {}) or {}, getattr(entity, "labels", ()) or (),
            getattr(entity, "created_at", None), getattr(entity, "name_embedding", None))


def _entity_label_set(labels: Any, attributes: Any, writer_intended: bool) -> set[str]:
    """Validate attribute/label shapes and return the label set (writer intents always gain ``Entity``)."""
    if not isinstance(attributes, dict) or not isinstance(labels, list | tuple | set):
        raise GraphOperationError("graph_entity_state_unsupported")
    if any(not isinstance(label, str) or not label for label in labels):
        raise GraphOperationError("graph_entity_state_unsupported")
    label_set = set(labels)
    if writer_intended:
        label_set.add("Entity")
    elif "Entity" not in label_set:
        raise GraphOperationError("graph_entity_state_unsupported")
    return label_set


def _entity_created_at(created_at: Any) -> str | None:
    """Normalize a node creation time (aware datetime or ISO string) to a UTC ISO string; reject naive input."""
    if created_at is None:
        return None
    if isinstance(created_at, datetime):
        if created_at.tzinfo is None:
            raise GraphOperationError("graph_entity_state_unsupported")
        return created_at.astimezone(UTC).isoformat()
    if isinstance(created_at, str):
        try:
            parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))  # noqa: FURB162  # keeps exact parsing of 'Z' suffix; fromisoformat(Z) is not strictly equivalent
        except ValueError:
            raise GraphOperationError("graph_entity_state_unsupported") from None
        if parsed.tzinfo is None:
            raise GraphOperationError("graph_entity_state_unsupported")
        return parsed.astimezone(UTC).isoformat()
    raise GraphOperationError("graph_entity_state_unsupported")


def _entity_state_fingerprint(
    entity: Any, dimensions: int | None = None, *, writer_intended: bool = False,
) -> str:
    """Hash exact node state; only writer intents synthesize Entity, while malformed persisted labels fail closed."""
    identity, attributes, labels, created_at, embedding = _entity_fields(entity)
    label_set = _entity_label_set(labels, attributes, writer_intended)
    created_at = _entity_created_at(created_at)
    normalized_embedding: list[float] | None = (
        _float32_vector(embedding, dimensions, "graph_entity_embedding_state_unsupported")
        if embedding is not None else None
    )
    payload = {
        **identity,
        "created_at": created_at,
        # EntityNode.save always applies the mandatory Entity label.
        "labels": sorted(label_set),
        "attributes": _normalize_graph_values(attributes),
        "name_embedding": normalized_embedding,
    }
    return hashlib.sha256(_canonical_json(_normalize_graph_values(payload))).hexdigest()


def _fact_states(receipt: GraphWriteReceipt) -> tuple[ExactFactState | ExactFactSupport, ...]:
    """Return both exact fact-state views of a receipt as one typed sequence."""
    return (*receipt.existing_fact_states, *receipt.intended_fact_support)


def _require_group(items: Sequence[Any], group_id: str) -> None:
    """Reject any graph record outside the authorized partition.

    All partitions share one Neo4j database and Graphiti's get_by_uuid(s) lookups are
    not group-scoped, so every fetched record must be proven to belong to group_id
    before it is trusted, compared or mutated.
    """
    if any(getattr(item, "group_id", None) != group_id for item in items):
        raise GraphOperationError("graph_partition_mismatch")


def _datetimes_to_strings(value: Any) -> Any:
    """Typed wrapper for graphiti's untyped UTC datetime-to-string converter (lazy import keeps graph optional)."""
    from graphiti_core.utils.datetime_utils import convert_datetimes_to_strings
    return convert_datetimes_to_strings(value)  # type: ignore[no-untyped-call]  # graphiti_core ships no type hints


def _reject_naive_datetimes(item: Any) -> None:
    """Reject naive datetime leaves before the pinned serializer would assume a timezone."""
    if isinstance(item, datetime):
        if item.tzinfo is None or item.utcoffset() is None:
            raise GraphOperationError("graph_state_datetime_unsupported")
    elif isinstance(item, dict):
        for nested in item.values():
            _reject_naive_datetimes(nested)
    elif isinstance(item, list | tuple):
        for nested in item:
            _reject_naive_datetimes(nested)


def _validate_json_primitives(item: Any) -> None:
    """Allow only normalized JSON primitives so fingerprints never use lossy object stringification."""
    if item is None or isinstance(item, str | bool | int):
        return
    if isinstance(item, float):
        if not math.isfinite(item):
            raise GraphOperationError("graph_state_value_unsupported")
        return
    if isinstance(item, dict):
        if any(not isinstance(key, str) for key in item):
            raise GraphOperationError("graph_state_value_unsupported")
        for nested in item.values():
            _validate_json_primitives(nested)
        return
    if isinstance(item, list | tuple):
        for nested in item:
            _validate_json_primitives(nested)
        return
    raise GraphOperationError("graph_state_value_unsupported")


def _normalize_graph_values(value: Any) -> Any:
    """Project values through UTC datetime-string and NUL-string normalization, rejecting lossy objects."""
    _reject_naive_datetimes(value)
    normalized = _strip_nul_bytes(_datetimes_to_strings(value))
    _validate_json_primitives(normalized)
    return normalized


def _normalized_fact_attributes(edge: Any) -> dict[str, Any]:
    """Validate pinned transport endpoint aliases and preserve all genuine fact attributes."""
    raw_attributes = edge.attributes or {}
    if not isinstance(raw_attributes, dict):
        raise GraphOperationError("graph_fact_attributes_unsupported")
    attributes = dict(raw_attributes)
    source_id = str(edge.source_node_uuid)
    target_id = str(edge.target_node_uuid)
    aliases = {
        "source_node_uuid": source_id, "source_uuid": source_id,
        "target_node_uuid": target_id, "target_uuid": target_id,
    }
    for key, expected in aliases.items():
        if key in attributes:
            actual = attributes[key]
            if not isinstance(actual, str | UUID) or str(actual) != expected:
                raise GraphOperationError("graph_fact_endpoint_alias_mismatch")
            attributes.pop(key)
    normalized_attributes: dict[str, Any] = _normalize_graph_values(attributes)
    return normalized_attributes


def _canonical_json(payload: Any) -> bytes:
    """Serialize an already normalized graph projection without stringifying unsupported values."""
    try:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError):
        raise GraphOperationError("graph_state_value_unsupported") from None


def _normalized_graph_timestamp(value: datetime | str | None) -> str | None:
    """Normalize a persisted Graphiti timestamp to one UTC ISO representation, rejecting naive input."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))  # noqa: FURB162  # keeps exact parsing of 'Z' suffix; fromisoformat(Z) is not strictly equivalent
        except ValueError:
            raise GraphOperationError("graph_state_datetime_unsupported") from None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise GraphOperationError("graph_state_datetime_unsupported")
    normalized_instant: str | None = _normalize_graph_values(value)
    return normalized_instant


def _fact_recovery_fingerprint(edge: Any, dimensions: int | None) -> str:
    """Hash full normalized persisted fact state, validating transport aliases and retaining domain attributes/vector."""
    normalized_vector = _float32_vector(
        getattr(edge, "fact_embedding", None), dimensions, "graph_fact_embedding_state_unsupported")
    payload = {
        "uuid": str(edge.uuid), "group_id": str(edge.group_id),
        "source_node_uuid": str(edge.source_node_uuid),
        "target_node_uuid": str(edge.target_node_uuid), "name": edge.name,
        "fact": edge.fact, "episodes": sorted(str(value) for value in edge.episodes),
        "created_at": _normalized_graph_timestamp(edge.created_at),
        "reference_time": _normalized_graph_timestamp(edge.reference_time),
        "valid_at": _normalized_graph_timestamp(edge.valid_at),
        "invalid_at": _normalized_graph_timestamp(edge.invalid_at),
        "expired_at": _normalized_graph_timestamp(edge.expired_at),
        "attributes": _normalized_fact_attributes(edge),
        "fact_embedding": normalized_vector,
    }
    return hashlib.sha256(_canonical_json(_normalize_graph_values(payload))).hexdigest()


def _receipt_bindings(
    bindings: Sequence[CanonicalEntityBinding],
) -> tuple[CanonicalEntityBinding, ...]:
    """Keep bounded field hashes and support identities while dropping source seed text."""
    return tuple(replace(
        item, node_name=None, node_summary=None,
    ) for item in bindings)


async def _authorize_cross_rebuild_absence(
    context: OperationAuthorization, kind: Literal["node", "fact"], effect_id: str,
    *, episode_id: UUID, endpoints: tuple[str, str] | None = None,
) -> bool:
    """Authorize an already-read exact absence through another operation's rebuild ledger.

    Owner must prove deletion-operation dependency, original cleanup witness,
    completed exact mutation/dispatch and current recovery lease. No callback
    waives a present graph object, current readback or ordinary receipt identity.
    """
    if context.authorize_rebuild_absence is None:
        return False
    await context.authorize_rebuild_absence(kind, effect_id)
    # Journal this operation's actual readback, never relabel the source deletion
    # receipt. Owner preserves the dependency/proof chain alongside this marker.
    receipt_kwargs: dict[str, Any] = {
        "operation_id": context.operation_id, "lease_token": context.lease_token,
        "episode_id": episode_id, "group_id": context.group_id,
        "mapping_revision": context.mapping_revision, "phase": "cleanup_write_intent",
        "desired_support_digest": hashlib.sha256(f"rebuild-absence:{kind}:{effect_id}".encode()).hexdigest(),
        "rebuild_absence_observed": kind,
    }
    if kind == "node":
        receipt_kwargs.update(
            entity_ids=(effect_id,), prior_entity_state_fingerprints=((effect_id, False, None),),
            intended_entity_state_fingerprints=((effect_id, None),),
        )
    else:
        if endpoints is None:
            raise GraphOperationError("graph_rebuild_absence_endpoints")
        receipt_kwargs.update(
            fact_ids=(effect_id,),
            existing_fact_states=(ExactFactState(
                effect_id, endpoints[0], endpoints[1], (), _ABSENT_FACT_FINGERPRINT, False,
            ),),
            intended_fact_support=(ExactFactSupport(
                fact_id=effect_id, episode_ids=(), source_node_id=endpoints[0], target_node_id=endpoints[1],
                state_fingerprint=_ABSENT_FACT_FINGERPRINT,
            ),),
        )
    await context.record_write_intent(GraphWriteReceipt(**receipt_kwargs))
    return True


async def _incident_ids_absent(driver: Any, edge_ids: Sequence[str], group_id: str) -> bool:
    """Read exact formerly incident edge IDs after typed rebuild deletion.

    No broad graph traversal or text is loaded. Every observed UUID must be
    absent before node cleanup converges, including edges supported elsewhere.
    The single database is shared by all partitions, so the read is bound to group_id.
    """
    if len(set(edge_ids)) > MAX_SUPPORT:
        raise GraphOperationError("graph_rebuild_incident_bound")
    if not edge_ids:
        return True
    rows, _, _ = await driver.execute_query(
        "MATCH ()-[edge {group_id: $group_id}]->() WHERE edge.uuid IN $edge_ids "
        "RETURN edge.uuid AS edge_id LIMIT $limit",
        edge_ids=list(edge_ids), group_id=group_id, limit=MAX_SUPPORT + 1,
    )
    return isinstance(rows, list) and not rows


async def _delete_rebuild_node(driver: Any, action: CanonicalNodeRecoveryAction, group_id: str) -> bool:
    """Delete an exact node and its complete owner-authorized incident inventory atomically.

    Prior fingerprint and scheduling are checked by the caller. The single graph
    write rechecks every edge UUID/type/endpoints/support plus inventory equality;
    a new or changed link makes it a no-op. No DETACH or unknown edge is deleted.
    Exact edge absence is read back independently before the caller checks node.
    """
    expected = [{
        "edge_id": link.edge_id, "relationship_type": link.relationship_type,
        "source_node_id": link.source_node_id, "target_node_id": link.target_node_id,
        "episode_ids": list(link.episode_ids),
    } for link in action.expected_incident_links]
    rows, _, _ = await driver.execute_query(
        "MATCH (entity:Entity {uuid: $entity_id, group_id: $group_id}) "
        "OPTIONAL MATCH (entity)-[edge]-() "
        "WITH entity, collect(edge) AS edges "
        "WHERE size(edges) = size($expected) AND all(edge IN edges WHERE "
        "any(link IN $expected WHERE edge.uuid = link.edge_id "
        "AND type(edge) = link.relationship_type "
        "AND startNode(edge).uuid = link.source_node_id "
        "AND endNode(edge).uuid = link.target_node_id "
        "AND size(coalesce(edge.episodes, [])) = size(link.episode_ids) "
        "AND all(episode IN coalesce(edge.episodes, []) WHERE episode IN link.episode_ids))) "
        "WITH entity, edges, entity.uuid AS entity_id "
        "FOREACH (edge IN edges | DELETE edge) DELETE entity RETURN entity_id",
        entity_id=action.graph_entity_uuid, group_id=group_id, expected=expected,
    )
    if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
            or rows[0].get("entity_id") != action.graph_entity_uuid):
        return False
    return await _incident_ids_absent(
        driver, [link.edge_id for link in action.expected_incident_links], group_id,
    )


def _strip_nul_bytes(value: Any) -> Any:
    """Recursively remove NUL characters from strings, which graph storage rejects or truncates."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {key: _strip_nul_bytes(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_strip_nul_bytes(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_strip_nul_bytes(item) for item in value)
    return value


def _to_native(value: Any) -> Any:
    """Convert neo4j.time values (recursively) to stdlib date/time objects Graphiti's parsers expect."""
    from neo4j import time as neo_time

    if isinstance(value, neo_time.DateTime | neo_time.Date | neo_time.Time):
        return value.to_native()
    if isinstance(value, dict):
        return {key: _to_native(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_to_native(item) for item in value]
    return value


