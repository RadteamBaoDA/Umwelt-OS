"""Owner-state locking, owner callbacks and graph execution context for temporal graph operations.

Private implementation of ``modules.knowledge.temporal.worker``; import from that facade.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast, overload
from uuid import UUID

from pydantic import TypeAdapter
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.model_gateway.client import ModelGateway, PrivacyPolicyDenied
from core.model_gateway.schemas import ModelMapping, RequestPolicy
from modules.knowledge.documents import public as documents
from modules.knowledge.entities import public as entities
from modules.knowledge.temporal import public
from modules.knowledge.temporal._worker_actions import (
    _recovery_authorization,
    _validate_fact_replacement,
)
from modules.knowledge.temporal._worker_admission import (
    _Admission,
    _admit,
    _authorize_scheduled_rebuild,
    _commit,
    _get,
    _lease,
)
from modules.knowledge.temporal._worker_journal import (
    _bindings,
    _inventory,
    _receipt_inventory,
    _record,
)
from modules.knowledge.temporal.adapter import (
    CanonicalEntityBinding,
    CanonicalNodeRecoveryAction,
    DispatchOwnership,
    EvidenceIdentity,
    ExactFactRecoveryAction,
    GraphModelPolicy,
    GraphOperationError,
    GraphWriteReceipt,
    OperationAuthorization,
    RecoveryReceiptAggregate,
)
from modules.knowledge.temporal.models import (
    GraphDispatch,
    GraphMapping,
    GraphOperation,
    GraphRebuildDependency,
    GraphReceipt,
    GraphSupport,
)
from modules.settings import public as settings_public
from modules.sources import public as sources


@dataclass(slots=True)
class _OwnerCallbacks:
    """Owner-side callbacks handed to the graph adapter for one admitted invocation.

    Holds what the callbacks previously captured by closure (admission, session, mapping,
    inventory, bindings, policy snapshot). Module-level collaborators (``_lease``, ``_admit``,
    ``_get`` ...) are looked up at call time, so tests and the DB layer keep patching them on
    the worker module. ``active_dispatch`` is the one mutable field: it tracks the unfinished
    journaled dispatch between ``dispatch`` and ``dispatch_completed``.
    """

    factory: async_sessionmaker[AsyncSession]
    adm: _Admission
    session: AsyncSession
    operation: GraphOperation
    mapping: GraphMapping
    token: UUID
    cleanup: bool
    rows: list[GraphMapping]
    live_support: list[GraphSupport]
    source: Any
    bindings: tuple[CanonicalEntityBinding, ...]
    evidence: tuple[EvidenceIdentity, ...]
    settings: Settings
    redis: Redis
    config: Any
    snapshot: str
    bound: dict[str, Any]
    policy: RequestPolicy
    active_dispatch: DispatchOwnership | None = None

    async def validate_partition(self, mode: str, episode_id: str | None) -> None:
        """Recheck full owner inventory and current generation; only identifier cleanup may survive source deletion."""
        adm, mapping, source = self.adm, self.mapping, self.source
        await _lease(self.factory, adm, self.operation.id, self.token)
        current_rows, _current_support = await _inventory(self.session, mapping)
        if {(row.id, row.desired_revision, row.tombstoned) for row in current_rows} != {
            (row.id, row.desired_revision, row.tombstoned) for row in self.rows
        }:
            raise GraphOperationError("graph_canonical_dependency_changed")
        if mode in {"upsert", "search"}:
            if (
                mapping.tombstoned
                or source is None
                or source.status != "active"
                or source.generation != mapping.source_generation
            ):
                raise GraphOperationError("graph_source_generation_terminal")
            if any(
                row.status in {"reconcile_needed", "failed"}
                for row in current_rows
                if row.id != mapping.id
            ):
                raise GraphOperationError("graph_partition_requires_recovery")
        if mode != "delete" and self.live_support:
            await documents.read_evidence_refs(
                self.session,
                [(item.document_version_id, item.chunk_id) for item in self.live_support],
                scope=adm.scope,
                multi_workspace_enabled=adm.multi,
            )

    async def authorize(self, capability: str) -> None:
        """Recheck nonsecret gateway configuration and every exact permitted support immediately before inference."""
        if self.cleanup:
            raise PrivacyPolicyDenied("Identifier cleanup cannot invoke inference")
        await self.validate_partition("upsert", str(self.mapping.episode_id))
        async with self.factory() as check:
            await _admit(check, self.adm)
            current = settings_public.derive_for_role(
                await settings_public.get_ai_execution_config(
                    check, self.settings, self.redis, scope=self.adm.scope
                ),
                "background",
            )
        fingerprint = public.digest(
            [
                current.configuration_revision,
                current.gateway_identity,
                current.defaults.model_dump(mode="json"),
                current.privacy.model_dump(mode="json"),
                current.endpoint_destination_id,
            ]
        )
        if fingerprint != self.snapshot:
            raise PrivacyPolicyDenied("Graph policy changed before egress")

    async def canonicalize(self, raw: Any) -> list[Any]:
        """Publish only facts with exact current canonical endpoint bindings and retained support; never name-match candidates."""
        from modules.knowledge.temporal.adapter import TemporalSearchResult

        endpoint = {item.graph_entity_uuid: item for item in self.bindings}
        result = []
        for edge in raw:
            left, right = (
                endpoint.get(str(edge.source_node_uuid)),
                endpoint.get(str(edge.target_node_uuid)),
            )
            if left is None or right is None:
                continue
            allowed = set(edge.episodes)
            selected = [
                row for row in self.rows if str(row.episode_id) in allowed and not row.tombstoned
            ]
            if len(selected) != len(allowed) or any(
                row.status != "synchronized" for row in selected
            ):
                continue
            refs = tuple(
                item
                for item in self.evidence
                if item.document_version_id in {row.document_version_id for row in selected}
            )
            if not refs:
                continue
            result.append(
                TemporalSearchResult(
                    fact=edge.fact,
                    valid_from=edge.valid_at,
                    valid_to=edge.invalid_at,
                    observed_at=min(row.created_at for row in selected),
                    episode_ids=tuple(sorted(allowed)),
                    entity_ids=(left.canonical_entity_id, right.canonical_entity_id),
                    evidence=refs,
                )
            )
        return result

    async def fence_check(self) -> None:
        """Before every gateway send: original fence equality plus the live partition/intent lease."""
        await _lease(self.factory, self.adm, self.operation.id, self.token)

    @asynccontextmanager
    async def fence(self) -> AsyncIterator[None]:
        """Reuse the caller's held source/document transaction; no nested owner-lock acquisition during recovery."""
        await _lease(self.factory, self.adm, self.operation.id, self.token)
        yield

    async def record(self, receipt: GraphWriteReceipt) -> None:
        """Commit one prewrite/readback receipt; propagated absence must retain its exact durable owner proof chain."""
        if receipt.rebuild_absence_observed is not None:
            identifiers = (
                receipt.entity_ids
                if receipt.rebuild_absence_observed == "node"
                else receipt.fact_ids
            )
            if len(identifiers) != 1:
                raise GraphOperationError("graph_rebuild_absence_marker_invalid")
            await self.authorize_rebuild_absence(receipt.rebuild_absence_observed, identifiers[0])
        await _record(self.factory, self.adm, self.operation.id, self.token, receipt)

    async def dispatch(self, ownership: DispatchOwnership) -> None:
        """Persist the exact dispatch token (nonce + client id) before any graph transaction, rejecting reassignment during ambiguity."""
        adm, mapping = self.adm, self.mapping
        await _lease(self.factory, adm, self.operation.id, self.token)
        async with self.factory() as journal:
            await _admit(journal, adm)
            current = await _get(journal, GraphOperation, self.operation.id, adm, lock=True)
            if current is None:
                raise RuntimeError("invariant violated: current must not be None")
            if ownership.operation_id != current.id or ownership.group_id != str(
                mapping.partition_id
            ):
                raise GraphOperationError("graph_dispatch_not_owned")
            uncertain = await journal.scalar(
                select(GraphDispatch.id)
                .where(
                    GraphDispatch.workspace_id == adm.scope.workspace_id,
                    GraphDispatch.operation_id == current.id,
                    GraphDispatch.completed_at.is_(None),
                    GraphDispatch.cessation_verified_at.is_(None),
                )
                .limit(1)
            )
            if uncertain is not None:
                raise GraphOperationError("graph_previous_dispatch_not_ceased")
            journal.add(
                GraphDispatch(
                    workspace_id=adm.scope.workspace_id,
                    operation_id=current.id,
                    group_id=ownership.group_id,
                    server_run_id=ownership.server_run_id,
                    client_id=ownership.client_id,
                    lease_owner=self.token,
                )
            )
            current.dispatch_server_run_id, current.dispatch_client_id = (
                ownership.server_run_id,
                ownership.client_id,
            )
            current.dispatched_at = datetime.now(UTC)
            current.dispatch_deadline = datetime.now(UTC) + timedelta(seconds=120)
            current.cessation_verified_at, current.cessation_reason = None, None
            await _commit(journal, adm)
            self.active_dispatch = ownership

    async def dispatch_completed(self, ownership: DispatchOwnership) -> None:
        """Record exact synchronous reply completion after transport closure; never infer canonical synchronization."""
        adm = self.adm
        await _lease(self.factory, adm, self.operation.id, self.token)
        async with self.factory() as journal:
            await _admit(journal, adm)
            row = await journal.scalar(
                select(GraphDispatch)
                .where(
                    GraphDispatch.workspace_id == adm.scope.workspace_id,
                    GraphDispatch.operation_id == ownership.operation_id,
                    GraphDispatch.group_id == ownership.group_id,
                    GraphDispatch.server_run_id == ownership.server_run_id,
                    GraphDispatch.client_id == ownership.client_id,
                )
                .with_for_update()
            )
            if row is None or row.lease_owner != self.token:
                raise GraphOperationError("graph_dispatch_completion_not_owned")
            row.completed_at = datetime.now(UTC)
            await _commit(journal, adm)
            self.active_dispatch = None

    async def authorize_cessation(self, ownership: DispatchOwnership) -> None:
        """Authorize terminating exactly the journal-owned tagged transactions after exclusive local worker takeover; never accept arbitrary tokens."""
        adm = self.adm
        await _lease(self.factory, adm, self.operation.id, self.token)
        async with self.factory() as check:
            await _admit(check, adm)
            row = await check.scalar(
                select(GraphDispatch).where(
                    GraphDispatch.workspace_id == adm.scope.workspace_id,
                    GraphDispatch.operation_id == ownership.operation_id,
                    GraphDispatch.group_id == ownership.group_id,
                    GraphDispatch.server_run_id == ownership.server_run_id,
                    GraphDispatch.client_id == ownership.client_id,
                )
            )
            if (
                ownership.operation_id != self.operation.id
                or ownership.group_id != str(self.mapping.partition_id)
                or row is None
            ):
                raise GraphOperationError("graph_cessation_not_owned")

    async def _authorize_node_rebuild(self, action: CanonicalNodeRecoveryAction) -> None:
        """Prove a typed node rebuild deletion names exactly the dependent mappings and schedule them durably."""
        rows, mapping = self.rows, self.mapping
        episodes = {
            episode for link in action.expected_incident_links for episode in link.episode_ids
        }
        episodes.update(
            link.source_node_id
            for link in action.expected_incident_links
            if link.relationship_type == "MENTIONS"
        )
        expected_mappings = {
            row.id
            for row in rows
            if str(row.episode_id) in episodes and row.id != mapping.id and not row.tombstoned
        }
        if (
            set(action.rebuild_mapping_ids) != expected_mappings
            or not episodes <= {str(row.episode_id) for row in rows}
            or any(
                link.relationship_type != "MENTIONS" and not link.episode_ids
                for link in action.expected_incident_links
            )
        ):
            raise GraphOperationError("graph_node_rebuild_support_unproved")
        await _authorize_scheduled_rebuild(
            self.factory,
            self.adm,
            self.operation,
            mapping,
            action.rebuild_mapping_ids,
            {("node", action.graph_entity_uuid)}
            | {
                ("fact" if link.relationship_type != "MENTIONS" else "mention", link.edge_id)
                for link in action.expected_incident_links
            },
        )

    def _authorize_node_action(
        self,
        action: CanonicalNodeRecoveryAction,
        current: CanonicalEntityBinding | None,
        receipts: tuple[GraphWriteReceipt, ...],
    ) -> None:
        """Check one non-rebuild node decision against the current canonical seed and the journaled intent."""
        mapping = self.mapping
        if action.action == "replace_from_current_support":
            if current is None or action.replacement_binding != current:
                raise GraphOperationError("graph_node_current_seed_unproved")
        elif action.action == "retain":
            if current is None or mapping.tombstoned:
                raise GraphOperationError("graph_node_retained_fields_unproved")
            intended = {
                value
                for receipt in receipts
                for identifier, value in receipt.intended_entity_state_fingerprints
                if identifier == action.graph_entity_uuid
            }
            if action.expected_current_state_fingerprint not in intended:
                raise GraphOperationError("graph_node_retained_fields_unproved")
        elif current is not None or any(
            (
                link.relationship_type == "MENTIONS"
                and link.source_node_id != str(mapping.episode_id)
            )
            or (
                link.relationship_type != "MENTIONS"
                and (not link.episode_ids or set(link.episode_ids) != {str(mapping.episode_id)})
            )
            for link in action.expected_incident_links
        ):
            raise GraphOperationError("graph_node_independent_support")

    async def node_authorization(
        self,
        receipts: tuple[GraphWriteReceipt, ...],
        actions: tuple[CanonicalNodeRecoveryAction, ...],
    ) -> None:
        """Prove current canonical seeds/manual preservation and exact owned aggregate before every node decision."""
        adm, mapping = self.adm, self.mapping
        await _recovery_authorization(
            self.factory, adm, self.operation, mapping, self.token, receipts, self.active_dispatch
        )
        current_bindings = (
            tuple(
                replace(binding, mapping_revision=self.operation.desired_revision)
                for binding in await _bindings(
                    self.session,
                    mapping,
                    self.live_support,
                    scope=adm.scope,
                    multi_workspace_enabled=adm.multi,
                )
            )
            if self.live_support
            else ()
        )
        current_by_id = {item.graph_entity_uuid: item for item in current_bindings}
        for action in actions:
            if action.action == "delete_for_rebuild":
                await self._authorize_node_rebuild(action)
            else:
                self._authorize_node_action(
                    action, current_by_id.get(action.graph_entity_uuid), receipts
                )

    async def fact_authorization(
        self, receipts: tuple[GraphWriteReceipt, ...], actions: tuple[ExactFactRecoveryAction, ...]
    ) -> None:
        """Require current exact surviving mapping support and canonical replacement proof, never certify old shared fields by subtraction."""
        adm, mapping, rows = self.adm, self.mapping, self.rows
        await _recovery_authorization(
            self.factory, adm, self.operation, mapping, self.token, receipts, self.active_dispatch
        )
        latest = {
            state.fact_id: state for receipt in receipts for state in receipt.intended_fact_support
        }
        for action in actions:
            support = latest.get(action.fact_id)
            remaining = set(support.episode_ids) - {str(mapping.episode_id)} if support else set()
            if action.action == "delete_for_rebuild":
                historical_support = {
                    episode
                    for receipt in receipts
                    for state in receipt.intended_fact_support
                    if state.fact_id == action.fact_id
                    for episode in state.episode_ids
                } - {str(mapping.episode_id)}
                expected_mappings = {
                    row.id
                    for row in rows
                    if str(row.episode_id) in historical_support and not row.tombstoned
                }
                if set(action.rebuild_mapping_ids) != expected_mappings:
                    raise GraphOperationError("graph_fact_rebuild_support_unproved")
                await _authorize_scheduled_rebuild(
                    self.factory,
                    adm,
                    self.operation,
                    mapping,
                    action.rebuild_mapping_ids,
                    {("fact", action.fact_id)},
                )
            elif action.action == "delete_unsupported":
                if remaining:
                    terminal = {
                        str(row.episode_id)
                        for row in rows
                        if row.tombstoned
                        and row.external_state == "absent"
                        and row.applied_revision == row.desired_revision
                        and row.applied_digest == row.desired_digest
                        and row.error_code is None
                    }
                    if not remaining <= terminal:
                        raise GraphOperationError("graph_fact_surviving_support")
                    await self.authorize_rebuild_absence("fact", action.fact_id)
            elif action.replacement is None or set(action.replacement.episode_ids) != remaining:
                raise GraphOperationError("graph_fact_current_support_unproved")
            else:
                await _validate_fact_replacement(
                    self.session, adm, mapping, self.bindings, rows, action
                )

    async def authorize_aggregate(
        self,
        aggregate: RecoveryReceiptAggregate,
        receipts: tuple[GraphWriteReceipt, ...],
        mode: str,
    ) -> None:
        """Certify a complete immutable ledger prefix and actual witnesses without waiving graph state comparisons."""
        adm, operation, mapping = self.adm, self.operation, self.mapping
        await _lease(self.factory, adm, operation.id, self.token)
        async with self.factory() as check:
            await _admit(check, adm)
            expected_receipts, expected_aggregate = await _receipt_inventory(
                check,
                operation.id,
                aggregate.ledger_count,
                adm=adm,
                witness_sequences=aggregate.witness_sequences,
            )
        if (
            aggregate != expected_aggregate
            or receipts != expected_receipts
            or aggregate.operation_id != operation.id
            or aggregate.lease_token != operation.receipt_token
            or aggregate.group_id != str(mapping.partition_id)
            or aggregate.mapping_revision != operation.desired_revision
        ):
            raise GraphOperationError("graph_receipt_aggregate_not_owned")

    async def _journal_proves_absence(
        self, check: AsyncSession, source_id: UUID, kind: str, effect_id: str
    ) -> bool:
        """Page one operation's receipt ledger for a destructive cleanup receipt proving the exact node/fact deletion."""
        adm, mapping = self.adm, self.mapping
        sequence = 0
        while True:
            receipts = (
                await check.scalars(
                    select(GraphReceipt)
                    .where(
                        GraphReceipt.workspace_id == adm.scope.workspace_id,
                        GraphReceipt.operation_id == source_id,
                        GraphReceipt.sequence >= sequence,
                    )
                    .order_by(GraphReceipt.sequence)
                    .limit(100)
                )
            ).all()
            for row in receipts:
                receipt = TypeAdapter(GraphWriteReceipt).validate_python(row.payload)
                if (
                    receipt.phase != "cleanup_write_intent"
                    or receipt.group_id != str(mapping.partition_id)
                    or receipt.rebuild_absence_observed is not None
                ):
                    continue
                existed_nodes = {
                    identifier
                    for identifier, existed, fingerprint in receipt.prior_entity_state_fingerprints
                    if existed and fingerprint is not None
                }
                deleted_nodes = {
                    identifier
                    for identifier, fingerprint in receipt.intended_entity_state_fingerprints
                    if fingerprint is None and identifier in existed_nodes
                }
                if (
                    kind == "node"
                    and effect_id in deleted_nodes
                    or kind == "fact"
                    and (
                        any(
                            state.fact_id == effect_id and not state.episode_ids
                            for state in receipt.intended_fact_support
                        )
                        and any(
                            state.fact_id == effect_id and state.existed
                            for state in receipt.existing_fact_states
                        )
                        or deleted_nodes
                        and any(link.edge_id == effect_id for link in receipt.incident_links)
                    )
                ):
                    return True
            if not receipts or len(receipts) < 100:
                return False
            sequence = receipts[-1].sequence + 1

    async def _source_has_settled_dispatches(self, check: AsyncSession, source_id: UUID) -> bool:
        """Report whether the source operation has no unfinished dispatch other than this worker's own active one."""
        adm, operation = self.adm, self.operation
        unresolved = (
            await check.scalars(
                select(GraphDispatch)
                .where(
                    GraphDispatch.workspace_id == adm.scope.workspace_id,
                    GraphDispatch.operation_id == source_id,
                    GraphDispatch.completed_at.is_(None),
                    GraphDispatch.cessation_verified_at.is_(None),
                )
                .limit(2)
            )
        ).all()
        active = self.active_dispatch
        return not any(
            source_id != operation.id
            or active is None
            or row.lease_owner != self.token
            or row.server_run_id != active.server_run_id
            or row.client_id != active.client_id
            for row in unresolved
        )

    async def authorize_rebuild_absence(self, kind: str, effect_id: str) -> None:
        """Consume an original destructive journal through a durable dependency only after exact graph absence is observed."""
        adm, operation, mapping = self.adm, self.operation, self.mapping
        await _lease(self.factory, adm, operation.id, self.token)
        async with self.factory() as check:
            await _admit(check, adm)
            # Same-operation node cleanup can delete an incident fact before its
            # separate fact pass. It retains its own receipt identity throughout.
            source_ids = [operation.id]
            cursor = None
            while True:
                query = select(GraphRebuildDependency).where(
                    GraphRebuildDependency.workspace_id == adm.scope.workspace_id,
                    GraphRebuildDependency.mapping_id == mapping.id,
                    GraphRebuildDependency.source_generation == mapping.source_generation,
                )
                if cursor is not None:
                    query = query.where(GraphRebuildDependency.operation_id > cursor)
                dependencies = (
                    await check.scalars(
                        query.order_by(GraphRebuildDependency.operation_id).limit(100)
                    )
                ).all()
                source_ids.extend(
                    row.operation_id for row in dependencies if [kind, effect_id] in row.effect_ids
                )
                for source_id in source_ids:
                    source_operation = await _get(check, GraphOperation, source_id, adm)
                    if (
                        source_operation is None
                        or source_operation.partition_id != mapping.partition_id
                    ):
                        continue
                    if source_id != operation.id and source_operation.cleanup_completed_at is None:
                        continue
                    if not await self._source_has_settled_dispatches(check, source_id):
                        continue
                    if await self._journal_proves_absence(check, source_id, kind, effect_id):
                        return
                # Own journal is visited once; later iterations retain only
                # this bounded dependency page, never the lifetime source list.
                source_ids = []
                if len(dependencies) < 100:
                    break
                cursor = dependencies[-1].operation_id
        raise GraphOperationError("graph_rebuild_absence_proof_missing")

    @overload
    def model_policy(self, role: str) -> GraphModelPolicy: ...

    @overload
    def model_policy(self, role: str, *, optional: Literal[True]) -> GraphModelPolicy | None: ...

    def model_policy(self, role: str, *, optional: bool = False) -> GraphModelPolicy | None:
        """Resolve owner-configured roles with current privacy policy; absent required roles block egress."""
        mapping = self.mapping
        selected = self.config.defaults.resolve(role)
        if selected is None:
            if optional:
                return None
            if self.cleanup:
                # Empty ModelMapping is the gateway's actual disabled model
                # contract; authorize rejects inference for this cleanup scope.
                dimensions = (
                    mapping.embedding_identity.get("dimensions") if role == "embedding" else None
                )
                return GraphModelPolicy(
                    role, ModelMapping(), RequestPolicy(**self.bound), dimensions=dimensions
                )
            raise GraphOperationError("graph_model_role_unconfigured")
        dimensions = (
            (
                mapping.embedding_identity.get("dimensions")
                or selected.dimensions
                or self.settings.graph_embedding_dimensions
            )
            if role == "embedding"
            else None
        )
        return GraphModelPolicy(role, selected, self.policy, dimensions=dimensions)


async def _lock_owner_state(
    session: AsyncSession,
    adm: _Admission,
    operation: GraphOperation,
    mapping: GraphMapping,
    *,
    cleanup: bool,
) -> tuple[list[GraphMapping], list[GraphSupport], Any, tuple[CanonicalEntityBinding, ...]]:
    """Lock source, documents and canonical entities in order and return (rows, live support, source, bindings).

    Lock order is source, sorted documents, then sorted canonical entities; bindings are read
    before the entity locks to learn which to lock and again under them so field support cannot
    race an owner edit. Raises ``GraphOperationError`` for a terminal source generation (unless
    ``cleanup``) or support that no longer belongs to the mapping's source.
    """
    rows, supports = await _inventory(session, mapping)
    source = await sources.lock_source(
        session,
        mapping.source_id,
        scope=adm.scope,
        multi_workspace_enabled=adm.multi,
        expected_access_fence=adm.fence,
    )
    documents_ids = sorted({row.document_id for row in rows if not row.tombstoned}, key=str)
    await documents.lock_document_ids(
        session,
        documents_ids,
        scope=adm.scope,
        multi_workspace_enabled=adm.multi,
    )
    live_support = [item for item in supports if not item.removed]
    if not cleanup and (
        source is None
        or source.status != "active"
        or source.generation != mapping.source_generation
    ):
        raise GraphOperationError("graph_source_generation_terminal")
    if live_support:
        refs = await documents.read_evidence_refs(
            session,
            [(item.document_version_id, item.chunk_id) for item in live_support],
            scope=adm.scope,
            multi_workspace_enabled=adm.multi,
        )
        if {item.source_id for item in refs} != {mapping.source_id}:
            raise GraphOperationError("graph_support_source_changed")

    async def read_bindings() -> tuple[CanonicalEntityBinding, ...]:
        """Read current bindings at the revision being recovered (mapping_revision identifies the journal)."""
        # Current canonical revisions remain current; mapping_revision identifies
        # the journal being recovered, which may predate a newer queued correction.
        return (
            tuple(
                replace(binding, mapping_revision=operation.desired_revision)
                for binding in await _bindings(
                    session,
                    mapping,
                    live_support,
                    scope=adm.scope,
                    multi_workspace_enabled=adm.multi,
                )
            )
            if live_support
            else ()
        )

    bindings = await read_bindings()
    await entities.lock_entity_ids(
        session,
        sorted({binding.canonical_entity_id for binding in bindings}, key=str),
        scope=adm.scope,
        multi_workspace_enabled=adm.multi,
    )
    # Field support is re-read under canonical locks; owner edits cannot race
    # a source-proved seed between authorization and the external mutation.
    return rows, live_support, source, await read_bindings()


async def _record_embedding_identity(
    factory: async_sessionmaker[AsyncSession],
    adm: _Admission,
    mapping: GraphMapping,
    rows: list[GraphMapping],
    embedding: Any,
    settings: Settings,
) -> None:
    """Persist the embedding identity on the mapping, refusing a changed identity for already-written rows.

    Slot dimensions (from the embeddings probe) win; the env var is a legacy fallback.
    Commits in its own admitted short session.
    """
    dims = embedding.dimensions or settings.graph_embedding_dimensions if embedding else None
    if embedding is None or dims is None:
        raise GraphOperationError("graph_embedding_identity_unconfigured")
    identity = {"model": embedding.model, "version": embedding.version, "dimensions": dims}
    if any(
        row.external_state != "absent"
        and row.embedding_identity
        and row.embedding_identity != identity
        for row in rows
    ):
        raise GraphOperationError("graph_embedding_identity_changed_requires_rebuild")
    async with factory() as identity_session:
        await _admit(identity_session, adm)
        current = await _get(identity_session, GraphMapping, mapping.id, adm, lock=True)
        if current is None:
            raise RuntimeError("invariant violated: current must not be None")
        current.embedding_identity = identity
        await _commit(identity_session, adm)
    mapping.embedding_identity = identity


async def _context(
    factory: async_sessionmaker[AsyncSession],
    adm: _Admission,
    session: AsyncSession,
    ctx: dict[str, object],
    operation: GraphOperation,
    mapping: GraphMapping,
    token: UUID,
    *,
    cleanup: bool,
) -> tuple[OperationAuthorization, ModelGateway, list[GraphMapping]]:
    """Build real callbacks from owner contracts under sorted source/document fences; cleanup permits detached IDs only."""
    await _admit(session, adm)
    rows, live_support, source, bindings = await _lock_owner_state(
        session, adm, operation, mapping, cleanup=cleanup
    )
    evidence = tuple(
        EvidenceIdentity(
            item.source_id,
            item.source_generation,
            item.document_id,
            item.document_version_id,
            item.chunk_id,
        )
        for item in live_support
    )
    settings = cast(Settings, ctx["settings"])
    redis = cast(Redis, ctx["redis"])
    # ponytail: the graph pipeline shares one gateway, so every role resolves through the background provider;
    # split per-role gateways if graph roles ever need different providers.
    config = settings_public.derive_for_role(
        await settings_public.get_ai_execution_config(session, settings, redis, scope=adm.scope),
        "background",
    )
    embedding = config.defaults.resolve("embedding")
    if not cleanup:
        await _record_embedding_identity(factory, adm, mapping, rows, embedding, settings)
    snapshot = public.digest(
        [
            config.configuration_revision,
            config.gateway_identity,
            config.defaults.model_dump(mode="json"),
            config.privacy.model_dump(mode="json"),
            config.endpoint_destination_id,
        ]
    )
    # Policy and gateway are bound to the admitted job identity and the config revision read above.
    bound: dict[str, Any] = {
        "workspace_id": adm.scope.workspace_id,
        "actor_user_id": adm.scope.actor_user_id,
        "membership_revision": adm.scope.membership_revision,
        "gateway_identity": config.gateway_identity,
        "configuration_revision": config.configuration_revision,
    }
    policy = RequestPolicy(
        reasoning_allowed=config.privacy.allow_remote_reasoning,
        embeddings_allowed=config.privacy.allow_remote_embeddings,
        local_only=source.local_only if source else True,
        permitted_destinations=frozenset({config.endpoint_destination_id})
        if config.endpoint_destination_id
        else frozenset(),
        reasoning_destinations=frozenset(config.privacy.reasoning_destinations),
        embedding_destinations=frozenset(config.privacy.embedding_destinations),
        **bound,
    )
    owner = _OwnerCallbacks(
        factory=factory,
        adm=adm,
        session=session,
        operation=operation,
        mapping=mapping,
        token=token,
        cleanup=cleanup,
        rows=rows,
        live_support=live_support,
        source=source,
        bindings=bindings,
        evidence=evidence,
        settings=settings,
        redis=redis,
        config=config,
        snapshot=snapshot,
        bound=bound,
        policy=policy,
    )
    graph_episode_ids = tuple(
        str(row.episode_id)
        for row in rows
        if row.external_state != "absent" or row.id == mapping.id
    )
    history = tuple(
        str(row.episode_id) for row in rows if row.status == "synchronized" and not row.tombstoned
    )[:10]
    receipt_history, receipt_aggregate = await _receipt_inventory(session, operation.id, adm=adm)
    context = OperationAuthorization(
        group_id=str(mapping.partition_id),
        source_id=mapping.source_id,
        source_generation=mapping.source_generation,
        operation_id=operation.id,
        lease_token=operation.receipt_token,
        evidence=evidence,
        fence=owner.fence,
        authorize=owner.authorize,
        canonicalize=owner.canonicalize,
        validate_partition=owner.validate_partition,
        record_write_intent=owner.record,
        reasoning=owner.model_policy("background"),
        small_reasoning=owner.model_policy("background"),
        embedding=owner.model_policy("embedding"),
        reranking=owner.model_policy("reranker", optional=True),
        authorize_node_recovery=owner.node_authorization,
        authorize_fact_recovery=owner.fact_authorization,
        canonical_bindings=bindings,
        mapping_revision=operation.desired_revision,
        partition_episode_ids=graph_episode_ids,
        history_episode_ids=history,
        partition_inventory_complete=True,
        receipt_history=receipt_history,
        receipt_aggregate=receipt_aggregate,
        authorize_receipt_aggregate=owner.authorize_aggregate,
        record_dispatch_ownership=owner.dispatch,
        record_dispatch_completion=owner.dispatch_completed,
        authorize_dispatch_cessation=owner.authorize_cessation,
        authorize_rebuild_absence=owner.authorize_rebuild_absence,
    )
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
        before_send=owner.fence_check,
        approved_endpoint_cidrs=config.endpoint_allowed_cidrs,
        allow_public_endpoints=config.allow_public_providers,
        kind=config.gateway_kind,
    )
    return context, gateway, rows
