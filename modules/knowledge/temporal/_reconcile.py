"""Step helpers for ``TemporalGraph.reconcile_canonical_nodes`` and ``reconcile_fact_recovery``.

The adapter methods keep the fence, the ``OPERATION_SECONDS`` timeout, the owner
authorization calls and the outer exception mapping. These helpers hold the
validation of the request and the per-action classification/mutation, in the same
order as the original single procedures: classify from exact reads, journal the
cleanup intent, reauthorize, recheck, mutate, then read back.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from modules.knowledge.temporal._fingerprints import (
    _authorize_cross_rebuild_absence,
    _delete_rebuild_node,
    _entity_state_fingerprint,
    _fact_recovery_fingerprint,
    _incident_ids_absent,
    _receipt_bindings,
    _require_group,
)
from modules.knowledge.temporal._reads import (
    _endpoints_present,
    _load_action_entity,
    _node_absent,
    _read_exact_links,
    _replacement_edge,
)
from modules.knowledge.temporal._types import (
    _ABSENT_FACT_FINGERPRINT,
    MAX_RECOVERY_WITNESSES,
    MAX_SUPPORT,
    CandidateNodeReplacement,
    CanonicalEntityBinding,
    CanonicalNodeRecoveryAction,
    CanonicalNodeRecoveryOutcome,
    ExactFactRecoveryAction,
    ExactFactRecoveryOutcome,
    ExactFactState,
    ExactFactSupport,
    GraphOperationError,
    GraphWriteReceipt,
    OperationAuthorization,
)

_NODE_LINKS_ERROR = "graph_node_link_inventory_invalid"
_PHASE_ORDER = {
    "canonical_node_write_intent": 0, "shell_write_intent": 1,
    "bulk_ids_intent": 2, "bulk_write_intent": 3, "cleanup_write_intent": 4,
}


# --------------------------------------------------------------------------- nodes


@dataclass
class _NodeReconcileState:
    """Mutable per-call result buckets so timeouts and errors can still report partial progress."""

    retained: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)
    unwritten: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    retained_receipts: list[GraphWriteReceipt] = field(default_factory=list)

    def final_outcome(self) -> CanonicalNodeRecoveryOutcome:
        """Return the converged/unresolved outcome after every action was classified."""
        return CanonicalNodeRecoveryOutcome(
            converged=not self.unresolved,
            retained_ids=tuple(sorted(self.retained)), deleted_ids=tuple(sorted(self.deleted)),
            replaced_ids=tuple(sorted(self.replaced)), unwritten_ids=tuple(sorted(self.unwritten)),
            unresolved_ids=tuple(sorted(set(self.unresolved))),
            reason="converged" if not self.unresolved else "exact_node_state_unresolved",
        )

    def unknown_outcome(self, action_ids: set[str]) -> CanonicalNodeRecoveryOutcome:
        """Return the not-converged outcome used after a timeout or unexpected failure."""
        return CanonicalNodeRecoveryOutcome(
            converged=False, retained_ids=tuple(sorted(self.retained)), deleted_ids=tuple(sorted(self.deleted)),
            replaced_ids=tuple(sorted(self.replaced)), unwritten_ids=tuple(sorted(self.unwritten)),
            unresolved_ids=tuple(sorted(set(self.unresolved) | action_ids)),
            reason="graph_node_recovery_outcome_unknown",
        )


@dataclass(frozen=True)
class _NodeWritePlan:
    """Resolved replacement inputs for one mutating node action."""

    binding: CanonicalEntityBinding | None
    candidate: CandidateNodeReplacement | None
    replacement: Any
    intended_fingerprint: str | None


def _node_recovery_unavailable(
    context: OperationAuthorization, receipts: tuple[GraphWriteReceipt, ...],
    actions: tuple[CanonicalNodeRecoveryAction, ...],
) -> bool:
    """Report whether the owner callback is missing or receipt/action counts are out of bounds."""
    return (context.authorize_node_recovery is None
            or not 1 <= len(receipts) <= MAX_RECOVERY_WITNESSES or not actions
            or len(actions) > MAX_SUPPORT
            or len({item.graph_entity_uuid for item in actions}) != len(actions))


def _validate_node_request(
    context: OperationAuthorization, receipts: tuple[GraphWriteReceipt, ...],
    actions: tuple[CanonicalNodeRecoveryAction, ...],
) -> str:
    """Return the single episode id after proving receipts and actions stay inside the authorized partition.

    Raises ``GraphOperationError("graph_node_recovery_outside_authorized_partition")``
    for mixed episodes, foreign operation/group, wrong mapping revision, an
    over-bound physical id union or an action for a node no receipt authorizes.
    """
    episode_ids = {str(item.episode_id) for item in receipts}
    mapping_revisions = {item.mapping_revision for item in receipts}
    authorized_node_ids = {
        binding.graph_entity_uuid for receipt in receipts for binding in receipt.canonical_bindings
    }
    authorized_node_ids.update(
        entity_id for receipt in receipts if receipt.phase == "bulk_write_intent"
        for entity_id, _, state in receipt.prior_entity_state_fingerprints
        if any(candidate_id == entity_id and candidate_state is not None
               for candidate_id, candidate_state in receipt.intended_entity_state_fingerprints)
    )
    total_ids = {
        value for item in receipts
        for value in (*item.entity_ids, *item.mention_ids, *item.fact_ids)
    }
    total_ids.update(link.edge_id for item in receipts for link in item.incident_links)
    action_ids = {item.graph_entity_uuid for item in actions}
    expected_link_count = sum(len(item.expected_incident_links) for item in actions)
    if (len(episode_ids) != 1 or not episode_ids <= set(context.partition_episode_ids)
            or mapping_revisions != {context.mapping_revision}
            or any(item.group_id != context.group_id or item.operation_id != context.operation_id
                   for item in receipts)
            or len(total_ids | action_ids | {
                link.edge_id for action in actions for link in action.expected_incident_links
            }) > MAX_SUPPORT or expected_link_count > MAX_SUPPORT
            or not action_ids <= authorized_node_ids):
        raise GraphOperationError("graph_node_recovery_outside_authorized_partition")
    return next(iter(episode_ids))


async def _classify_absent_node(
    driver: Any, context: OperationAuthorization, receipts: tuple[GraphWriteReceipt, ...],
    action: CanonicalNodeRecoveryAction, episode_id: str, state: _NodeReconcileState,
) -> None:
    """Classify a missing node for a delete action as deleted, unwritten or unresolved.

    A completed own cleanup deletion wins, then a journaled never-existed prior
    state, then an owner-certified cross-operation absence. Rebuild deletion
    additionally requires every formerly incident edge to be absent and otherwise
    downgrades the node to unresolved.
    """
    node_id = action.graph_entity_uuid
    prior_node_state = next((
        (exists, fingerprint)
        for receipt in receipts
        for candidate_id, exists, fingerprint in receipt.prior_entity_state_fingerprints
        if candidate_id == node_id
    ), None)
    cleanup_deleted = any(
        receipt.phase == "cleanup_write_intent"
        and (node_id, None) in receipt.intended_entity_state_fingerprints
        and (receipt.rebuild_absence_observed == "node"
             or any(candidate_id == node_id and exists and fingerprint is not None
                    for candidate_id, exists, fingerprint in receipt.prior_entity_state_fingerprints))
        for receipt in receipts
    )
    if cleanup_deleted:
        state.deleted.append(node_id)
    elif prior_node_state is not None and not prior_node_state[0]:
        state.unwritten.append(node_id)
    elif await _authorize_cross_rebuild_absence(
        context, "node", node_id, episode_id=UUID(str(episode_id)),
    ):
        state.deleted.append(node_id)
    else:
        state.unresolved.append(node_id)
    if action.action == "delete_for_rebuild" and not await _incident_ids_absent(
        driver, [link.edge_id for item in receipts
                 if node_id in item.entity_ids
                 for link in item.incident_links]
        + [link.edge_id for link in action.expected_incident_links],
        context.group_id,
    ):
        state.deleted[:] = [value for value in state.deleted if value != node_id]
        state.unwritten[:] = [value for value in state.unwritten if value != node_id]
        state.unresolved.append(node_id)


def _build_replacement_node(
    context: OperationAuthorization, action: CanonicalNodeRecoveryAction, entity: Any,
    binding: CanonicalEntityBinding | None, candidate: CandidateNodeReplacement | None,
) -> Any:
    """Build the replacement ``EntityNode`` from the binding (preferred) or the candidate seed fields."""
    from graphiti_core.nodes import EntityNode

    if binding is not None:
        node_name, node_summary = binding.node_name, binding.node_summary or ""
    else:
        assert candidate is not None  # guarded by the caller: binding and candidate not both None  # noqa: S101  # mypy narrowing invariant
        node_name, node_summary = candidate.node_name, candidate.node_summary
    return EntityNode(
        uuid=action.graph_entity_uuid, group_id=context.group_id,
        name=node_name, summary=node_summary, attributes={},
        # Label assignment is additive (SET n:$(labels)), so preserve the exact current physical labels.
        labels=(list(entity.labels) if entity is not None else ["Entity"]),
        created_at=(entity.created_at if entity is not None else action.replacement_created_at),
    )


def _plan_node_write(
    context: OperationAuthorization, receipts: tuple[GraphWriteReceipt, ...],
    action: CanonicalNodeRecoveryAction, entity: Any, episode_id: str,
) -> _NodeWritePlan | None:
    """Resolve binding/candidate and the intended replacement; None means the action stays unresolved."""
    binding = action.replacement_binding or next((
        item for receipt in receipts for item in receipt.canonical_bindings
        if item.graph_entity_uuid == action.graph_entity_uuid
    ), None)
    candidate = action.replacement_candidate
    if (candidate is not None and (
        candidate.group_id != context.group_id
        or episode_id in candidate.support_episode_ids
        or not set(candidate.support_episode_ids) <= set(context.partition_episode_ids)
    )):
        return None
    if action.action != "replace_from_current_support":
        return _NodeWritePlan(binding, candidate, None, None)
    if binding is None and candidate is None:
        return None
    if entity is None and action.replacement_created_at is None:
        return None
    replacement = _build_replacement_node(context, action, entity, binding, candidate)
    intended_fingerprint = _entity_state_fingerprint(
        replacement, context.embedding.dimensions, writer_intended=True,
    )
    return _NodeWritePlan(binding, candidate, replacement, intended_fingerprint)


def _node_cleanup_receipt(
    context: OperationAuthorization, episode_id: str, action: CanonicalNodeRecoveryAction,
    entity: Any, current_fingerprint: str | None, plan: _NodeWritePlan,
) -> GraphWriteReceipt:
    """Build the pre-mutation cleanup receipt for one node action."""
    binding, candidate = plan.binding, plan.candidate
    return GraphWriteReceipt(
        operation_id=context.operation_id, lease_token=context.lease_token,
        episode_id=UUID(episode_id), group_id=context.group_id,
        mapping_revision=context.mapping_revision,
        desired_support_digest=hashlib.sha256(json.dumps(
            [action.graph_entity_uuid, action.action, current_fingerprint,
             plan.intended_fingerprint], separators=(",", ":"),
        ).encode()).hexdigest(),
        phase="cleanup_write_intent",
        entity_ids=(action.graph_entity_uuid,),
        intended_entity_state_fingerprints=((action.graph_entity_uuid, plan.intended_fingerprint),),
        prior_entity_state_fingerprints=((
            action.graph_entity_uuid, entity is not None, current_fingerprint,
        ),),
        canonical_bindings=_receipt_bindings((binding,)) if binding is not None else (),
        candidate_field_support=(tuple(
            (action.graph_entity_uuid, field_name, digest, support_ids)
            for field_name, digest, support_ids in candidate.field_support
        ) if candidate is not None else ()),
        incident_links=action.expected_incident_links,
    )


async def _recheck_node_before_write(
    driver: Any, context: OperationAuthorization, action: CanonicalNodeRecoveryAction,
    current_fingerprint: str | None,
) -> bool:
    """Re-read the node and its links after the intent; True only if both still equal the classified state."""
    from graphiti_core.nodes import EntityNode

    latest = None
    try:
        latest = await EntityNode.get_by_uuid(driver, action.graph_entity_uuid)
        await latest.load_name_embedding(driver)
    except Exception as exc:
        if not _node_absent(exc):
            raise
    if latest is not None:
        _require_group([latest], context.group_id)
    return not ((_entity_state_fingerprint(latest, context.embedding.dimensions)
                 if latest is not None else None)
                != current_fingerprint
                or await _read_exact_links(driver, context, action.graph_entity_uuid, _NODE_LINKS_ERROR)
                != action.expected_incident_links)


async def _delete_node_and_confirm(
    driver: Any, context: OperationAuthorization, action: CanonicalNodeRecoveryAction,
    state: _NodeReconcileState,
) -> None:
    """Delete the node (typed rebuild or plain orphan) and record it deleted only if it reads back absent."""
    from graphiti_core.nodes import EntityNode

    node_id = action.graph_entity_uuid
    if action.action == "delete_for_rebuild":
        if not await _delete_rebuild_node(driver, action, context.group_id):
            state.unresolved.append(node_id)
            return
    else:
        await EntityNode.delete_by_uuids(driver, [node_id])
    try:
        await EntityNode.get_by_uuid(driver, node_id)
    except Exception as exc:
        if not _node_absent(exc):
            raise
        state.deleted.append(node_id)
    else:
        state.unresolved.append(node_id)


async def _replace_node_and_confirm(
    driver: Any, context: OperationAuthorization, action: CanonicalNodeRecoveryAction,
    plan: _NodeWritePlan, state: _NodeReconcileState,
) -> None:
    """Save the replacement, drop the stale vector and record success only if the read-back matches."""
    from graphiti_core.nodes import EntityNode

    node_id = action.graph_entity_uuid
    assert plan.replacement is not None  # noqa: S101  # mypy narrowing invariant
    await plan.replacement.save(driver)
    # Seed replacement deliberately drops old vectors derived from deleted support.
    records, _, _ = await driver.execute_query(
        "MATCH (entity:Entity {uuid: $entity_id, group_id: $group_id}) "
        "SET entity.name_embedding = null RETURN entity.uuid AS entity_id",
        entity_id=node_id, group_id=context.group_id,
    )
    if (not isinstance(records, list) or len(records) != 1
            or not isinstance(records[0], dict)
            or records[0].get("entity_id") != node_id):
        state.unresolved.append(node_id)
        return
    saved = await EntityNode.get_by_uuid(driver, node_id)
    await saved.load_name_embedding(driver)
    if (_entity_state_fingerprint(saved, context.embedding.dimensions)
            == plan.intended_fingerprint
            and saved.group_id == context.group_id
            and await _read_exact_links(driver, context, node_id, _NODE_LINKS_ERROR)
            == action.expected_incident_links):
        state.replaced.append(node_id)
    else:
        state.unresolved.append(node_id)


async def _write_node_action(
    driver: Any, context: OperationAuthorization, actions: tuple[CanonicalNodeRecoveryAction, ...],
    action: CanonicalNodeRecoveryAction, receipt: GraphWriteReceipt, current_fingerprint: str | None,
    plan: _NodeWritePlan, state: _NodeReconcileState,
) -> None:
    """Journal the intent, reauthorize with the extended history, recheck, then delete or replace.

    Any failure is the caller's to treat as unresolved; the intent has already
    been journaled by then, exactly like the original ordering.
    """
    await context.record_write_intent(receipt)
    state.retained_receipts.append(receipt)
    assert context.authorize_node_recovery is not None  # noqa: S101  # mypy narrowing invariant
    await context.authorize_node_recovery(tuple(state.retained_receipts), actions)
    if not await _recheck_node_before_write(driver, context, action, current_fingerprint):
        state.unresolved.append(action.graph_entity_uuid)
        return
    if action.action in {"delete_orphan", "delete_for_rebuild"}:
        await _delete_node_and_confirm(driver, context, action, state)
    else:
        await _replace_node_and_confirm(driver, context, action, plan, state)


async def _reconcile_node_action(
    driver: Any, context: OperationAuthorization, receipts: tuple[GraphWriteReceipt, ...],
    actions: tuple[CanonicalNodeRecoveryAction, ...], action: CanonicalNodeRecoveryAction,
    episode_id: str, state: _NodeReconcileState,
) -> None:
    """Classify and, when required, apply one node recovery action, updating ``state`` in place.

    Reads happen first (node, incident links, fingerprint); only a still-matching
    deletable or replaceable node reaches the journaled mutation, whose own
    failures are caught and recorded as unresolved.
    """
    node_id = action.graph_entity_uuid
    entity = await _load_action_entity(driver, context, node_id)
    links = await _read_exact_links(driver, context, node_id, _NODE_LINKS_ERROR)
    if (links != action.expected_incident_links
            and not (entity is None and not links and action.action == "delete_for_rebuild")):
        state.unresolved.append(node_id)
        return
    current_fingerprint = (
        _entity_state_fingerprint(entity, context.embedding.dimensions) if entity is not None else None
    )
    if current_fingerprint != action.expected_current_state_fingerprint:
        state.unresolved.append(node_id)
        return
    if action.action == "retain":
        (state.unwritten if entity is None else state.retained).append(node_id)
        return
    if action.action in {"delete_orphan", "delete_for_rebuild"} and entity is None:
        await _classify_absent_node(driver, context, receipts, action, episode_id, state)
        return
    if action.action == "delete_orphan" and links:
        state.unresolved.append(node_id)
        return
    plan = _plan_node_write(context, receipts, action, entity, episode_id)
    if plan is None:
        state.unresolved.append(node_id)
        return
    receipt = _node_cleanup_receipt(context, episode_id, action, entity, current_fingerprint, plan)
    try:
        await _write_node_action(driver, context, actions, action, receipt, current_fingerprint, plan, state)
    except Exception:  # noqa: BLE001  # fail-closed boundary: any failure denies/degrades
        state.unresolved.append(node_id)


# --------------------------------------------------------------------------- facts


@dataclass
class _FactReconcileState:
    """Mutable per-call result buckets so timeouts and errors can still report partial progress."""

    replaced: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    retained_receipts: list[GraphWriteReceipt] = field(default_factory=list)

    def final_outcome(self) -> ExactFactRecoveryOutcome:
        """Return the converged/unresolved outcome after every action was classified."""
        return ExactFactRecoveryOutcome(
            converged=not self.unresolved, replaced_ids=tuple(sorted(self.replaced)),
            deleted_ids=tuple(sorted(self.deleted)), unresolved_ids=tuple(sorted(self.unresolved)),
            reason="converged" if not self.unresolved else "exact_fact_state_unresolved",
        )

    def unknown_outcome(self, action_ids: set[str]) -> ExactFactRecoveryOutcome:
        """Return the not-converged outcome used after a timeout or unexpected failure."""
        return ExactFactRecoveryOutcome(
            converged=False, replaced_ids=tuple(sorted(self.replaced)),
            deleted_ids=tuple(sorted(self.deleted)),
            unresolved_ids=tuple(sorted(set(self.unresolved) | action_ids)),
            reason="graph_fact_recovery_outcome_unknown",
        )


def _fact_recovery_unavailable(
    context: OperationAuthorization, receipts: tuple[GraphWriteReceipt, ...],
    actions: tuple[ExactFactRecoveryAction, ...],
) -> bool:
    """Report whether the owner callback is missing or receipt/action counts and order are invalid."""
    return (context.authorize_fact_recovery is None
            or not 1 <= len(receipts) <= MAX_RECOVERY_WITNESSES or not actions
            or len(actions) > MAX_SUPPORT
            or len({item.fact_id for item in actions}) != len(actions)
            or tuple(sorted(actions, key=lambda item: item.fact_id)) != actions)


def _fact_proofs(receipts: tuple[GraphWriteReceipt, ...]) -> dict[str, tuple[str, str, bool]]:
    """Map each fact id to its journaled (source, target, existed) and require stable endpoints."""
    proof_by_id: dict[str, tuple[str, str, bool]] = {}
    for receipt in receipts:
        prior_by_id = {item.fact_id: item for item in receipt.existing_fact_states}
        intended_by_id = {item.fact_id: item for item in receipt.intended_fact_support}
        for fact_id in set(prior_by_id) & set(intended_by_id):
            prior = prior_by_id[fact_id]
            intended = intended_by_id[fact_id]
            proof_by_id[fact_id] = (prior.source_node_id, prior.target_node_id, prior.existed)
            if (intended.source_node_id != prior.source_node_id
                    or intended.target_node_id != prior.target_node_id):
                raise GraphOperationError("graph_fact_recovery_receipt_invalid")
    return proof_by_id


def _validate_fact_request(
    context: OperationAuthorization, receipts: tuple[GraphWriteReceipt, ...],
    actions: tuple[ExactFactRecoveryAction, ...],
) -> str:
    """Return the single episode id after proving receipts and actions are journaled and in-partition.

    Raises ``GraphOperationError`` (stable categories) for foreign receipts,
    mis-ordered phases, actions not backed by a journaled endpoint pair, replacement
    snapshots outside the partition/dimensions or an over-bound physical id union.
    """
    episode_ids = {str(item.episode_id) for item in receipts}
    if (len(episode_ids) != 1 or not episode_ids <= set(context.partition_episode_ids)
            or any(item.group_id != context.group_id or item.operation_id != context.operation_id
                   or item.lease_token != context.lease_token
                   or item.mapping_revision != context.mapping_revision
                   or item.phase not in _PHASE_ORDER
                   for item in receipts)):
        raise GraphOperationError("graph_fact_recovery_outside_authorized_partition")
    if [item.phase for item in receipts] != sorted(
        (item.phase for item in receipts), key=lambda phase: _PHASE_ORDER[phase],
    ):
        raise GraphOperationError("graph_fact_recovery_receipt_invalid")
    episode_id = next(iter(episode_ids))
    proof_by_id = _fact_proofs(receipts)
    if any(action.fact_id not in proof_by_id
           or proof_by_id[action.fact_id][:2] != (action.source_node_id, action.target_node_id)
           or (action.replacement is not None and (
               action.replacement.group_id != context.group_id
               or episode_id in action.replacement.episode_ids
               or not set(action.replacement.episode_ids) <= set(context.partition_episode_ids)
               or (context.embedding.dimensions is not None
                   and len(action.replacement.fact_embedding) != context.embedding.dimensions)
           )) for action in actions):
        raise GraphOperationError("graph_fact_recovery_receipt_invalid")
    total_recovery_ids = {
        value for item in receipts
        for value in (*item.entity_ids, *item.mention_ids, *item.fact_ids,
                      *(link.edge_id for link in item.incident_links))
    } | {item.fact_id for item in actions}
    if len(total_recovery_ids) > MAX_SUPPORT:
        raise GraphOperationError("graph_fact_recovery_receipt_invalid")
    return episode_id


async def _classify_absent_fact(
    context: OperationAuthorization, receipts: tuple[GraphWriteReceipt, ...],
    action: ExactFactRecoveryAction, episode_id: str, state: _FactReconcileState,
) -> None:
    """Classify a missing fact: deleted when a deleting action has recorded or certified absence."""
    recorded_delete = any(
        receipt.phase == "cleanup_write_intent"
        and any(item.fact_id == action.fact_id
                and item.state_fingerprint == _ABSENT_FACT_FINGERPRINT
                and not item.episode_ids
                for item in receipt.intended_fact_support)
        and any(item.fact_id == action.fact_id and item.existed
                for item in receipt.existing_fact_states)
        for receipt in receipts
    )
    recorded_initial_absence = any(
        any(item.fact_id == action.fact_id and not item.existed
            and item.source_node_id == action.source_node_id
            and item.target_node_id == action.target_node_id
            for item in receipt.existing_fact_states)
        and any(item.fact_id == action.fact_id
                and item.source_node_id == action.source_node_id
                and item.target_node_id == action.target_node_id
                for item in receipt.intended_fact_support)
        for receipt in receipts
    )
    if action.action in {"delete_unsupported", "delete_for_rebuild"} and (
        recorded_delete or recorded_initial_absence
        or await _authorize_cross_rebuild_absence(
            context, "fact", action.fact_id, episode_id=UUID(str(episode_id)),
            endpoints=(action.source_node_id, action.target_node_id),
        )
    ):
        state.deleted.append(action.fact_id)
    else:
        state.unresolved.append(action.fact_id)


def _fact_cleanup_receipt(
    context: OperationAuthorization, receipts: tuple[GraphWriteReceipt, ...], episode_id: str,
    action: ExactFactRecoveryAction, current: Any, current_fingerprint: str, intended_fingerprint: str,
) -> GraphWriteReceipt:
    """Build the pre-mutation cleanup receipt for one fact action."""
    prior_state = ExactFactState(
        fact_id=action.fact_id, source_node_id=action.source_node_id,
        target_node_id=action.target_node_id,
        episode_ids=tuple(sorted(str(value) for value in current.episodes)),
        state_fingerprint=current_fingerprint, existed=True,
        valid_at=current.valid_at, invalid_at=current.invalid_at,
        expired_at=current.expired_at,
    )
    replacement = action.replacement
    intended_support = ExactFactSupport(
        fact_id=action.fact_id,
        episode_ids=(replacement.episode_ids if replacement else ()),
        source_node_id=action.source_node_id, target_node_id=action.target_node_id,
        valid_at=(replacement.valid_at if replacement else None),
        invalid_at=(replacement.invalid_at if replacement else None),
        expired_at=(replacement.expired_at if replacement else None),
        state_fingerprint=intended_fingerprint,
    )
    return GraphWriteReceipt(
        operation_id=context.operation_id, lease_token=context.lease_token,
        episode_id=UUID(episode_id), group_id=context.group_id,
        mapping_revision=context.mapping_revision,
        desired_support_digest=hashlib.sha256(json.dumps(
            [action.fact_id, action.action, current_fingerprint, intended_fingerprint],
            separators=(",", ":"),
        ).encode()).hexdigest(),
        phase="cleanup_write_intent",
        prior_episode_state_fingerprint=next((
            item.episode_state_fingerprint for item in reversed(receipts)
            if item.episode_state_fingerprint is not None
        ), None),
        fact_ids=(action.fact_id,),
        existing_fact_states=(prior_state,),
        intended_fact_support=(intended_support,),
        canonical_bindings=_receipt_bindings(context.canonical_bindings),
    )


async def _fetch_fact(driver: Any, context: OperationAuthorization, fact_id: str) -> Any:
    """Read one exact fact in the authorized group (None when absent) and load its embedding."""
    from graphiti_core.edges import EntityEdge

    rows = await EntityEdge.get_by_uuids(driver, [fact_id])
    _require_group(rows, context.group_id)
    fresh = next((item for item in rows if str(item.uuid) == fact_id), None)
    if fresh is not None:
        await fresh.load_fact_embedding(driver)
    return fresh


async def _write_fact_action(
    driver: Any, context: OperationAuthorization, actions: tuple[ExactFactRecoveryAction, ...],
    action: ExactFactRecoveryAction, receipt: GraphWriteReceipt, current_fingerprint: str,
    intended_fingerprint: str, replacement_edge: Any, endpoint_ids: list[str], state: _FactReconcileState,
) -> None:
    """Journal intent, reauthorize, recheck fact and endpoints, then replace or delete with read-back.

    Unlike node actions, failures here propagate to the adapter's outer handler,
    which maps them to the unknown outcome.
    """
    from graphiti_core.edges import EntityEdge

    await context.record_write_intent(receipt)
    state.retained_receipts.append(receipt)
    assert context.authorize_fact_recovery is not None  # noqa: S101  # mypy narrowing invariant
    await context.authorize_fact_recovery(tuple(state.retained_receipts), actions)
    fresh = await _fetch_fact(driver, context, action.fact_id)
    if fresh is None:
        state.unresolved.append(action.fact_id)
        return
    # The endpoint read always precedes the fingerprint comparison, as in the original procedure.
    endpoints_ok = await _endpoints_present(driver, context, endpoint_ids)
    if (_fact_recovery_fingerprint(fresh, context.embedding.dimensions) != current_fingerprint
            or not endpoints_ok):
        state.unresolved.append(action.fact_id)
        return
    if replacement_edge is not None:
        await replacement_edge.save(driver)
        saved = await _fetch_fact(driver, context, action.fact_id)
        if (saved is not None
                and _fact_recovery_fingerprint(saved, context.embedding.dimensions) == intended_fingerprint):
            state.replaced.append(action.fact_id)
        else:
            state.unresolved.append(action.fact_id)
    else:
        await EntityEdge.delete_by_uuids(driver, [action.fact_id])
        remaining = await EntityEdge.get_by_uuids(driver, [action.fact_id])
        (state.unresolved if remaining else state.deleted).append(action.fact_id)


async def _current_fact_fingerprint(
    driver: Any, context: OperationAuthorization, action: ExactFactRecoveryAction, state: _FactReconcileState,
) -> tuple[bool, Any, str | None]:
    """Read the live fact; return (usable, edge, fingerprint).

    ``usable`` is False when the read is ambiguous (duplicate rows) or the edge
    does not belong to the action's endpoints/partition support; the action is
    then already recorded unresolved.
    """
    from graphiti_core.edges import EntityEdge

    current_rows = await EntityEdge.get_by_uuids(driver, [action.fact_id])
    _require_group(current_rows, context.group_id)
    current_by_id = {str(item.uuid): item for item in current_rows}
    if len(current_by_id) != len(current_rows):
        state.unresolved.append(action.fact_id)
        return False, None, None
    current = current_by_id.get(action.fact_id)
    if current is None:
        return True, None, None
    await current.load_fact_embedding(driver)
    current_fingerprint = _fact_recovery_fingerprint(current, context.embedding.dimensions)
    if (current.group_id != context.group_id
            or str(current.source_node_uuid) != action.source_node_id
            or str(current.target_node_uuid) != action.target_node_id
            or not {str(value) for value in current.episodes} <= set(context.partition_episode_ids)):
        state.unresolved.append(action.fact_id)
        return False, None, None
    return True, current, current_fingerprint


async def _reconcile_fact_action(
    driver: Any, context: OperationAuthorization, receipts: tuple[GraphWriteReceipt, ...],
    actions: tuple[ExactFactRecoveryAction, ...], action: ExactFactRecoveryAction, episode_id: str,
    state: _FactReconcileState,
) -> None:
    """Classify and, when required, apply one exact fact action, updating ``state`` in place.

    Exact absence is resolved before endpoint presence, so completed node rebuilds
    can still remove facts. A fact already at its intended state needs no write.
    """
    usable, current, current_fingerprint = await _current_fact_fingerprint(driver, context, action, state)
    if not usable:
        return
    if current is None:
        await _classify_absent_fact(context, receipts, action, episode_id, state)
        return
    endpoint_ids = sorted({action.source_node_id, action.target_node_id})
    if not await _endpoints_present(driver, context, endpoint_ids):
        state.unresolved.append(action.fact_id)
        return
    replacement_edge = None
    intended_fingerprint = _ABSENT_FACT_FINGERPRINT
    if action.replacement is not None:
        replacement_edge = _replacement_edge(action.replacement)
        intended_fingerprint = _fact_recovery_fingerprint(replacement_edge, context.embedding.dimensions)
    if current_fingerprint == intended_fingerprint:
        (state.replaced if replacement_edge is not None else state.deleted).append(action.fact_id)
        return
    # Graph episode references can be stale; the owner callback proves current support absence.
    if current_fingerprint != action.expected_current_state_fingerprint:
        state.unresolved.append(action.fact_id)
        return
    assert current_fingerprint is not None  # noqa: S101  # mypy narrowing invariant
    receipt = _fact_cleanup_receipt(
        context, receipts, episode_id, action, current, current_fingerprint, intended_fingerprint,
    )
    await _write_fact_action(
        driver, context, actions, action, receipt, current_fingerprint, intended_fingerprint,
        replacement_edge, endpoint_ids, state,
    )
