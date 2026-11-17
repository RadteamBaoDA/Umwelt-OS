"""Step helpers for ``TemporalGraph._record_bulk_write_intent``.

The adapter method keeps the journal write and the partition rechecks; these
helpers hold the identifier validation, the exact prior-state reads and the
receipt construction, in the original order. Reads use the partition-scoped
driver the caller passes in; nothing here writes to the graph or the journal.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from modules.knowledge.temporal._fingerprints import (
    _entity_state_fingerprint,
    _episode_state_fingerprint,
    _fact_state,
    _receipt_bindings,
    _require_group,
)
from modules.knowledge.temporal._types import (
    _UUID,
    MAX_SUPPORT,
    EpisodeRequest,
    ExactFactState,
    ExactFactSupport,
    GraphOperationError,
    GraphWriteReceipt,
    OperationAuthorization,
)


def _bulk_ids(
    request: EpisodeRequest,
    episodes: Sequence[Any],
    mentions: Sequence[Any],
    nodes: Sequence[Any],
    facts: Sequence[Any],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Return sorted (entity, mention, fact) ids after proving they are exact, unique and bounded.

    Raises ``GraphOperationError`` for ids outside the request's group/episode, malformed
    UUIDs, over-bound sets or duplicate objects.
    """
    episode_ids = tuple(str(item.uuid) for item in episodes)
    entity_ids = tuple(sorted({str(item.uuid) for item in nodes}))
    mention_ids = tuple(sorted({str(item.uuid) for item in mentions}))
    fact_ids = tuple(sorted({str(item.uuid) for item in facts}))
    if (
        episode_ids != (str(request.episode_id),)
        or any(item.group_id != request.group_id for item in (*episodes, *mentions, *nodes, *facts))
        or any(not _UUID.fullmatch(value) for value in (*entity_ids, *mention_ids, *fact_ids))
        or len(entity_ids) > MAX_SUPPORT
        or len(mention_ids) > MAX_SUPPORT
        or len(fact_ids) > MAX_SUPPORT
        or len(episodes) > 1
        or len({*entity_ids, *mention_ids, *fact_ids, *episode_ids}) > MAX_SUPPORT
    ):
        raise GraphOperationError("graph_write_intent_identifiers_invalid")
    if (
        len(fact_ids) != len(facts)
        or len(entity_ids) != len(nodes)
        or len(mention_ids) != len(mentions)
    ):
        raise GraphOperationError("graph_write_intent_identifiers_ambiguous")
    return entity_ids, mention_ids, fact_ids


async def _prior_entity_states(
    driver: Any,
    request: EpisodeRequest,
    context: OperationAuthorization,
    entity_ids: tuple[str, ...],
) -> tuple[tuple[str, bool, str | None], ...]:
    """Read the current state of every exact node id: (id, existed, fingerprint) sorted by id."""
    from graphiti_core.nodes import EntityNode

    prior_entities = await EntityNode.get_by_uuids(driver, list(entity_ids)) if entity_ids else []
    prior_entity_by_id = {str(item.uuid): item for item in prior_entities}
    if (
        len(prior_entity_by_id) != len(prior_entities)
        or not set(prior_entity_by_id) <= set(entity_ids)
        or any(item.group_id != request.group_id for item in prior_entities)
    ):
        raise GraphOperationError("graph_write_intent_prior_state_invalid")
    for entity in prior_entities:
        await entity.load_name_embedding(driver)
    return tuple(
        sorted(
            (
                entity_id,
                entity_id in prior_entity_by_id,
                (
                    _entity_state_fingerprint(
                        prior_entity_by_id[entity_id], context.embedding.dimensions
                    )
                    if entity_id in prior_entity_by_id
                    else None
                ),
            )
            for entity_id in entity_ids
        )
    )


def _intended_entity_states(
    context: OperationAuthorization,
    entity_ids: tuple[str, ...],
    final_node_payloads: Sequence[dict[str, Any]],
) -> tuple[tuple[str, str | None], ...]:
    """Fingerprint the final hydrated node payloads the writer will persist, one per exact id."""
    payload_by_id = {str(item.get("uuid")): item for item in final_node_payloads}
    if len(payload_by_id) != len(final_node_payloads) or set(payload_by_id) != set(entity_ids):
        raise GraphOperationError("graph_write_intent_identifiers_ambiguous")
    return tuple(
        sorted(
            (
                entity_id,
                _entity_state_fingerprint(
                    payload_by_id[entity_id],
                    context.embedding.dimensions,
                    writer_intended=True,
                ),
            )
            for entity_id in entity_ids
        )
    )


async def _fact_states_and_support(
    driver: Any,
    request: EpisodeRequest,
    context: OperationAuthorization,
    facts: Sequence[Any],
    fact_ids: tuple[str, ...],
) -> tuple[list[ExactFactState], list[ExactFactSupport]]:
    """Read each fact's prior state and derive the intended support the bulk write would persist.

    Raises ``GraphOperationError`` when a prior edge is outside the group, support is
    duplicated/empty/outside the partition, or the write neither adds this
    episode's support nor invalidates an existing fact.
    """
    from graphiti_core.edges import EntityEdge

    prior_edges = await EntityEdge.get_by_uuids(driver, list(fact_ids))
    _require_group(prior_edges, request.group_id)
    prior_by_id = {str(item.uuid): item for item in prior_edges}
    if set(prior_by_id) - set(fact_ids):
        raise GraphOperationError("graph_write_intent_prior_state_invalid")
    states: list[ExactFactState] = []
    support: list[ExactFactSupport] = []
    for edge in facts:
        fact_id = str(edge.uuid)
        previous = prior_by_id.get(fact_id)
        if previous is not None:
            await previous.load_fact_embedding(driver)
            states.append(_fact_state(previous, context.embedding.dimensions))
        else:
            states.append(
                ExactFactState(
                    fact_id=fact_id,
                    source_node_id=str(edge.source_node_uuid),
                    target_node_id=str(edge.target_node_uuid),
                    episode_ids=(),
                    state_fingerprint=hashlib.sha256(b"null").hexdigest(),
                    existed=False,
                )
            )
        desired = tuple(sorted(str(value) for value in edge.episodes))
        if len(set(desired)) != len(desired):
            raise GraphOperationError("graph_write_intent_support_invalid")
        adds_episode_support = str(request.episode_id) in desired
        invalidates_existing_fact = previous is not None and (
            previous.valid_at != edge.valid_at
            or previous.invalid_at != edge.invalid_at
            or previous.expired_at != edge.expired_at
        )
        if (
            not desired
            or not (adds_episode_support or invalidates_existing_fact)
            or not set(desired) <= set(context.partition_episode_ids)
        ):
            raise GraphOperationError("graph_write_intent_support_invalid")
        support.append(
            ExactFactSupport(
                fact_id=fact_id,
                episode_ids=desired,
                source_node_id=str(edge.source_node_uuid),
                target_node_id=str(edge.target_node_uuid),
                valid_at=edge.valid_at,
                invalid_at=edge.invalid_at,
                expired_at=edge.expired_at,
                state_fingerprint=_fact_state(edge, context.embedding.dimensions).state_fingerprint,
            )
        )
    return states, support


def _bulk_receipt(
    request: EpisodeRequest,
    context: OperationAuthorization,
    shell_receipt: GraphWriteReceipt,
    episodes: Sequence[Any],
    ids: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]],
    prior_entity_states: tuple[tuple[str, bool, str | None], ...],
    intended_entity_states: tuple[tuple[str, str | None], ...],
    states: list[ExactFactState],
    support: list[ExactFactSupport],
    hydrated: bool,
) -> GraphWriteReceipt:
    """Assemble the bulk receipt: ``bulk_write_intent`` when node payloads are final, else ``bulk_ids_intent``."""
    entity_ids, mention_ids, fact_ids = ids
    desired_support_digest = hashlib.sha256(
        json.dumps(
            [
                (
                    item.fact_id,
                    item.episode_ids,
                    item.source_node_id,
                    item.target_node_id,
                    str(item.valid_at),
                    str(item.invalid_at),
                    str(item.expired_at),
                    item.state_fingerprint,
                )
                for item in support
            ],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return GraphWriteReceipt(
        operation_id=context.operation_id,
        lease_token=context.lease_token,
        episode_id=request.episode_id,
        group_id=request.group_id,
        mapping_revision=request.mapping_revision,
        desired_support_digest=desired_support_digest,
        phase="bulk_write_intent" if hydrated else "bulk_ids_intent",
        episode_state_fingerprint=_episode_state_fingerprint(episodes[0]),
        prior_episode_state_fingerprint=shell_receipt.episode_state_fingerprint,
        entity_ids=entity_ids,
        intended_entity_state_fingerprints=intended_entity_states,
        prior_entity_state_fingerprints=prior_entity_states,
        mention_ids=mention_ids,
        fact_ids=fact_ids,
        existing_fact_states=tuple(states),
        intended_fact_support=tuple(support),
        canonical_bindings=_receipt_bindings(request.canonical_bindings),
    )
