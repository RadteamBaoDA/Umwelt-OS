"""Step helpers for ``TemporalGraph.delete_episode``.

The caller (``TemporalGraph._delete_episode_fenced``) already holds the source/document
fence and the ``OPERATION_SECONDS`` timeout. These helpers only split the original
single procedure into named steps in the same order: receipt validation, graph
inventory, pure support-subtraction planning, intent journaling, per-edge
recheck -> write -> read-back, episode removal, node/fact recovery proofs and the
final fact-inventory verification. Nothing here opens a transaction of its own;
every graph read/write goes through the exact-ID driver the caller passes in.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from modules.knowledge.temporal._fingerprints import (
    _authorize_cross_rebuild_absence,
    _entity_state_fingerprint,
    _episode_state_fingerprint,
    _fact_recovery_fingerprint,
    _fact_state,
    _fact_states,
    _incident_ids_absent,
    _receipt_bindings,
    _require_group,
)
from modules.knowledge.temporal._reads import (
    _load_action_entity,
    _node_absent,
    _read_exact_links,
    _replacement_edge,
)
from modules.knowledge.temporal._types import (
    _ABSENT_FACT_FINGERPRINT,
    _UUID,
    MAX_SUPPORT,
    CanonicalNodeRecoveryAction,
    DeleteOutcome,
    ExactFactRecoveryAction,
    ExactFactState,
    ExactFactSupport,
    ExactGraphLink,
    GraphOperationError,
    GraphWriteReceipt,
    OperationAuthorization,
    _authorize_receipt_witnesses,
)

_FACT_PHASES = frozenset({"bulk_write_intent", "cleanup_write_intent"})
_PHASE_ORDER = {
    "canonical_node_write_intent": 0,
    "shell_write_intent": 1,
    "bulk_ids_intent": 2,
    "bulk_write_intent": 3,
    "cleanup_write_intent": 4,
}
Validate = Callable[[], Awaitable[None]]


@dataclass
class _DeleteProgress:
    """Mutable ids gathered so far so the outer ``except`` can still return a partial outcome.

    ``stale_ids`` is only filled by the terminal step, exactly like the original
    procedure, so a failure before that point reports no stale ids.
    """

    affected_ids: tuple[str, ...] = ()
    shared_ids: tuple[str, ...] = ()
    stale_ids: tuple[str, ...] = ()
    recovery_ids: tuple[str, ...] = ()

    def partial_outcome(self, episode_id: UUID) -> DeleteOutcome:
        """Return the not-removed ``unknown`` outcome carrying every id recorded so far."""
        return DeleteOutcome(
            str(episode_id),
            False,
            self.affected_ids,
            self.shared_ids,
            "unknown",
            self.stale_ids,
            self.recovery_ids,
        )


@dataclass(frozen=True)
class _ReceiptView:
    """Validated owner receipt history plus the exact ids it proves."""

    receipts: tuple[GraphWriteReceipt, ...]
    prior_receipt: GraphWriteReceipt
    known_entity_ids: tuple[str, ...]
    known_mention_ids: tuple[str, ...]
    known_fact_ids: tuple[str, ...]
    node_actions: tuple[CanonicalNodeRecoveryAction, ...]
    fact_actions: tuple[ExactFactRecoveryAction, ...]


@dataclass
class _EpisodeInventory:
    """Fetched, group-checked episode, fact edges and observed fact fingerprints."""

    episode: Any
    edges: list[Any]
    candidate_fact_ids: tuple[str, ...]
    observed_fact_fingerprints: dict[str, str] = field(default_factory=dict)
    cross_rebuild_absent_fact_ids: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class DeletePlan:
    """Result of the pure support subtraction: which facts keep, lose or must recompute support."""

    shared_ids: tuple[str, ...]
    remove_ids: tuple[str, ...]
    support: tuple[ExactFactSupport, ...]
    states: tuple[ExactFactState, ...]
    stale: frozenset[str]


@dataclass(frozen=True)
class _EpisodeVerification:
    """Read-back facts about episode removal gathered after the destructive steps."""

    episode_absent: bool
    remaining_mentions: list[Any]
    reverse_support: list[Any]


@dataclass(frozen=True)
class _FinalFacts:
    """Final fact inventory verdict plus the data fact recovery needs."""

    matches: bool
    current_by_id: dict[str, Any]
    latest_support: dict[str, ExactFactSupport]
    authorized_absent: set[str]


def _unknown_outcome(episode_id: UUID) -> DeleteOutcome:
    """Return the id-free ``unknown`` outcome used when receipts cannot be trusted."""
    return DeleteOutcome(str(episode_id), False, (), (), "unknown")


def _receipt_history(context: OperationAuthorization) -> tuple[GraphWriteReceipt, ...]:
    """Return the owner receipt history, falling back to the single episode receipt."""
    return context.receipt_history or (
        (context.episode_receipt,) if context.episode_receipt is not None else ()
    )


def _receipts_belong_to_delete(
    context: OperationAuthorization,
    episode_id: UUID,
    receipts: tuple[GraphWriteReceipt, ...],
) -> GraphWriteReceipt | None:
    """Return the first write-intent receipt when the whole history matches this lease and phase order.

    Returns None (caller reports ``unknown``) when no initial intent exists, any
    receipt belongs to another episode/group/operation/lease/mapping revision, or
    phases are not monotonically ordered.
    """
    prior_receipt = next(
        (
            item
            for item in receipts
            if item.phase
            in {
                "shell_write_intent",
                "bulk_ids_intent",
                "bulk_write_intent",
            }
        ),
        None,
    )
    if (
        prior_receipt is None
        or prior_receipt.episode_id != episode_id
        or any(
            item.phase not in _PHASE_ORDER
            or item.episode_id != episode_id
            or item.group_id != context.group_id
            or item.operation_id != context.operation_id
            or item.lease_token != context.lease_token
            or item.mapping_revision != context.mapping_revision
            for item in receipts
        )
    ):
        return None
    ordered_phases = [_PHASE_ORDER[item.phase] for item in receipts]
    if ordered_phases != sorted(ordered_phases):
        return None
    return prior_receipt


def _known_ids(
    receipts: tuple[GraphWriteReceipt, ...],
    aggregate: Any,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Return (entity, mention, fact) ids, preferring the owner-certified complete aggregate."""
    if aggregate is not None:
        # Physical readback covers the complete owner-certified journal effects.
        return aggregate.entity_ids, aggregate.mention_ids, aggregate.fact_ids
    return (
        tuple(sorted({value for item in receipts for value in item.entity_ids})),
        tuple(sorted({value for item in receipts for value in item.mention_ids})),
        tuple(sorted({value for item in receipts for value in item.fact_ids})),
    )


def _actions_within_bounds(view: _ReceiptView) -> bool:
    """Check detached recovery actions stay inside the shared 100-id cap and the journaled ids."""
    receipts = view.receipts
    aggregate_ids = (
        set(view.known_entity_ids) | set(view.known_mention_ids) | set(view.known_fact_ids)
    )
    aggregate_ids.update(link.edge_id for item in receipts for link in item.incident_links)
    node_actions, fact_actions = view.node_actions, view.fact_actions
    return not (
        len(
            aggregate_ids
            | {item.graph_entity_uuid for item in node_actions}
            | {link.edge_id for item in node_actions for link in item.expected_incident_links}
            | {item.fact_id for item in fact_actions}
        )
        > MAX_SUPPORT
        or any(item.graph_entity_uuid not in set(view.known_entity_ids) for item in node_actions)
        or any(item.fact_id not in set(view.known_fact_ids) for item in fact_actions)
    )


def _fact_actions_proven(episode_id: UUID, view: _ReceiptView) -> bool:
    """Check each fact action matches a journaled endpoint pair and state and excludes this episode."""
    fact_receipts = tuple(item for item in view.receipts if item.phase in _FACT_PHASES)
    fact_proofs: dict[str, set[tuple[str, str]]] = {}
    for item in fact_receipts:
        for state in _fact_states(item):
            fact_proofs.setdefault(state.fact_id, set()).add(
                (state.source_node_id, state.target_node_id),
            )
    return not any(
        (item.source_node_id, item.target_node_id) not in fact_proofs.get(item.fact_id, set())
        or not any(
            state.state_fingerprint == item.expected_current_state_fingerprint
            for receipt in fact_receipts
            for state in _fact_states(receipt)
            if state.fact_id == item.fact_id
        )
        or (item.replacement is not None and str(episode_id) in item.replacement.episode_ids)
        for item in view.fact_actions
    )


async def _validate_delete_receipts(
    context: OperationAuthorization,
    episode_id: UUID,
) -> _ReceiptView | None:
    """Validate the owner receipt history and detached recovery actions before any graph access.

    Returns None whenever the history is missing, belongs to another lease, is
    mis-ordered, exceeds the id bound or carries unproven fact actions; the caller
    then reports an id-free ``unknown`` outcome. May raise ``GraphOperationError``
    from the owner's aggregate authorization.
    """
    receipts = _receipt_history(context)
    prior_receipt = _receipts_belong_to_delete(context, episode_id, receipts)
    if prior_receipt is None:
        return None
    aggregate = await _authorize_receipt_witnesses(context, receipts, "delete")
    entity_ids, mention_ids, fact_ids = _known_ids(receipts, aggregate)
    view = _ReceiptView(
        receipts=receipts,
        prior_receipt=prior_receipt,
        known_entity_ids=entity_ids,
        known_mention_ids=mention_ids,
        known_fact_ids=fact_ids,
        node_actions=context.node_recovery_actions,
        fact_actions=context.fact_recovery_actions,
    )
    if not _actions_within_bounds(view) or not _fact_actions_proven(episode_id, view):
        return None
    return view


async def _authorize_recoveries(
    context: OperationAuthorization,
    receipts: tuple[GraphWriteReceipt, ...],
    node_actions: tuple[CanonicalNodeRecoveryAction, ...],
    fact_actions: tuple[ExactFactRecoveryAction, ...],
) -> None:
    """Re-authorize detached node then fact recovery actions against the current receipts.

    Fact authorization receives only the fact-bearing receipts (bulk/cleanup).
    """
    if node_actions:
        if context.authorize_node_recovery is None:
            raise RuntimeError(
                "invariant violated: context.authorize_node_recovery must not be None"
            )
        await context.authorize_node_recovery(receipts, node_actions)
    if fact_actions:
        fact_receipts = tuple(item for item in receipts if item.phase in _FACT_PHASES)
        if context.authorize_fact_recovery is None:
            raise RuntimeError(
                "invariant violated: context.authorize_fact_recovery must not be None"
            )
        await context.authorize_fact_recovery(fact_receipts, fact_actions)


async def _get_episode_or_none(driver: Any, episode_id: UUID) -> Any:
    """Fetch the episode node, returning None only for Graphiti's NodeNotFoundError."""
    from graphiti_core.nodes import EpisodicNode

    try:
        return await EpisodicNode.get_by_uuid(driver, str(episode_id))
    except Exception as exc:
        if not _node_absent(exc):
            raise
        return None


def _check_episode_state(
    context: OperationAuthorization,
    view: _ReceiptView,
    episode: Any,
) -> None:
    """Reject an episode in another partition or whose shell drifted from every journaled fingerprint."""
    if episode is not None and episode.group_id != context.group_id:
        raise GraphOperationError("graph_episode_partition_mismatch")
    if episode is not None and view.prior_receipt.episode_state_fingerprint:
        allowed_episode_states = {
            value
            for item in view.receipts
            for value in (item.episode_state_fingerprint, item.prior_episode_state_fingerprint)
            if value
        }
        if _episode_state_fingerprint(episode) not in allowed_episode_states:
            raise GraphOperationError("graph_episode_state_changed")


async def _prove_missing_facts(
    context: OperationAuthorization,
    episode_id: UUID,
    view: _ReceiptView,
    inventory: _EpisodeInventory,
) -> None:
    """Prove each absent candidate fact had no other support, or has an owner-proved cross-rebuild absence."""
    present = {str(edge.uuid) for edge in inventory.edges}
    for missing_fact_id in set(inventory.candidate_fact_ids) - present:
        latest_intent = next(
            (
                support_item
                for item in reversed(view.receipts)
                for support_item in item.intended_fact_support
                if support_item.fact_id == missing_fact_id
            ),
            None,
        )
        remaining_support = (
            set(latest_intent.episode_ids) - {str(episode_id)}
            if latest_intent is not None
            else set()
        )
        if remaining_support:
            # Current exact absence is required before consulting another operation's rebuild proof.
            if latest_intent is None:
                raise RuntimeError("invariant violated: latest_intent must not be None")
            if not await _authorize_cross_rebuild_absence(
                context,
                "fact",
                missing_fact_id,
                episode_id=UUID(str(episode_id)),
                endpoints=(latest_intent.source_node_id, latest_intent.target_node_id),
            ):
                raise GraphOperationError("graph_delete_fact_absence_unproved")
            inventory.cross_rebuild_absent_fact_ids.add(missing_fact_id)


def _check_support_inventory(context: OperationAuthorization, edges: list[Any]) -> None:
    """Require bounded, unique, well-formed edge support confined to the authorized partition."""
    if len(edges) > MAX_SUPPORT or any(
        edge.group_id != context.group_id
        or any(not _UUID.fullmatch(str(value)) for value in edge.episodes)
        or len({str(value) for value in edge.episodes}) != len(edge.episodes)
        or not {str(value) for value in edge.episodes} <= set(context.partition_episode_ids)
        for edge in edges
    ):
        raise GraphOperationError("graph_delete_support_inventory_invalid")


_MENTION_QUERY = (
    "MATCH (episode:Episodic {uuid: $episode_id, group_id: $group_id})-"
    "[mention:MENTIONS]->() RETURN mention.uuid AS mention_id "
    "ORDER BY mention.uuid LIMIT $limit"
)


async def _check_live_mentions(
    driver: Any,
    context: OperationAuthorization,
    episode_id: UUID,
    known_mention_ids: tuple[str, ...],
) -> None:
    """Require the episode's live MENTIONS relationships to equal receipt-journaled, group-owned edges."""
    from graphiti_core.edges import EpisodicEdge

    mentions = (
        await EpisodicEdge.get_by_uuids(
            driver,
            list(known_mention_ids),
        )
        if known_mention_ids
        else []
    )
    records, _, _ = await driver.execute_query(
        _MENTION_QUERY,
        episode_id=str(episode_id),
        group_id=context.group_id,
        limit=MAX_SUPPORT + 1,
    )
    if not isinstance(records, list) or len(records) > MAX_SUPPORT:
        raise GraphOperationError("graph_episode_mention_inventory_invalid")
    live_mention_ids = {row.get("mention_id") for row in records if isinstance(row, dict)}
    if (
        len(live_mention_ids) > MAX_SUPPORT
        or len(live_mention_ids) != len(records)
        or not live_mention_ids <= set(known_mention_ids)
        or {str(item.uuid) for item in mentions} != live_mention_ids
        or any(
            item.group_id != context.group_id or str(item.source_node_uuid) != str(episode_id)
            for item in mentions
        )
    ):
        raise GraphOperationError("graph_episode_mention_receipt_mismatch")


def _record_fact_fingerprints(
    context: OperationAuthorization,
    view: _ReceiptView,
    inventory: _EpisodeInventory,
) -> None:
    """Record each live fact fingerprint and require it to match a journaled state."""
    for edge in inventory.edges:
        fact_id = str(edge.uuid)
        current_fingerprint = _fact_state(edge, context.embedding.dimensions).state_fingerprint
        inventory.observed_fact_fingerprints[fact_id] = current_fingerprint
        allowed = {
            state.state_fingerprint
            for item in view.receipts
            for state in _fact_states(item)
            if state.fact_id == fact_id
        }
        if current_fingerprint not in allowed:
            raise GraphOperationError("graph_episode_fact_state_changed")


async def _inventory_episode(
    driver: Any,
    context: OperationAuthorization,
    episode_id: UUID,
    view: _ReceiptView,
    progress: _DeleteProgress,
) -> _EpisodeInventory:
    """Read and verify the episode, its fact edges and mentions against the receipt history.

    Read-only. Sets ``progress.affected_ids`` as soon as the candidate fact ids are
    known so a later failure still reports them. Raises ``GraphOperationError`` for
    partition, state, support or mention-inventory mismatches.
    """
    from graphiti_core.edges import EntityEdge

    episode = await _get_episode_or_none(driver, episode_id)
    _check_episode_state(context, view, episode)
    episode_fact_ids = {str(value) for value in episode.entity_edges} if episode else set()
    if not episode_fact_ids <= set(view.known_fact_ids):
        raise GraphOperationError("graph_episode_receipt_mismatch")
    candidate_fact_ids = tuple(
        sorted(set(view.known_fact_ids) | {str(value) for value in episode_fact_ids})
    )
    if len(candidate_fact_ids) > MAX_SUPPORT:
        raise GraphOperationError("graph_episode_receipt_mismatch")
    edges = await EntityEdge.get_by_uuids(driver, list(candidate_fact_ids))
    _require_group(edges, context.group_id)
    inventory = _EpisodeInventory(
        episode=episode, edges=edges, candidate_fact_ids=candidate_fact_ids
    )
    for edge in edges:
        await edge.load_fact_embedding(driver)
    await _prove_missing_facts(context, episode_id, view, inventory)
    progress.affected_ids = candidate_fact_ids
    _check_support_inventory(context, edges)
    await _check_live_mentions(driver, context, episode_id, view.known_mention_ids)
    _record_fact_fingerprints(context, view, inventory)
    return inventory


def _cleanup_obligations(
    receipts: tuple[GraphWriteReceipt, ...],
    episode_id: UUID,
) -> tuple[set[str], set[str]]:
    """Rebuild (shared, stale) obligations from owned cleanup history, even after support was removed."""
    shared_obligations: set[str] = set()
    stale_obligations: set[str] = set()
    for receipt_item in (item for item in receipts if item.phase in _FACT_PHASES):
        prior_states = {item.fact_id: item for item in receipt_item.existing_fact_states}
        intended_states = {item.fact_id: item for item in receipt_item.intended_fact_support}
        for fact_id in set(prior_states) & set(intended_states):
            prior_state = prior_states[fact_id]
            intended_state = intended_states[fact_id]
            target_was_supported = str(episode_id) in prior_state.episode_ids
            target_is_supported = str(episode_id) in intended_state.episode_ids
            is_cleanup = receipt_item.phase == "cleanup_write_intent" and prior_state.existed
            if (
                is_cleanup
                and target_was_supported
                and not target_is_supported
                and intended_state.episode_ids
            ):
                shared_obligations.add(fact_id)
            # A retry must preserve recomputation required by an earlier owned cleanup.
            if (
                is_cleanup
                and not target_was_supported
                and not target_is_supported
                and (prior_state.valid_at, prior_state.invalid_at, prior_state.expired_at)
                != (intended_state.valid_at, intended_state.invalid_at, intended_state.expired_at)
            ):
                stale_obligations.add(fact_id)
    return shared_obligations, stale_obligations


def _validity_changed(receipts: tuple[GraphWriteReceipt, ...], fact_id: str) -> bool:
    """Report whether extraction itself changed the fact's validity window (not undoable by subtraction)."""
    historical_prior = next(
        (
            state
            for item in receipts
            for state in item.existing_fact_states
            if state.fact_id == fact_id and state.existed
        ),
        None,
    )
    historical_intended = next(
        (
            state
            for item in receipts
            for state in item.intended_fact_support
            if state.fact_id == fact_id
        ),
        None,
    )
    return (
        historical_prior is not None
        and historical_intended is not None
        and (historical_prior.valid_at, historical_prior.invalid_at, historical_prior.expired_at)
        != (
            historical_intended.valid_at,
            historical_intended.invalid_at,
            historical_intended.expired_at,
        )
    )


def _subtract_support(edge: Any, remaining: list[Any], dimensions: int | None) -> ExactFactSupport:
    """Build the intended post-delete support record for one edge from a non-mutating copy."""
    reduced = copy.copy(edge)
    reduced.episodes = remaining
    intended_fingerprint = (
        _ABSENT_FACT_FINGERPRINT
        if not remaining
        else _fact_state(reduced, dimensions).state_fingerprint
    )
    return ExactFactSupport(
        fact_id=str(edge.uuid),
        episode_ids=tuple(sorted(str(value) for value in remaining)),
        source_node_id=str(edge.source_node_uuid),
        target_node_id=str(edge.target_node_uuid),
        valid_at=edge.valid_at,
        invalid_at=edge.invalid_at,
        expired_at=edge.expired_at,
        state_fingerprint=intended_fingerprint,
    )


def _plan_support_subtraction(
    inventory: _EpisodeInventory,
    view: _ReceiptView,
    episode_id: UUID,
    dimensions: int | None,
) -> DeletePlan:
    """Pure: compute shared, removed, stale and pending facts from the inventory and receipts.

    Edges lacking this episode contribute only a stale marker (when extraction
    changed their validity); edges with other support are shared (support rewritten
    without this episode); edges with no other support are removed. No graph I/O
    and the inventory edges are not mutated.
    """
    shared_obligations, stale_obligations = _cleanup_obligations(view.receipts, episode_id)
    shared: list[str] = []
    remove: list[str] = []
    support: list[ExactFactSupport] = []
    states: list[ExactFactState] = []
    stale: set[str] = set()
    for edge in inventory.edges:
        remaining = [item for item in edge.episodes if item != str(episode_id)]
        fact_id = str(edge.uuid)
        if len(remaining) == len(edge.episodes):
            if _validity_changed(view.receipts, fact_id):
                # Support subtraction cannot undo a separate extraction-time invalidation.
                stale.add(fact_id)
            continue
        states.append(_fact_state(edge, dimensions))
        support.append(_subtract_support(edge, remaining, dimensions))
        # T3 suppresses stale fact text until surviving evidence recomputes it.
        (shared if remaining else remove).append(fact_id)
    stale.update(stale_obligations)
    shared_ids = tuple(sorted({str(value) for value in shared} | stale | shared_obligations))
    return DeletePlan(
        shared_ids=shared_ids,
        remove_ids=tuple(remove),
        support=tuple(support),
        states=tuple(states),
        stale=frozenset(stale),
    )


def _cleanup_receipt(
    context: OperationAuthorization,
    episode_id: UUID,
    view: _ReceiptView,
    inventory: _EpisodeInventory,
    plan: DeletePlan,
) -> GraphWriteReceipt:
    """Build the pre-mutation ``cleanup_write_intent`` receipt for the planned support subtraction."""
    digest = hashlib.sha256(
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
                for item in plan.support
            ],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    episode = inventory.episode
    return GraphWriteReceipt(
        operation_id=context.operation_id,
        lease_token=context.lease_token,
        episode_id=episode_id,
        group_id=context.group_id,
        mapping_revision=context.mapping_revision,
        desired_support_digest=digest,
        phase="cleanup_write_intent",
        prior_episode_state_fingerprint=(
            _episode_state_fingerprint(episode)
            if episode is not None
            else view.prior_receipt.episode_state_fingerprint
        ),
        entity_ids=view.known_entity_ids,
        mention_ids=view.known_mention_ids,
        fact_ids=tuple(sorted(item.fact_id for item in plan.states)),
        existing_fact_states=plan.states,
        intended_fact_support=plan.support,
        canonical_bindings=_receipt_bindings(context.canonical_bindings),
    )


async def _record_cleanup_intent(
    context: OperationAuthorization,
    episode_id: UUID,
    view: _ReceiptView,
    inventory: _EpisodeInventory,
    plan: DeletePlan,
    validate: Validate,
) -> tuple[GraphWriteReceipt, ...] | None:
    """Journal the cleanup intent through the owner before any graph mutation.

    Returns the receipt history extended by the new receipt, or None when
    partition revalidation or the owner journal write fails (nothing was mutated;
    the caller reports the partial ``unknown`` outcome). Building the receipt may
    raise ``ValueError`` and, like the original, is not part of the guarded block.
    """
    receipt = _cleanup_receipt(context, episode_id, view, inventory, plan)
    try:
        await validate()
        await context.record_write_intent(receipt)
        return (*view.receipts, receipt)
    except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
        return None


async def _recheck_fact(
    driver: Any,
    context: OperationAuthorization,
    fact_id: str,
    prior: ExactFactState | None,
    validate: Validate,
) -> Any:
    """Revalidate the partition and require the live fact to still match its journaled prior state."""
    from graphiti_core.edges import EntityEdge

    await validate()
    fresh_rows = await EntityEdge.get_by_uuids(driver, [fact_id])
    fresh = next((item for item in fresh_rows if str(item.uuid) == fact_id), None)
    if fresh is None or prior is None or fresh.group_id != context.group_id:
        raise GraphOperationError("graph_delete_fact_recheck_failed")
    await fresh.load_fact_embedding(driver)
    if (
        _fact_state(fresh, context.embedding.dimensions).state_fingerprint
        != prior.state_fingerprint
    ):
        raise GraphOperationError("graph_delete_fact_recheck_failed")
    return fresh


async def _write_shared_support(
    driver: Any,
    context: OperationAuthorization,
    edge_id: str,
    intended: ExactFactSupport,
) -> None:
    """Write one shared fact's reduced support, then read it back and compare to the intended state."""
    from graphiti_core.edges import EntityEdge

    mutation_rows, _, _ = await driver.execute_query(
        "MATCH (source:Entity {uuid: $source_id, group_id: $group_id})-"
        "[edge:RELATES_TO {uuid: $fact_id, group_id: $group_id}]->"
        "(target:Entity {uuid: $target_id, group_id: $group_id}) "
        "SET edge.episodes = $episode_ids RETURN edge.uuid AS fact_id",
        source_id=intended.source_node_id,
        target_id=intended.target_node_id,
        group_id=context.group_id,
        fact_id=edge_id,
        episode_ids=list(intended.episode_ids),
    )
    if (
        not isinstance(mutation_rows, list)
        or len(mutation_rows) != 1
        or not isinstance(mutation_rows[0], dict)
        or mutation_rows[0].get("fact_id") != edge_id
    ):
        raise GraphOperationError("graph_delete_fact_support_write_unconfirmed")
    saved_rows = await EntityEdge.get_by_uuids(driver, [edge_id])
    saved = next((item for item in saved_rows if str(item.uuid) == edge_id), None)
    if saved is None or saved.group_id != context.group_id:
        raise GraphOperationError("graph_delete_fact_support_readback_missing")
    await saved.load_fact_embedding(driver)
    if _fact_state(
        saved, context.embedding.dimensions
    ).state_fingerprint != intended.state_fingerprint or {
        str(value) for value in saved.episodes
    } != set(intended.episode_ids):
        raise GraphOperationError("graph_delete_fact_support_readback_mismatch")


async def _apply_support_writes(
    driver: Any,
    context: OperationAuthorization,
    plan: DeletePlan,
    validate: Validate,
) -> None:
    """Apply the plan edge by edge: recheck -> write -> read-back (shared), recheck -> delete (removed).

    Shared facts are processed first in sorted-id order, then fully unsupported
    facts. Any mismatch raises ``GraphOperationError`` and leaves later edges untouched.
    """
    from graphiti_core.edges import EntityEdge

    prior_by_id = {item.fact_id: item for item in plan.states}
    intended_by_id = {item.fact_id: item for item in plan.support}
    for edge_id in plan.shared_ids:
        intended = intended_by_id.get(edge_id)
        if intended is None or not intended.episode_ids:
            continue
        await _recheck_fact(driver, context, edge_id, prior_by_id.get(edge_id), validate)
        await _write_shared_support(driver, context, edge_id, intended)
    for fact_id in plan.remove_ids:
        await _recheck_fact(driver, context, fact_id, prior_by_id.get(fact_id), validate)
        await EntityEdge.delete_by_uuids(driver, [fact_id])


async def _recheck_and_delete_episode(
    driver: Any,
    context: OperationAuthorization,
    episode_id: UUID,
    receipts: tuple[GraphWriteReceipt, ...],
    known_mention_ids: tuple[str, ...],
    validate: Validate,
) -> None:
    """Recheck the episode shell and its mentions against the journal, then delete the episode node."""
    from graphiti_core.edges import EpisodicEdge
    from graphiti_core.nodes import EpisodicNode

    await validate()
    latest_episode = await EpisodicNode.get_by_uuid(driver, str(episode_id))
    _require_group([latest_episode], context.group_id)
    if _episode_state_fingerprint(latest_episode) not in {
        item.episode_state_fingerprint
        for item in receipts
        if item.episode_state_fingerprint is not None
    }:
        raise GraphOperationError("graph_delete_episode_recheck_failed")
    latest_mentions = (
        await EpisodicEdge.get_by_uuids(driver, list(known_mention_ids))
        if known_mention_ids
        else []
    )
    _require_group(latest_mentions, context.group_id)
    live_links, _, _ = await driver.execute_query(
        _MENTION_QUERY,
        episode_id=str(episode_id),
        group_id=context.group_id,
        limit=MAX_SUPPORT + 1,
    )
    live_ids = (
        {row.get("mention_id") for row in live_links if isinstance(row, dict)}
        if isinstance(live_links, list)
        else set()
    )
    if (
        not isinstance(live_links, list)
        or len(live_links) > MAX_SUPPORT
        or len(live_ids) != len(live_links)
        or not live_ids <= set(known_mention_ids)
        or {str(item.uuid) for item in latest_mentions} != live_ids
        or any(
            item.group_id != context.group_id or str(item.source_node_uuid) != str(episode_id)
            for item in latest_mentions
        )
    ):
        raise GraphOperationError("graph_delete_mention_recheck_failed")
    await latest_episode.delete(driver)


async def _delete_and_verify_episode(
    driver: Any,
    context: OperationAuthorization,
    episode_id: UUID,
    episode: Any,
    receipts: tuple[GraphWriteReceipt, ...],
    known_mention_ids: tuple[str, ...],
    validate: Validate,
) -> _EpisodeVerification:
    """Delete the episode node (when present) and read back mentions and reverse fact support.

    The returned verification feeds the terminal verdict: the episode must be
    absent, no mention may remain and no edge in the group may still list the
    episode as support. Raises ``GraphOperationError`` if the reverse-support read
    is malformed or a recheck fails.
    """
    from graphiti_core.edges import EpisodicEdge

    if episode is not None:
        await _recheck_and_delete_episode(
            driver, context, episode_id, receipts, known_mention_ids, validate
        )
    episode_absent = await _get_episode_or_none(driver, episode_id) is None
    remaining_mentions = (
        await EpisodicEdge.get_by_uuids(driver, list(known_mention_ids))
        if known_mention_ids
        else []
    )
    _require_group(remaining_mentions, context.group_id)
    reverse_support, _, _ = await driver.execute_query(
        "MATCH ()-[edge {group_id: $group_id}]->() WHERE $episode_id IN coalesce(edge.episodes, []) "
        "RETURN edge.uuid AS fact_id ORDER BY edge.uuid LIMIT $limit",
        episode_id=str(episode_id),
        group_id=context.group_id,
        limit=MAX_SUPPORT + 1,
    )
    if (
        not isinstance(reverse_support, list)
        or len(reverse_support) > MAX_SUPPORT
        or any(
            not isinstance(row, dict) or not _UUID.fullmatch(str(row.get("fact_id")))
            for row in reverse_support
        )
    ):
        raise GraphOperationError("graph_delete_reverse_support_invalid")
    return _EpisodeVerification(episode_absent, remaining_mentions, reverse_support)


async def _read_node_links(
    driver: Any,
    context: OperationAuthorization,
    entity_id: str,
) -> tuple[ExactGraphLink, ...]:
    """Read and validate the complete bounded incident-link set for terminal node proof."""
    return await _read_exact_links(driver, context, entity_id, "graph_delete_node_links_invalid")


def _replacement_converged(
    context: OperationAuthorization,
    episode_id: UUID,
    receipts: tuple[GraphWriteReceipt, ...],
    known_entity_ids: tuple[str, ...],
    action: CanonicalNodeRecoveryAction,
    entity: Any,
    fingerprint: str | None,
) -> bool:
    """Prove a ``replace_from_current_support`` action matches the latest journaled intent and proof."""
    node_id = action.graph_entity_uuid
    if (
        entity is None
        or fingerprint != action.expected_current_state_fingerprint
        or node_id not in set(known_entity_ids)
    ):
        return False
    latest_node_intent = next(
        (
            value
            for item in reversed(receipts)
            if item.phase == "cleanup_write_intent"
            for intent_id, value in item.intended_entity_state_fingerprints
            if intent_id == node_id
        ),
        None,
    )
    if latest_node_intent != fingerprint:
        return False
    latest_node_receipt = next(
        (
            item
            for item in reversed(receipts)
            if item.phase == "cleanup_write_intent"
            and any(
                intent_id == node_id for intent_id, _ in item.intended_entity_state_fingerprints
            )
        ),
        None,
    )
    if latest_node_receipt is None:
        return False
    binding = action.replacement_binding or next(
        (
            item
            for receipt_item in receipts
            for item in receipt_item.canonical_bindings
            if item.graph_entity_uuid == node_id
        ),
        None,
    )
    candidate = action.replacement_candidate
    if binding is None and candidate is None:
        return False
    if binding is not None and (
        binding.graph_entity_uuid != node_id or binding.group_id != context.group_id
    ):
        return False
    if candidate is not None and not _candidate_proved(
        context,
        episode_id,
        latest_node_receipt,
        node_id,
        candidate,
    ):
        return False
    if binding is not None:
        binding_proof = next(
            (
                item
                for item in latest_node_receipt.canonical_bindings
                if item.graph_entity_uuid == node_id
            ),
            None,
        )
        if (
            binding_proof is None
            or binding_proof.group_id != binding.group_id
            or binding_proof.canonical_revision != binding.canonical_revision
            or binding_proof.field_support != binding.field_support
        ):
            return False
    return True


def _candidate_proved(
    context: OperationAuthorization,
    episode_id: UUID,
    latest_node_receipt: GraphWriteReceipt,
    node_id: str,
    candidate: Any,
) -> bool:
    """Check a replacement candidate excludes this episode, stays in-partition and matches the journaled field support."""
    if (
        candidate.graph_entity_uuid != node_id
        or candidate.group_id != context.group_id
        or str(episode_id) in candidate.support_episode_ids
        or not set(candidate.support_episode_ids) <= set(context.partition_episode_ids)
    ):
        return False
    candidate_proof = {
        (field_name, digest, support_ids)
        for entity_id, field_name, digest, support_ids in latest_node_receipt.candidate_field_support
        if entity_id == node_id
    }
    return candidate_proof == set(candidate.field_support)


async def _absent_delete_converged(
    driver: Any,
    context: OperationAuthorization,
    episode_id: UUID,
    receipts: tuple[GraphWriteReceipt, ...],
    action: CanonicalNodeRecoveryAction,
) -> bool:
    """Prove a missing node was legitimately deleted (own intent, never dispatched, or cross-rebuild)."""
    node_id = action.graph_entity_uuid
    deletion_intent = any(
        item.phase == "cleanup_write_intent"
        and (node_id, None) in item.intended_entity_state_fingerprints
        and (
            item.rebuild_absence_observed == "node"
            or any(
                entity_id == node_id and existed and state is not None
                for entity_id, existed, state in item.prior_entity_state_fingerprints
            )
        )
        for item in receipts
    )
    no_dispatch_absence = (
        any(
            item.phase == "bulk_ids_intent"
            and any(
                entity_id == node_id and not existed and state is None
                for entity_id, existed, state in item.prior_entity_state_fingerprints
            )
            for item in receipts
        )
        and not any(
            item.phase
            in {
                "canonical_node_write_intent",
                "bulk_write_intent",
            }
            and node_id in item.entity_ids
            for item in receipts
        )
        and not any(
            item.phase == "cleanup_write_intent" and node_id in item.entity_ids for item in receipts
        )
    )
    if not (
        deletion_intent
        or no_dispatch_absence
        or await _authorize_cross_rebuild_absence(
            context,
            "node",
            node_id,
            episode_id=UUID(str(episode_id)),
        )
    ):
        return False
    return action.action != "delete_for_rebuild" or await _incident_ids_absent(
        driver,
        [
            link.edge_id
            for item in receipts
            if node_id in item.entity_ids
            for link in item.incident_links
        ]
        + [link.edge_id for link in action.expected_incident_links],
        context.group_id,
    )


async def _recover_nodes(
    driver: Any,
    context: OperationAuthorization,
    episode_id: UUID,
    view: _ReceiptView,
    receipts: tuple[GraphWriteReceipt, ...],
) -> set[str]:
    """Return ids of node recovery actions whose exact live state proves convergence.

    Reads each node and its complete incident-link set; an action converges only
    when links equal the authorized inventory (or the node is provably absent and
    the action deletes it) and the action-specific proof holds.
    """
    converged: set[str] = set()
    for action in view.node_actions:
        entity = await _load_action_entity(driver, context, action.graph_entity_uuid)
        links = await _read_node_links(driver, context, action.graph_entity_uuid)
        fingerprint = (
            _entity_state_fingerprint(entity, context.embedding.dimensions)
            if entity is not None
            else None
        )
        if links != action.expected_incident_links and not (
            entity is None and not links and action.action == "delete_for_rebuild"
        ):
            continue
        if action.action == "retain":
            done = entity is not None and fingerprint == action.expected_current_state_fingerprint
        elif action.action == "replace_from_current_support":
            done = _replacement_converged(
                context,
                episode_id,
                receipts,
                view.known_entity_ids,
                action,
                entity,
                fingerprint,
            )
        elif action.action in {"delete_orphan", "delete_for_rebuild"} and entity is None:
            done = await _absent_delete_converged(driver, context, episode_id, receipts, action)
        else:
            done = False
        if done:
            converged.add(action.graph_entity_uuid)
    return converged


def _latest_support(receipts: tuple[GraphWriteReceipt, ...]) -> dict[str, ExactFactSupport]:
    """Return the most recently journaled intended support for every fact id."""
    latest: dict[str, ExactFactSupport] = {}
    for receipt_item in receipts:
        for support_item in receipt_item.intended_fact_support:
            latest[support_item.fact_id] = support_item
    return latest


def _absence_proved(
    action: ExactFactRecoveryAction, receipts: tuple[GraphWriteReceipt, ...]
) -> bool:
    """Prove a deleting fact action is backed by an owned cleanup intent or a journaled never-existed fact."""
    cleanup_proved = any(
        item.phase == "cleanup_write_intent"
        and any(
            value.fact_id == action.fact_id
            and value.state_fingerprint == _ABSENT_FACT_FINGERPRINT
            and not value.episode_ids
            and value.source_node_id == action.source_node_id
            and value.target_node_id == action.target_node_id
            for value in item.intended_fact_support
        )
        and any(
            value.fact_id == action.fact_id
            and value.existed
            and value.source_node_id == action.source_node_id
            and value.target_node_id == action.target_node_id
            and value.state_fingerprint == action.expected_current_state_fingerprint
            for value in item.existing_fact_states
        )
        for item in receipts
    )
    never_existed = any(
        any(
            value.fact_id == action.fact_id
            and not value.existed
            and value.source_node_id == action.source_node_id
            and value.target_node_id == action.target_node_id
            for value in item.existing_fact_states
        )
        and any(
            value.fact_id == action.fact_id
            and value.source_node_id == action.source_node_id
            and value.target_node_id == action.target_node_id
            for value in item.intended_fact_support
        )
        for item in receipts
    )
    return cleanup_proved or never_existed


def _check_known_fact(
    context: OperationAuthorization,
    episode_id: UUID,
    fact_id: str,
    expected: ExactFactSupport | None,
    current: Any,
    observed_fingerprint: str | None,
) -> tuple[bool, ExactFactSupport | None]:
    """Compare one non-authorized-absent fact with its expectation; return (matches, expected survivor)."""
    dims = context.embedding.dimensions
    if expected is None:
        if observed_fingerprint is None:
            return current is None, None
        if (
            current is None
            or current.group_id != context.group_id
            or _fact_state(current, dims).state_fingerprint != observed_fingerprint
        ):
            return False, None
        return True, ExactFactSupport(
            fact_id=fact_id,
            episode_ids=tuple(sorted(str(value) for value in current.episodes)),
            source_node_id=str(current.source_node_uuid),
            target_node_id=str(current.target_node_uuid),
            state_fingerprint=observed_fingerprint,
        )
    if expected.episode_ids:
        matches = not (
            current is None
            or current.group_id != context.group_id
            or str(current.source_node_uuid) != expected.source_node_id
            or str(current.target_node_uuid) != expected.target_node_id
            or tuple(sorted(str(value) for value in current.episodes)) != expected.episode_ids
            or str(episode_id) in expected.episode_ids
            or _fact_state(current, dims).state_fingerprint != expected.state_fingerprint
        )
        return matches, expected
    return expected.state_fingerprint == _ABSENT_FACT_FINGERPRINT and current is None, None


async def _verify_final_fact_inventory(
    driver: Any,
    context: OperationAuthorization,
    episode_id: UUID,
    view: _ReceiptView,
    receipts: tuple[GraphWriteReceipt, ...],
    inventory: _EpisodeInventory,
) -> _FinalFacts:
    """Read every journaled fact once more and check the surviving set equals the planned one.

    Facts with an authorized absence must be gone; the rest must match the latest
    intended support (or their observed fingerprint when no support was journaled).
    Raises ``GraphOperationError`` when the readback itself is malformed.
    """
    from graphiti_core.edges import EntityEdge

    latest = _latest_support(receipts)
    authorized_absent = {
        action.fact_id
        for action in view.fact_actions
        if action.action in {"delete_unsupported", "delete_for_rebuild"}
        and _absence_proved(action, receipts)
    }
    authorized_absent.update(inventory.cross_rebuild_absent_fact_ids)
    remaining_facts = (
        await EntityEdge.get_by_uuids(driver, list(view.known_fact_ids))
        if view.known_fact_ids
        else []
    )
    _require_group(remaining_facts, context.group_id)
    current_by_id = {str(edge.uuid): edge for edge in remaining_facts}
    if len(current_by_id) != len(remaining_facts) or not set(current_by_id) <= set(
        view.known_fact_ids
    ):
        raise GraphOperationError("graph_delete_fact_final_inventory_invalid")
    for edge in remaining_facts:
        await edge.load_fact_embedding(driver)
    survivors: dict[str, ExactFactSupport] = {}
    matches = True
    for fact_id in view.known_fact_ids:
        if fact_id in authorized_absent:
            matches = matches and fact_id not in current_by_id
            continue
        ok, survivor = _check_known_fact(
            context,
            episode_id,
            fact_id,
            latest.get(fact_id),
            current_by_id.get(fact_id),
            inventory.observed_fact_fingerprints.get(fact_id),
        )
        matches = matches and ok
        if survivor is not None:
            survivors[fact_id] = survivor
    return _FinalFacts(
        matches and set(current_by_id) == set(survivors),
        current_by_id,
        latest,
        authorized_absent,
    )


def _fact_replacement_converged(
    context: OperationAuthorization,
    episode_id: UUID,
    action: ExactFactRecoveryAction,
    current: Any,
    latest: ExactFactSupport | None,
) -> bool:
    """Prove the live fact equals the owner snapshot, excludes this episode and matches the latest journal."""
    snapshot = action.replacement
    if snapshot is None:
        raise RuntimeError("invariant violated: snapshot must not be None")
    dims = context.embedding.dimensions
    snapshot_fingerprint = _fact_recovery_fingerprint(_replacement_edge(snapshot), dims)
    return bool(
        current is not None
        and current.group_id == context.group_id
        and str(current.source_node_uuid) == action.source_node_id
        and str(current.target_node_uuid) == action.target_node_id
        and tuple(sorted(str(value) for value in current.episodes)) == snapshot.episode_ids
        and str(episode_id) not in snapshot.episode_ids
        and _fact_recovery_fingerprint(current, dims) == snapshot_fingerprint
        and latest is not None
        and latest.episode_ids == snapshot.episode_ids
        and latest.state_fingerprint == snapshot_fingerprint
    )


def _recover_facts(
    context: OperationAuthorization,
    episode_id: UUID,
    fact_actions: Sequence[ExactFactRecoveryAction],
    final: _FinalFacts,
) -> set[str]:
    """Return ids of fact recovery actions whose live state proves they have converged."""
    converged: set[str] = set()
    for fact_action in fact_actions:
        current = final.current_by_id.get(fact_action.fact_id)
        if (
            fact_action.action == "replace_from_current_support"
            and fact_action.replacement is not None
        ):
            if _fact_replacement_converged(
                context,
                episode_id,
                fact_action,
                current,
                final.latest_support.get(fact_action.fact_id),
            ):
                converged.add(fact_action.fact_id)
        elif (
            fact_action.action in {"delete_unsupported", "delete_for_rebuild"}
            and current is None
            and fact_action.fact_id in final.authorized_absent
        ):
            converged.add(fact_action.fact_id)
    return converged
