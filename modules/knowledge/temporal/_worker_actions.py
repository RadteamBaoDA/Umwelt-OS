"""Recovery authorization and node/fact recovery action planning for temporal graph operations.

Private implementation of ``modules.knowledge.temporal.worker``; import from that facade.
"""

import math
from dataclasses import replace
from typing import Any, cast
from uuid import UUID

from pydantic import TypeAdapter
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.model_gateway.client import ModelGateway, PrivacyPolicyDenied
from modules.knowledge.relationships import public as relationships
from modules.knowledge.temporal._worker_admission import (
    _Admission,
    _admit,
    _lease,
    _mark_dependents_for_rebuild,
)
from modules.knowledge.temporal.adapter import (
    CanonicalEntityBinding,
    CanonicalNodeRecoveryAction,
    DispatchOwnership,
    ExactFactRecoveryAction,
    ExactFactReplacement,
    GraphOperationError,
    GraphReceiptInspection,
    GraphWriteReceipt,
    OperationAuthorization,
)
from modules.knowledge.temporal.models import (
    GraphDispatch,
    GraphMapping,
    GraphOperation,
    GraphReceipt,
)
from modules.settings import public as settings_public
from modules.sources import public as sources


async def _recovery_authorization(
    factory: async_sessionmaker[AsyncSession],
    adm: _Admission,
    operation: GraphOperation,
    mapping: GraphMapping,
    token: UUID,
    receipts: tuple[GraphWriteReceipt, ...],
    active_dispatch: DispatchOwnership | None = None,
) -> None:
    """Compare each supplied receipt with the complete committed ledger and require concrete prior dispatch cessation."""
    await _lease(factory, adm, operation.id, token)
    async with factory() as check:
        await _admit(check, adm)
        missing = list(receipts)
        sequence = 0
        while missing:
            page = (
                await check.scalars(
                    select(GraphReceipt)
                    .where(
                        GraphReceipt.workspace_id == adm.scope.workspace_id,
                        GraphReceipt.operation_id == operation.id,
                        GraphReceipt.sequence >= sequence,
                    )
                    .order_by(GraphReceipt.sequence)
                    .limit(100)
                )
            ).all()
            if not page:
                break
            originals = [
                TypeAdapter(GraphWriteReceipt).validate_python(row.payload) for row in page
            ]
            missing = [receipt for receipt in missing if receipt not in originals]
            sequence = page[-1].sequence + 1
        unresolved = (
            await check.scalars(
                select(GraphDispatch).where(
                    GraphDispatch.workspace_id == adm.scope.workspace_id,
                    GraphDispatch.operation_id == operation.id,
                    GraphDispatch.completed_at.is_(None),
                    GraphDispatch.cessation_verified_at.is_(None),
                )
            )
        ).all()
        # Only the exact current scope may be live during its recovery callback;
        # an earlier ambiguous scope with the same task token still blocks.
        prior_uncertain = any(
            active_dispatch is None
            or row.lease_owner != token
            or row.server_run_id != active_dispatch.server_run_id
            or row.client_id != active_dispatch.client_id
            for row in unresolved
        )
        if (
            prior_uncertain
            or missing
            or any(
                receipt.operation_id != operation.id
                or receipt.lease_token != operation.receipt_token
                or receipt.group_id != str(mapping.partition_id)
                for receipt in receipts
            )
        ):
            raise GraphOperationError("graph_recovery_owner_proof_missing")


async def _validate_fact_replacement(
    session: AsyncSession,
    adm: _Admission,
    mapping: GraphMapping,
    bindings: tuple[CanonicalEntityBinding, ...],
    rows: list[GraphMapping],
    action: ExactFactRecoveryAction,
) -> None:
    """Authorize a fresh canonical relationship snapshot over exact mapped endpoints and survivor evidence."""
    endpoints = {binding.graph_entity_uuid: binding.canonical_entity_id for binding in bindings}
    left, right = endpoints.get(action.source_node_id), endpoints.get(action.target_node_id)
    if left is None or right is None:
        raise GraphOperationError("graph_candidate_fact_requires_current_owner_proof")
    snapshot = action.replacement
    if snapshot is None:
        raise RuntimeError("invariant violated: snapshot must not be None")
    relation = await _current_relationship_snapshot(session, adm, left, right, snapshot.name)
    version_ids = {
        row.document_version_id
        for row in rows
        if str(row.episode_id) in snapshot.episode_ids and not row.tombstoned
    }
    if (
        not relation
        or not relation.relationship.evidence
        or any(
            UUID(str(item["document_version_id"])) not in version_ids for item in relation.supports
        )
    ):
        raise GraphOperationError("graph_fact_current_evidence_unproved")
    seeds = {binding.graph_entity_uuid: binding for binding in bindings}
    left_seed, right_seed = seeds[action.source_node_id], seeds[action.target_node_id]
    expected_text = f"{left_seed.node_name} {relation.relationship.type} {right_seed.node_name}"
    if (
        not left_seed.node_name
        or not right_seed.node_name
        or snapshot.fact != expected_text
        or snapshot.valid_at != relation.relationship.valid_from
        or snapshot.invalid_at != relation.relationship.valid_to
        or snapshot.expired_at is not None
        or dict(snapshot.attributes)
        != {
            "canonical_relationship_id": str(relation.relationship.id),
            "canonical_snapshot_digest": relation.digest,
        }
    ):
        raise GraphOperationError("graph_fact_current_snapshot_unproved")


async def _current_relationship_snapshot(
    session: AsyncSession, adm: _Admission, left: UUID, right: UUID, kind: str | None = None
) -> relationships.RelationshipSnapshot | None:
    """Page owner relationships and lock the one unambiguous exact canonical endpoint match; no name inference."""
    cursor, found = None, None
    while True:
        page = await relationships.list_relationships(
            session,
            limit=100,
            cursor=cursor,
            entity_id=left,
            scope=adm.scope,
            multi_workspace_enabled=adm.multi,
        )
        for relation in page.items:
            if (
                relation.source_entity_id == left
                and relation.target_entity_id == right
                and (kind is None or relation.type == kind)
            ):
                if found is not None:
                    raise GraphOperationError("graph_fact_canonical_mapping_ambiguous")
                found = relation.id
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    if found is None:
        raise GraphOperationError("graph_fact_canonical_mapping_unavailable")
    await relationships.lock_relationship_ids(
        session,
        [found],
        scope=adm.scope,
        multi_workspace_enabled=adm.multi,
    )
    return await relationships.get_relationship_snapshot(
        session,
        found,
        scope=adm.scope,
        multi_workspace_enabled=adm.multi,
    )


async def _fresh_fact_action(
    factory: async_sessionmaker[AsyncSession],
    adm: _Admission,
    session: AsyncSession,
    ctx: dict[str, object],
    operation: GraphOperation,
    mapping: GraphMapping,
    token: UUID,
    context: OperationAuthorization,
    rows: list[GraphMapping],
    fact_id: str,
    source_node_id: str,
    target_node_id: str,
    expected: str,
    survivors: set[str],
) -> ExactFactRecoveryAction:
    """Build fresh canonical fact fields and a new finite vector from current exact surviving owner evidence.

    Old graph narrative, metadata, dates and embeddings are never reused. Endpoint
    identity is an exact canonical binding; ambiguous/candidate facts require the
    separate dependent-mapping rebuild path. Every send rechecks lease, current
    relationship snapshot, source generation and configured model identity.
    """
    bindings = {binding.graph_entity_uuid: binding for binding in context.canonical_bindings}
    left, right = bindings.get(source_node_id), bindings.get(target_node_id)
    if left is None or right is None or not left.node_name or not right.node_name:
        raise GraphOperationError("graph_candidate_fact_requires_rebuild")
    relation = await _current_relationship_snapshot(
        session, adm, left.canonical_entity_id, right.canonical_entity_id
    )
    if relation is None or not relation.relationship.evidence:
        raise GraphOperationError("graph_fact_current_evidence_unproved")
    versions = {
        row.document_version_id
        for row in rows
        if str(row.episode_id) in survivors and not row.tombstoned
    }
    supported_versions = {UUID(str(item["document_version_id"])) for item in relation.supports}
    if len(versions) != len(survivors) or supported_versions != versions:
        raise GraphOperationError("graph_fact_current_support_unproved")
    value = f"{left.node_name} {relation.relationship.type} {right.node_name}"
    model = context.embedding
    dimensions = model.dimensions
    if not model.mapping.model or dimensions is None:
        raise GraphOperationError("graph_embedding_identity_unconfigured")
    identity = mapping.embedding_identity
    if identity and (
        identity.get("model") != model.mapping.model
        or identity.get("version") != model.mapping.version
        or identity.get("dimensions") != dimensions
    ):
        raise GraphOperationError("graph_embedding_identity_changed_requires_rebuild")
    settings = cast(Settings, ctx["settings"])
    redis = cast(Redis, ctx["redis"])
    config = settings_public.derive_for_role(
        await settings_public.get_ai_execution_config(session, settings, redis, scope=adm.scope),
        "embedding",
    )

    async def before_send() -> None:
        """Fence a fresh surviving-fact embedding against policy/model drift and deleted canonical evidence."""
        await _lease(factory, adm, operation.id, token)
        current_source = await sources.get_source(
            session,
            mapping.source_id,
            scope=adm.scope,
            multi_workspace_enabled=adm.multi,
        )
        current_relation = await relationships.get_relationship_snapshot(
            session,
            relation.relationship.id,
            scope=adm.scope,
            multi_workspace_enabled=adm.multi,
        )
        async with factory() as check:
            await _admit(check, adm)
            current_config = await settings_public.get_ai_execution_config(
                check, settings, redis, scope=adm.scope
            )
        if (
            current_source is None
            or current_source.status != "active"
            or current_source.generation != mapping.source_generation
            or current_relation is None
            or current_relation.digest != relation.digest
            or current_config.configuration_revision != config.configuration_revision
            or current_config.defaults.resolve("embedding") != model.mapping
        ):
            raise PrivacyPolicyDenied("Current surviving fact support or model policy changed")

    gateway = ModelGateway(
        redis,
        config.gateway_base_url,
        config.gateway_api_key,
        config.endpoint_destination_id or "",
        timeout_seconds=min(config.request_timeout_seconds, 30),
        scope=adm.scope,
        gateway_identity=config.gateway_identity,
        model_identity=config.model_identity,
        configuration_revision=config.configuration_revision,
        before_send=before_send,
        approved_endpoint_cidrs=config.endpoint_allowed_cidrs,
        allow_public_endpoints=config.allow_public_providers,
        kind=config.gateway_kind,
    )

    raw = await gateway.embed(
        model.role, model.mapping, model.policy, [value], before_send=before_send
    )
    data = raw.get("data") if isinstance(raw, dict) else None
    vector = (
        data[0].get("embedding")
        if isinstance(data, list)
        and len(data) == 1
        and isinstance(data[0], dict)
        and data[0].get("index") == 0
        else None
    )
    if (
        not isinstance(vector, list)
        or len(vector) != dimensions
        or any(
            isinstance(item, bool) or not isinstance(item, int | float) or not math.isfinite(item)
            for item in vector
        )
        or not any(vector)
    ):
        raise GraphOperationError("graph_fact_embedding_invalid")
    replacement = ExactFactReplacement(
        fact_id=fact_id,
        group_id=context.group_id,
        source_node_id=source_node_id,
        target_node_id=target_node_id,
        name=relation.relationship.type,
        fact=value,
        episode_ids=tuple(sorted(survivors)),
        created_at=operation.replacement_created_at,
        reference_time=max(item.observed_at for item in relation.relationship.evidence),
        valid_at=relation.relationship.valid_from,
        invalid_at=relation.relationship.valid_to,
        expired_at=None,
        fact_embedding=tuple(float(item) for item in vector),
        attributes=(
            ("canonical_relationship_id", str(relation.relationship.id)),
            ("canonical_snapshot_digest", relation.digest),
        ),
    )
    return ExactFactRecoveryAction(
        fact_id,
        source_node_id,
        target_node_id,
        expected,
        "replace_from_current_support",
        replacement,
    )


def _initial_node_actions(
    context: OperationAuthorization,
    operation: GraphOperation,
) -> tuple[CanonicalNodeRecoveryAction, ...]:
    """Build one recovery action per journaled node: replace when a current binding exists, else delete as orphan.

    Fingerprints are the latest intended ones; replacement carries the current canonical binding
    and the operation's stable creation time. Pure over the owner context.
    """
    receipts = context.receipt_history
    latest_nodes = {
        identifier: state
        for receipt in receipts
        for identifier, state in receipt.intended_entity_state_fingerprints
    }
    bindings = {binding.graph_entity_uuid: binding for binding in context.canonical_bindings}
    return tuple(
        CanonicalNodeRecoveryAction(
            identifier,
            fingerprint,
            "replace_from_current_support" if identifier in bindings else "delete_orphan",
            replacement_binding=bindings.get(identifier),
            replacement_created_at=operation.replacement_created_at
            if identifier in bindings
            else None,
        )
        for identifier, fingerprint in sorted(latest_nodes.items())
    )


def _readback_matches_desired(
    mapping: GraphMapping,
    operation: GraphOperation,
    context: OperationAuthorization,
    receipts: tuple[GraphWriteReceipt, ...],
    inspection: GraphReceiptInspection,
    bulk: GraphWriteReceipt,
) -> bool:
    """Report whether an interrupted upsert's exact graph readback already equals the desired projection.

    True only when the mapping's desired revision/digest match the operation, the journaled
    bindings, episode, node, fact and mention fingerprints all match the inspection, and every
    mention link is a MENTIONS edge from this episode to a journaled node. Pure.
    """
    intended_nodes = {
        identifier: fingerprint
        for receipt in receipts
        for identifier, fingerprint in receipt.intended_entity_state_fingerprints
    }
    intended_facts = {
        state.fact_id: state for receipt in receipts for state in receipt.intended_fact_support
    }
    intended_mentions = {identifier for receipt in receipts for identifier in receipt.mention_ids}
    current_facts = {state.fact_id: state for state in inspection.current_fact_states}
    normalized_bindings = tuple(
        replace(binding, node_name=None, node_summary=None)
        for binding in context.canonical_bindings
    )
    return (
        mapping.desired_revision == operation.desired_revision
        and mapping.desired_digest == operation.desired_digest
        and tuple(bulk.canonical_bindings) == normalized_bindings
        and inspection.episode_present
        and inspection.episode_state_fingerprint == bulk.episode_state_fingerprint
        and dict(inspection.current_entity_state_fingerprints) == intended_nodes
        and set(current_facts) == set(intended_facts)
        and all(
            current_facts[identifier].state_fingerprint == state.state_fingerprint
            for identifier, state in intended_facts.items()
        )
        and set(inspection.mention_ids_present) == intended_mentions
        and {link.edge_id for link in inspection.current_mention_links} == intended_mentions
        and all(
            link.relationship_type == "MENTIONS"
            and link.source_node_id == str(mapping.episode_id)
            and link.target_node_id in intended_nodes
            for link in inspection.current_mention_links
        )
    )


def _bind_node_actions(
    actions: tuple[CanonicalNodeRecoveryAction, ...],
    inspection: GraphReceiptInspection,
) -> tuple[CanonicalNodeRecoveryAction, ...]:
    """Pin each node action to the inspected current fingerprint and exact incident links. Pure."""
    node_fingerprints = dict(inspection.current_entity_state_fingerprints)
    return tuple(
        replace(
            action,
            expected_current_state_fingerprint=node_fingerprints.get(action.graph_entity_uuid),
            expected_incident_links=tuple(
                link
                for link in inspection.incident_links
                if action.graph_entity_uuid in {link.source_node_id, link.target_node_id}
            ),
        )
        for action in actions
    )


async def _prepare_node_actions(
    factory: async_sessionmaker[AsyncSession],
    adm: _Admission,
    operation: GraphOperation,
    mapping: GraphMapping,
    token: UUID,
    rows: list[GraphMapping],
    actual_actions: tuple[CanonicalNodeRecoveryAction, ...],
) -> tuple[CanonicalNodeRecoveryAction, ...]:
    """Turn orphan deletions with other episodes' support into durable ``delete_for_rebuild`` actions.

    Marks the dependent mappings for rebuild (committing their schedules through
    ``_mark_dependents_for_rebuild``) before the destructive action is authorized.
    """
    prepared_actions = []
    for action in actual_actions:
        episodes = {
            episode for link in action.expected_incident_links for episode in link.episode_ids
        }
        episodes.update(
            link.source_node_id
            for link in action.expected_incident_links
            if link.relationship_type == "MENTIONS"
        )
        if action.action == "delete_orphan" and episodes - {str(mapping.episode_id)}:
            dependencies = await _mark_dependents_for_rebuild(
                factory,
                adm,
                operation,
                mapping,
                token,
                rows,
                episodes,
                {("node", action.graph_entity_uuid)}
                | {
                    ("fact" if link.relationship_type != "MENTIONS" else "mention", link.edge_id)
                    for link in action.expected_incident_links
                },
            )
            action = replace(action, action="delete_for_rebuild", rebuild_mapping_ids=dependencies)
        prepared_actions.append(action)
    return tuple(prepared_actions)


def _expected_fact_fingerprint(
    current: Any,
    receipts: tuple[GraphWriteReceipt, ...],
    fact_id: str,
    state: Any,
) -> str:
    """Pick the fingerprint a recovery action must match for one journaled fact.

    Pure: the current graph state wins; otherwise the newest receipt that observed the fact as
    existing; otherwise the intended support state itself.
    """
    expected = (
        current.state_fingerprint
        if current
        else next(
            (
                item.state_fingerprint
                for receipt in reversed(receipts)
                for item in receipt.existing_fact_states
                if item.fact_id == fact_id
            ),
            state.state_fingerprint,
        )
    )
    return expected


def _terminal_episode_ids(rows: list[GraphMapping]) -> set[str]:
    """Return episode ids of converged tombstoned mappings whose graph state is proven absent.

    Pure filter over already-loaded rows; these owners are discarded from a fact's support claim.
    """
    terminal = {
        str(row.episode_id)
        for row in rows
        if row.tombstoned
        and row.external_state == "absent"
        and row.applied_revision == row.desired_revision
        and row.applied_digest == row.desired_digest
        and row.error_code is None
    }
    return terminal


async def _build_fact_actions(
    factory: async_sessionmaker[AsyncSession],
    adm: _Admission,
    session: AsyncSession,
    ctx: dict[str, object],
    operation: GraphOperation,
    mapping: GraphMapping,
    token: UUID,
    context: OperationAuthorization,
    rows: list[GraphMapping],
    receipts: tuple[GraphWriteReceipt, ...],
    inspection: GraphReceiptInspection,
) -> list[ExactFactRecoveryAction]:
    """Decide, per journaled fact, whether to delete it, replace it from fresh support or rebuild it.

    A fact with no surviving support is deleted; surviving support yields a fresh replacement,
    falling back to a durable dependent rebuild for the recognised unprovable-support errors
    (any other ``GraphOperationError`` propagates). May consume another operation's
    rebuild-absence proof before discarding terminal owners from the support claim.
    """
    latest_support = {
        state.fact_id: state for receipt in receipts for state in receipt.intended_fact_support
    }
    current_facts = {state.fact_id: state for state in inspection.current_fact_states}
    fact_actions = []
    for fact_id, state in sorted(latest_support.items()):
        remaining = set(state.episode_ids) - {str(mapping.episode_id)}
        current = current_facts.get(fact_id)
        if current is None and remaining:
            # A different owned rebuild may already have physically removed
            # this fact. Consume that exact deletion proof before discarding
            # terminal owners from the historical support claim.
            if context.authorize_rebuild_absence is None:
                raise RuntimeError(
                    "invariant violated: context.authorize_rebuild_absence must not be None"
                )
            await context.authorize_rebuild_absence("fact", fact_id)
            terminal = _terminal_episode_ids(rows)
            remaining -= terminal
        expected = _expected_fact_fingerprint(current, receipts, fact_id, state)
        if not remaining:
            fact_actions.append(
                ExactFactRecoveryAction(
                    fact_id,
                    state.source_node_id,
                    state.target_node_id,
                    expected,
                    "delete_unsupported",
                )
            )
        else:
            try:
                fact_actions.append(
                    await _fresh_fact_action(
                        factory,
                        adm,
                        session,
                        ctx,
                        operation,
                        mapping,
                        token,
                        context,
                        rows,
                        fact_id,
                        state.source_node_id,
                        state.target_node_id,
                        expected,
                        remaining,
                    )
                )
            except GraphOperationError as exc:
                if str(exc) not in {
                    "graph_candidate_fact_requires_rebuild",
                    "graph_fact_canonical_mapping_unavailable",
                    "graph_fact_canonical_mapping_ambiguous",
                    "graph_fact_current_support_unproved",
                    "graph_embedding_identity_changed_requires_rebuild",
                }:
                    raise
                dependencies = await _mark_dependents_for_rebuild(
                    factory, adm, operation, mapping, token, rows, remaining, {("fact", fact_id)}
                )
                fact_actions.append(
                    ExactFactRecoveryAction(
                        fact_id,
                        state.source_node_id,
                        state.target_node_id,
                        expected,
                        "delete_for_rebuild",
                        rebuild_mapping_ids=dependencies,
                    )
                )
    return fact_actions
