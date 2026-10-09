"""Durable temporal execution with full owner fences, ordered receipts and exclusive graph dispatch."""

import asyncio
import hashlib
import json
import math
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast, overload
from uuid import UUID, uuid4, uuid5

from arq import Retry
from arq.connections import ArqRedis
from fastapi import HTTPException
from pydantic import TypeAdapter
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.heavy_work import (
    HEAVY_RETRY_DEFER_SECONDS,
    HeavyLeaseLost,
    HeavyWorkBusy,
    RemoteHeavyWorkBlocked,
    heavy_job_slot,
)
from core.job_denial import admit_retry_stale, denial_code, terminalize
from core.model_gateway.client import ModelGateway, PrivacyPolicyDenied
from core.model_gateway.schemas import ModelMapping, RequestPolicy
from core.realtime import commit_with_replay
from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope
from modules.knowledge.documents import public as documents
from modules.knowledge.entities import public as entities
from modules.knowledge.relationships import public as relationships
from modules.knowledge.temporal import public
from modules.knowledge.temporal.adapter import (
    CanonicalEntityBinding,
    CanonicalNodeRecoveryAction,
    DispatchOwnership,
    EpisodeRequest,
    EvidenceIdentity,
    ExactFactRecoveryAction,
    ExactFactReplacement,
    ExactFactState,
    ExactFactSupport,
    GraphConfiguration,
    GraphModelPolicy,
    GraphOperationError,
    GraphOperationUnknown,
    GraphReceiptInspection,
    GraphState,
    GraphWriteReceipt,
    OperationAuthorization,
    RecoveryReceiptAggregate,
    TemporalGraph,
)
from modules.knowledge.temporal.models import (
    GraphDispatch,
    GraphMapping,
    GraphOperation,
    GraphPartition,
    GraphRebuildDependency,
    GraphReceipt,
    GraphReconcileRun,
    GraphSupport,
)
from modules.settings import public as settings_public
from modules.sources import public as sources
from modules.timeline import public as timeline

LEASE_SECONDS = 180
OWNER_BUDGET_SECONDS = 150
RECOVERY_WORKSPACE_PAGE = 25
RECOVERY_CURSOR_KEY = "temporal:graph-recovery:workspace-cursor"


@dataclass(frozen=True, slots=True)
class _Admission:
    """ One invocation's original authority, carried through every short transaction and callback.

    The scope and fence are captured once by admit_workspace before any claim or domain lock.
    Every later session re-reads the fence without locking and requires exact equality; a
    revoked or changed original aborts that subject (no ACK, no rebase onto fresh authority).
    """

    scope: InternalJobScope
    multi: bool
    fence: AccessFence


async def _admit(session: AsyncSession, adm: _Admission) -> None:
    """ Compare current workspace authority with the original invocation fence (nonlocking, 409 on drift).

    Nonlocking on purpose: the outer job transaction may already hold the admission locks
    (Sources.lock_source) while nested journal sessions run; a locking call here would
    wait on its own invocation.
    """
    current = await workspaces.read_access_fence(session, scope=adm.scope, multi_workspace_enabled=adm.multi)
    if current != adm.fence:
        raise HTTPException(status_code=409, detail="Workspace authority changed")


async def _commit(session: AsyncSession, adm: _Admission, drafts: list[Any] | tuple[Any, ...] = ()) -> None:
    """ Commit domain rows (and optional replay drafts) only if the original fence still holds."""
    await commit_with_replay(session, drafts, scope=adm.scope, multi_workspace_enabled=adm.multi,
                             access_fence=adm.fence)


async def _get(session: AsyncSession, model: Any, identity: UUID | int, adm: _Admission, *, lock: bool = False) -> Any:
    """ Read one temporal root by ID inside the admitted workspace; a foreign ID is simply absent."""
    query = select(model).where(model.id == identity, model.workspace_id == adm.scope.workspace_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return await session.scalar(query)


async def admit_workspace(
    factory: async_sessionmaker[AsyncSession], settings: Settings, workspace_id: UUID,
    denial: list[str] | None = None,
) -> _Admission | None:
    """ Resolve the workspace's durable owner, lock admission FIRST and capture one original fence.

    No other lock is taken before this fence. Returns None (skip, no mutation) when the owner
    lineage, account/membership admission or the per-workspace knowledge.temporal module gate denies.
    A typed denial (409 only after one re-resolve) appends its terminal code to ``denial`` so a
    caller owning a durable row can end it. The locks are released before any claim, graph or
    model I/O; later effects only compare.
    """
    multi = bool(settings.multi_workspace_enabled)

    async def attempt() -> _Admission | None:
        async with factory() as session:
            try:
                owner = await workspaces.resolve_workspace_owner_context(
                    session, workspace_id, multi_workspace_enabled=multi,
                )
                if owner is None:
                    return None
                scope = InternalJobScope(
                    workspace_id=workspace_id, actor_user_id=owner.user_id,
                    membership_revision=owner.membership_revision,
                )
                fence = await workspaces.authorize_internal_job(session, scope=scope, multi_workspace_enabled=multi)
                enabled = await settings_public.module_is_enabled(
                    session, "knowledge.temporal", scope=scope, multi_workspace_enabled=multi,
                )
            finally:
                await session.rollback()
        return _Admission(scope, multi, fence) if enabled else None

    try:
        return await admit_retry_stale(attempt)
    except HTTPException as exc:
        code = denial_code(exc)
        if code is None:
            raise
        if denial is not None:
            denial.append(code)
        return None


async def _admit_job(
    factory: async_sessionmaker[AsyncSession], settings: Settings, operation_id: UUID,
    denial: list[str] | None = None,
) -> _Admission | None:
    """ Admit one queued operation: validate its durable workspace/partition/mapping lineage, then the fence."""
    async with factory() as session:
        row = (await session.execute(select(
            GraphOperation.workspace_id, GraphOperation.mapping_id, GraphOperation.partition_id,
        ).where(GraphOperation.id == operation_id))).one_or_none()
        lineage = await session.scalar(select(GraphMapping.id).join(
            GraphPartition, GraphPartition.id == GraphMapping.partition_id,
        ).where(
            GraphMapping.id == row.mapping_id, GraphMapping.partition_id == row.partition_id,
            GraphMapping.workspace_id == row.workspace_id, GraphPartition.workspace_id == row.workspace_id,
        )) if row is not None else None
        await session.rollback()
    if row is None or lineage is None:
        return None
    adm = await admit_workspace(factory, settings, row.workspace_id, denial)
    if adm is None and denial:
        # Only never-dispatched, unleased work is ended; dispatched rows keep unknown-outcome recovery.
        await terminalize(
            factory, GraphOperation, operation_id, row.workspace_id, denial[0],
            from_status=("pending", "blocked"),
            extra_where=(GraphOperation.dispatched_at.is_(None), GraphOperation.lease_owner.is_(None)),
        )
    return adm


async def _dependency_fingerprint(session: AsyncSession, ctx: dict[str, object], graph_state: GraphState,
                                    adm: _Admission, partition_id: UUID | None = None) -> str:
    """Hash nonsecret policy/model and graph configuration so blocked work retries only after an actual dependency change."""
    settings = cast(Settings, ctx["settings"])
    config = await settings_public.get_ai_execution_config(
        session, settings, cast(Redis, ctx["redis"]), scope=adm.scope,
    )
    owner_state = []
    if partition_id is not None:
        rows = (await session.scalars(select(GraphMapping).where(
            GraphMapping.workspace_id == adm.scope.workspace_id, GraphMapping.partition_id == partition_id,
        )
            .order_by(GraphMapping.id).limit(101))).all()
        owner_state = [[str(row.id), row.desired_revision, row.desired_digest, row.tombstoned,
            row.external_state, row.embedding_identity] for row in rows]
        partition = await _get(session, GraphPartition, partition_id, adm)
        source = await sources.get_source_fence(
            session, partition.source_id, scope=adm.scope,
            multi_workspace_enabled=adm.multi,
        ) if partition is not None else None
        owner_state.append([source.status, source.generation, source.local_only] if source else ["source_unavailable"])
    return public.digest([str(graph_state), owner_state, settings.graph_enabled, settings.graph_host, settings.graph_port,
        settings.graph_database, settings.graph_embedding_dimensions,
        config.configuration_revision, config.gateway_identity,
        {alias: value.model_dump(mode="json") for alias, value in config.aliases.items()},
        config.privacy.model_dump(mode="json"), config.endpoint_destination_id])


async def _mark_dependents_for_rebuild(factory: async_sessionmaker[AsyncSession], adm: _Admission, operation: GraphOperation,
                                       mapping: GraphMapping, token: UUID, rows: list[GraphMapping],
                                       episode_ids: set[str], effects: set[tuple[str, str]]) -> tuple[UUID, ...]:
    """Commit every exact surviving projection dependency before destructive candidate/shared cleanup.

    The complete bounded bucket converts graph episode IDs into owner mapping
    IDs. A durable link preserves the original deletion journal identity, while
    a fresh same-revision upsert waits behind the partition uncertainty fence.
    Empty/foreign support or effects over100 fails instead of partial scheduling.
    """
    survivors = episode_ids - {str(mapping.episode_id)}
    selected = {str(row.episode_id): row.id for row in rows if not row.tombstoned}
    if not survivors or not survivors <= selected.keys() or len(effects) > 100:
        raise GraphOperationError("graph_rebuild_dependency_closure_unproved")
    mapping_ids = tuple(sorted({selected[episode_id] for episode_id in survivors}, key=str))
    async with factory() as journal:
        await _admit(journal, adm)
        partition = await _get(journal, GraphPartition, mapping.partition_id, adm, lock=True)
        current_operation = await _get(journal, GraphOperation, operation.id, adm, lock=True)
        assert current_operation is not None
        assert partition is not None
        if partition.lease_token != token or current_operation.lease_owner != token:
            raise GraphOperationError("graph_rebuild_dependency_lease_lost")
        drafts = []
        for mapping_id in mapping_ids:
            dependent = await _get(journal, GraphMapping, mapping_id, adm, lock=True)
            if dependent is None or dependent.partition_id != mapping.partition_id or dependent.tombstoned:
                raise GraphOperationError("graph_rebuild_dependency_changed")
            dependency = await journal.scalar(select(GraphRebuildDependency).where(
                GraphRebuildDependency.workspace_id == adm.scope.workspace_id,
                GraphRebuildDependency.operation_id == operation.id,
                GraphRebuildDependency.mapping_id == mapping_id).with_for_update())
            if dependency is None:
                journal.add(GraphRebuildDependency(workspace_id=adm.scope.workspace_id,
                    operation_id=operation.id, mapping_id=mapping_id,
                    source_generation=mapping.source_generation, effect_ids=[list(item) for item in sorted(effects)]))
                # An explicit new attempt owns no copied receipts. Its ancestor
                # cleanup will consume the original destructive proof below.
                journal.add(GraphOperation(workspace_id=adm.scope.workspace_id, mapping_id=mapping_id,
                    partition_id=mapping.partition_id, kind="upsert",
                    desired_revision=dependent.desired_revision, desired_digest=dependent.desired_digest,
                    next_attempt_at=datetime.now(UTC), replacement_created_at=datetime.now(UTC)))
            else:
                dependency.effect_ids = [list(item) for item in sorted({tuple(item) for item in dependency.effect_ids} | effects)]
                if len(dependency.effect_ids) > 100:
                    raise GraphOperationError("graph_rebuild_dependency_effects_over_bound")
            dependent.status, dependent.external_state = "pending", "unknown"
            dependent.error_code = "graph_dependent_rebuild_pending"
            drafts.append(await public.graph_status_change(journal, mapping_id, scope=adm.scope))
        await _commit(journal, adm, drafts)
    return mapping_ids


async def _authorize_scheduled_rebuild(factory: async_sessionmaker[AsyncSession], adm: _Admission, operation: GraphOperation,
                                       mapping: GraphMapping, mapping_ids: tuple[UUID, ...],
                                       effects: set[tuple[str, str]]) -> None:
    """Require committed original-operation links for every affected owner mapping and exact destructive effect."""
    if not mapping_ids:
        raise GraphOperationError("graph_rebuild_dependencies_missing")
    async with factory() as check:
        await _admit(check, adm)
        dependencies = (await check.scalars(select(GraphRebuildDependency).where(
            GraphRebuildDependency.workspace_id == adm.scope.workspace_id,
            GraphRebuildDependency.operation_id == operation.id,
            GraphRebuildDependency.mapping_id.in_(mapping_ids)))).all()
        if {row.mapping_id for row in dependencies} != set(mapping_ids) or any(
                row.source_generation != mapping.source_generation or not effects <= {tuple(item) for item in row.effect_ids}
                for row in dependencies):
            raise GraphOperationError("graph_rebuild_dependencies_uncommitted")


async def _claim(factory: async_sessionmaker[AsyncSession], adm: _Admission, operation_id: UUID) -> UUID | None:
    """Claim partition then intent in one short transaction; expired uncertain writers never become new extraction."""
    async with factory() as session:
        await _admit(session, adm)
        locator = await _get(session, GraphOperation, operation_id, adm)
        if locator is None:
            return None
        partition = await _get(session, GraphPartition, locator.partition_id, adm, lock=True)
        operation = await _get(session, GraphOperation, operation_id, adm, lock=True)
        now = datetime.now(UTC)
        assert operation is not None
        if operation.status in {"succeeded", "failed"} or operation.next_attempt_at > now:
            return None
        assert partition is not None
        if partition.lease_expires_at and partition.lease_expires_at > now:
            return None
        if partition.uncertain_operation_id and partition.uncertain_operation_id != operation.id:
            return None
        desired = await _get(session, GraphMapping, operation.mapping_id, adm, lock=True)
        assert desired is not None
        if (desired.desired_revision != operation.desired_revision or desired.desired_digest != operation.desired_digest) and operation.dispatched_at is None:
            assert operation is not None
            operation.status, operation.error_code = "succeeded", "superseded_before_dispatch"
            await _commit(session, adm)
            return None
        if operation.attempts >= 5 and not partition.uncertain_operation_id:
            assert operation is not None
            operation.status, operation.error_code = "failed", "attempts_exhausted"
            mapping = await _get(session, GraphMapping, operation.mapping_id, adm, lock=True)
            assert mapping is not None
            if mapping.desired_revision == operation.desired_revision and mapping.desired_digest == operation.desired_digest:
                assert mapping is not None
                mapping.status, mapping.error_code = "failed", "attempts_exhausted"
            await _commit(session, adm, [await public.graph_status_change(session, mapping.id, scope=adm.scope)])
            return None
        token = uuid4()
        partition.lease_token, partition.lease_expires_at = token, now + timedelta(seconds=LEASE_SECONDS)
        operation.lease_owner, operation.lease_expires_at = token, partition.lease_expires_at
        assert operation is not None
        operation.status = "running"
        operation.attempts += 1
        await _commit(session, adm)
        return token


async def _lease(factory: async_sessionmaker[AsyncSession], adm: _Admission, operation_id: UUID, token: UUID) -> None:
    """Recheck live exact partition/intent ownership; stale results cannot publish or authorize writes."""
    async with factory() as session:
        await _admit(session, adm)
        operation = await _get(session, GraphOperation, operation_id, adm)
        partition = await _get(session, GraphPartition, operation.partition_id, adm) if operation else None
        now = datetime.now(UTC)
        if (operation is None or partition is None or operation.lease_owner != token
                or partition.lease_token != token or operation.lease_expires_at is None
                or operation.lease_expires_at <= now
                or partition.lease_expires_at is None or partition.lease_expires_at <= now):
            raise GraphOperationError("graph_partition_lease_lost")


async def _receipts(session: AsyncSession, operation_id: UUID, adm: _Admission) -> tuple[GraphWriteReceipt, ...]:
    """Return actual immutable witnesses covering every distinct journal obligation; never truncate effects."""
    receipts, _ = await _receipt_inventory(session, operation_id, adm=adm)
    return receipts


async def _receipt_inventory(session: AsyncSession, operation_id: UUID,
                             prefix_count: int | None = None, *, adm: _Admission,
                             inspection: GraphReceiptInspection | None = None,
                             witness_sequences: tuple[int, ...] = ()) -> tuple[tuple[GraphWriteReceipt, ...], RecoveryReceiptAggregate | None]:
    """Page all original receipts and select bounded first/latest/current-matching witnesses per physical effect.

    Lifetime mutations are scanned without materializing their payloads. An
    original intermediate receipt is retained when its fingerprint matches the
    current exact readback; first prior and latest intended receipts also remain.
    The prefix digest binds every sequence/payload, including unselected history.
    Caller-supplied extra witness sequences are reread as originals and compared
    by the adapter against actual state; they cannot forge historical fingerprints.
    """
    adapter = TypeAdapter(GraphWriteReceipt)
    cursor, digest = 0, hashlib.sha256()
    first: dict[str, tuple[int, GraphWriteReceipt]] = {}
    latest: dict[str, tuple[int, GraphWriteReceipt]] = {}
    matching: dict[str, tuple[int, GraphWriteReceipt]] = {}
    obligations: dict[str, tuple[int, GraphWriteReceipt]] = {}
    extras: dict[int, GraphWriteReceipt] = {}
    current_nodes = dict(inspection.current_entity_state_fingerprints) if inspection else {}
    current_facts = {item.fact_id: item.state_fingerprint for item in inspection.current_fact_states} if inspection else {}
    effects: list[set[str]] = [set(), set(), set(), set()]
    while prefix_count is None or cursor < prefix_count:
        query = select(GraphReceipt).where(
            GraphReceipt.workspace_id == adm.scope.workspace_id,
            GraphReceipt.operation_id == operation_id, GraphReceipt.sequence >= cursor)
        if prefix_count is not None:
            query = query.where(GraphReceipt.sequence < prefix_count)
        rows = (await session.scalars(query.order_by(GraphReceipt.sequence).limit(100))).all()
        if not rows:
            break
        for row in rows:
            if row.sequence != cursor:
                raise GraphOperationError("graph_receipt_inventory_invalid")
            encoded = json.dumps(row.payload, sort_keys=True, separators=(",", ":"))
            digest.update(f"{cursor}:".encode() + encoded.encode() + b"\n")
            receipt = adapter.validate_python(row.payload)
            entry = (cursor, receipt)
            keys = {"phase:" + receipt.phase}
            keys.update("effect:" + identifier for identifier in (*receipt.entity_ids, *receipt.mention_ids,
                *receipt.fact_ids, *(link.edge_id for link in receipt.incident_links)))
            for key in keys:
                first.setdefault(key, entry)
                latest[key] = entry
            if cursor in witness_sequences:
                extras[cursor] = receipt
            for identifier, value in receipt.intended_entity_state_fingerprints:
                if value is not None and current_nodes.get(identifier) == value:
                    matching["effect:" + identifier] = entry
            for identifier, present, value in receipt.prior_entity_state_fingerprints:
                if present and value is not None and current_nodes.get(identifier) == value:
                    matching["effect:" + identifier] = entry
            states: tuple[ExactFactState | ExactFactSupport, ...] = (
                *receipt.existing_fact_states, *receipt.intended_fact_support)
            for state in states:
                if current_facts.get(state.fact_id) == state.state_fingerprint:
                    matching["effect:" + state.fact_id] = entry
            if receipt.phase == "cleanup_write_intent":
                prior_states = {state.fact_id: state for state in receipt.existing_fact_states}
                for intended in receipt.intended_fact_support:
                    prior = prior_states.get(intended.fact_id)
                    if prior is None or not prior.existed:
                        continue
                    target = str(receipt.episode_id)
                    if target in prior.episode_ids and target not in intended.episode_ids and intended.episode_ids:
                        obligations.setdefault("shared:" + intended.fact_id, entry)
                    if (target not in prior.episode_ids and target not in intended.episode_ids
                            and (prior.valid_at, prior.invalid_at, prior.expired_at)
                            != (intended.valid_at, intended.invalid_at, intended.expired_at)):
                        obligations.setdefault("stale:" + intended.fact_id, entry)
            if inspection and inspection.episode_state_fingerprint in {
                    receipt.episode_state_fingerprint, receipt.prior_episode_state_fingerprint}:
                matching["episode"] = entry
            if inspection:
                for link in receipt.incident_links:
                    if link in inspection.incident_links:
                        matching["effect:" + link.edge_id] = entry
            for destination, values in zip(effects, (receipt.entity_ids, receipt.mention_ids,
                    receipt.fact_ids, tuple(link.edge_id for link in receipt.incident_links)), strict=True):
                destination.update(values)
            if len(set().union(*effects)) > 100:
                raise GraphOperationError("graph_recovery_effect_inventory_over_bound")
            cursor += 1
    if prefix_count is not None and cursor != prefix_count:
        raise GraphOperationError("graph_receipt_prefix_missing")
    selected = dict(extras)
    selected.update({sequence: receipt for sequence, receipt in (*first.values(), *latest.values(), *matching.values(), *obligations.values())})
    entries = tuple(sorted(selected.items()))
    if len(entries) > 512 or len(extras) != len(witness_sequences):
        raise GraphOperationError("graph_recovery_witness_inventory_invalid")
    if not entries:
        return (), None
    head = entries[0][1]
    aggregate = RecoveryReceiptAggregate(operation_id=head.operation_id, group_id=head.group_id,
        lease_token=head.lease_token, mapping_revision=head.mapping_revision, ledger_count=cursor,
        ledger_digest=digest.hexdigest(), witness_sequences=tuple(sequence for sequence, _ in entries),
        entity_ids=tuple(sorted(effects[0])), mention_ids=tuple(sorted(effects[1])),
        fact_ids=tuple(sorted(effects[2])), incident_link_ids=tuple(sorted(effects[3])))
    return tuple(receipt for _, receipt in entries), aggregate


async def _record(factory: async_sessionmaker[AsyncSession], adm: _Admission, operation_id: UUID, token: UUID,
                  receipt: GraphWriteReceipt) -> None:
    """Commit every no-text prewrite receipt independently while source/document locks remain held by its caller."""
    await _lease(factory, adm, operation_id, token)
    async with factory() as session:
        await _admit(session, adm)
        operation = await _get(session, GraphOperation, operation_id, adm, lock=True)
        assert operation is not None
        mapping = await _get(session, GraphMapping, operation.mapping_id, adm)
        assert mapping is not None
        if (receipt.operation_id != operation.id or receipt.lease_token != operation.receipt_token
                or receipt.mapping_revision != operation.desired_revision
                or receipt.episode_id != mapping.episode_id or receipt.group_id != str(mapping.partition_id)):
            raise GraphOperationError("graph_receipt_not_owned")
        sequence = (await session.scalar(select(func.max(GraphReceipt.sequence)).where(
            GraphReceipt.workspace_id == adm.scope.workspace_id, GraphReceipt.operation_id == operation.id)))
        # Adapter strips all seed/candidate text from receipts; reject accidental leakage
        # instead of relying on a subsequent purge to remove copied narrative fields.
        if any(binding.node_name is not None or binding.node_summary is not None for binding in receipt.canonical_bindings):
            raise GraphOperationError("graph_receipt_contains_seed_text")
        payload = TypeAdapter(GraphWriteReceipt).dump_python(receipt, mode="json")
        session.add(GraphReceipt(workspace_id=adm.scope.workspace_id, operation_id=operation.id, sequence=0 if sequence is None else sequence + 1, payload=payload))
        operation.phase = receipt.phase
        mapping.external_state = "unknown"
        await _commit(session, adm)


async def _inventory(session: AsyncSession, mapping: GraphMapping) -> tuple[list[GraphMapping], list[GraphSupport]]:
    """Read every reservation/support in a selected bounded bucket, including historical/unknown rows."""
    rows = list((await session.scalars(select(GraphMapping).where(
                                       GraphMapping.workspace_id == mapping.workspace_id,
                                       GraphMapping.partition_id == mapping.partition_id)
                                      .order_by(GraphMapping.id).limit(101))).all())
    if len(rows) > 100 or len({row.document_version_id for row in rows}) > 100:
        raise GraphOperationError("graph_partition_over_bound")
    supports = list((await session.scalars(select(GraphSupport).where(
                                           GraphSupport.workspace_id == mapping.workspace_id,
                                           GraphSupport.mapping_id.in_([row.id for row in rows]))
                                          .order_by(GraphSupport.mapping_id, GraphSupport.chunk_id).limit(101))).all())
    if len(supports) > 100:
        raise GraphOperationError("graph_partition_support_over_bound")
    return rows, supports


async def _bindings(
    session: AsyncSession, mapping: GraphMapping, supports: list[GraphSupport], *,
    scope: Scope, multi_workspace_enabled: bool,
) -> tuple[CanonicalEntityBinding, ...]:
    """Resolve exact owner memberships/seeds, assigning stable graph UUIDs only to proven canonical identities."""
    memberships = []
    by_version: dict[UUID, list[UUID]] = {}
    for support in supports:
        if not support.removed:
            by_version.setdefault(support.document_version_id, []).append(support.chunk_id)
    for version_id, chunk_ids in sorted(by_version.items(), key=lambda item: str(item[0])):
        memberships.extend(await entities.list_retained_version_membership_refs(
            session, version_id, chunk_ids, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        ))
    if len(memberships) > 100:
        raise GraphOperationError("graph_membership_closure_over_bound")
    seeds = await entities.get_temporal_node_seeds(
        session, [item.membership_id for item in memberships], scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    ) if memberships else ()
    bound_ids = {seed.entity_id for seed in seeds}
    if bound_ids != {item.entity_id for item in memberships}:
        raise GraphOperationError("graph_canonical_seed_unavailable")
    result = []
    support_by_ref = {(row.document_version_id, row.chunk_id): row for row in supports if not row.removed}
    for seed in seeds:
        evidence = []
        for member in seed.memberships:
            row = support_by_ref[(member.document_version_id, member.chunk_id)]
            evidence.append(EvidenceIdentity(row.source_id, row.source_generation, row.document_id,
                                             row.document_version_id, row.chunk_id))
        fields = [("name", seed.name_hash, tuple(seed.name_support_membership_ids))]
        if seed.summary_hash is not None:
            fields.append(("description", seed.summary_hash, tuple(seed.summary_support_membership_ids)))
        result.append(CanonicalEntityBinding(
            canonical_entity_id=seed.entity_id, canonical_revision=seed.revision,
            membership_ids=tuple(member.id for member in seed.memberships), evidence=tuple(sorted(set(evidence), key=str)),
            graph_entity_uuid=str(uuid5(mapping.partition_id, str(seed.entity_id))), group_id=str(mapping.partition_id),
            mapping_revision=mapping.desired_revision, node_name=seed.name, node_summary=seed.summary,
            field_support=tuple(fields),
        ))
    return tuple(result)


async def _context(factory: async_sessionmaker[AsyncSession], adm: _Admission, session: AsyncSession, ctx: dict[str, object],
                   operation: GraphOperation, mapping: GraphMapping, token: UUID,
                   *, cleanup: bool) -> tuple[OperationAuthorization, ModelGateway, list[GraphMapping]]:
    """Build real callbacks from owner contracts under sorted source/document fences; cleanup permits detached IDs only."""
    await _admit(session, adm)
    rows, supports = await _inventory(session, mapping)
    source = await sources.lock_source(
        session, mapping.source_id, scope=adm.scope,
        multi_workspace_enabled=adm.multi, expected_access_fence=adm.fence,
    )
    documents_ids = sorted({row.document_id for row in rows if not row.tombstoned}, key=str)
    await documents.lock_document_ids(
        session, documents_ids, scope=adm.scope, multi_workspace_enabled=adm.multi,
    )
    live_support = [item for item in supports if not item.removed]
    if not cleanup and (source is None or source.status != "active" or source.generation != mapping.source_generation):
        raise GraphOperationError("graph_source_generation_terminal")
    if live_support:
        refs = await documents.read_evidence_refs(
            session, [(item.document_version_id, item.chunk_id) for item in live_support],
            scope=adm.scope, multi_workspace_enabled=adm.multi,
        )
        if {item.source_id for item in refs} != {mapping.source_id}:
            raise GraphOperationError("graph_support_source_changed")
    # Current canonical revisions remain current; mapping_revision identifies
    # the journal being recovered, which may predate a newer queued correction.
    bindings = tuple(replace(binding, mapping_revision=operation.desired_revision)
        for binding in await _bindings(session, mapping, live_support, scope=adm.scope,
                                       multi_workspace_enabled=adm.multi)) if live_support else ()
    await entities.lock_entity_ids(
        session, sorted({binding.canonical_entity_id for binding in bindings}, key=str),
        scope=adm.scope, multi_workspace_enabled=adm.multi,
    )
    # Field support is re-read under canonical locks; owner edits cannot race
    # a source-proved seed between authorization and the external mutation.
    bindings = tuple(replace(binding, mapping_revision=operation.desired_revision)
        for binding in await _bindings(session, mapping, live_support, scope=adm.scope,
                                       multi_workspace_enabled=adm.multi)) if live_support else ()
    evidence = tuple(EvidenceIdentity(item.source_id, item.source_generation, item.document_id,
                                      item.document_version_id, item.chunk_id) for item in live_support)
    settings = cast(Settings, ctx["settings"])
    redis = cast(Redis, ctx["redis"])
    config = await settings_public.get_ai_execution_config(session, settings, redis, scope=adm.scope)
    embedding = config.aliases.get("embedding")
    if not cleanup:
        if embedding is None or settings.graph_embedding_dimensions is None:
            raise GraphOperationError("graph_embedding_identity_unconfigured")
        identity = {"model": embedding.model, "version": embedding.version, "dimensions": settings.graph_embedding_dimensions}
        if any(row.external_state != "absent" and row.embedding_identity and row.embedding_identity != identity for row in rows):
            raise GraphOperationError("graph_embedding_identity_changed_requires_rebuild")
        async with factory() as identity_session:
            await _admit(identity_session, adm)
            current = await _get(identity_session, GraphMapping, mapping.id, adm, lock=True)
            assert current is not None
            current.embedding_identity = identity
            await _commit(identity_session, adm)
        mapping.embedding_identity = identity
    snapshot = public.digest([config.configuration_revision, config.gateway_identity,
                              {alias: value.model_dump(mode="json") for alias, value in config.aliases.items()},
                              config.privacy.model_dump(mode="json"), config.endpoint_destination_id])
    # Policy and gateway are bound to the admitted job identity and the config revision read above.
    bound: dict[str, Any] = {
        "workspace_id": adm.scope.workspace_id, "actor_user_id": adm.scope.actor_user_id,
        "membership_revision": adm.scope.membership_revision, "gateway_identity": config.gateway_identity,
        "configuration_revision": config.configuration_revision,
    }
    policy = RequestPolicy(reasoning_allowed=config.privacy.allow_remote_reasoning,
                           embeddings_allowed=config.privacy.allow_remote_embeddings,
                           local_only=source.local_only if source else True,
                           permitted_destinations=frozenset({config.endpoint_destination_id}) if config.endpoint_destination_id else frozenset(),
                           reasoning_destinations=frozenset(config.privacy.reasoning_destinations),
                           embedding_destinations=frozenset(config.privacy.embedding_destinations),
                           **bound)

    async def validate_partition(mode: str, episode_id: str | None) -> None:
        """Recheck full owner inventory and current generation; only identifier cleanup may survive source deletion."""
        await _lease(factory, adm, operation.id, token)
        current_rows, _current_support = await _inventory(session, mapping)
        if {(row.id, row.desired_revision, row.tombstoned) for row in current_rows} != {
            (row.id, row.desired_revision, row.tombstoned) for row in rows
        }:
            raise GraphOperationError("graph_canonical_dependency_changed")
        if mode in {"upsert", "search"}:
            if mapping.tombstoned or source is None or source.status != "active" or source.generation != mapping.source_generation:
                raise GraphOperationError("graph_source_generation_terminal")
            if any(row.status in {"reconcile_needed", "failed"} for row in current_rows if row.id != mapping.id):
                raise GraphOperationError("graph_partition_requires_recovery")
        if mode != "delete" and live_support:
            await documents.read_evidence_refs(
                session, [(item.document_version_id, item.chunk_id) for item in live_support],
                scope=adm.scope, multi_workspace_enabled=adm.multi,
            )

    async def authorize(capability: str) -> None:
        """Recheck nonsecret gateway configuration and every exact permitted support immediately before inference."""
        if cleanup:
            raise PrivacyPolicyDenied("Identifier cleanup cannot invoke inference")
        await validate_partition("upsert", str(mapping.episode_id))
        async with factory() as check:
            await _admit(check, adm)
            current = await settings_public.get_ai_execution_config(check, settings, redis, scope=adm.scope)
        fingerprint = public.digest([current.configuration_revision, current.gateway_identity,
                                     {alias: value.model_dump(mode="json") for alias, value in current.aliases.items()},
                                     current.privacy.model_dump(mode="json"), current.endpoint_destination_id])
        if fingerprint != snapshot:
            raise PrivacyPolicyDenied("Graph policy changed before egress")

    async def canonicalize(raw: Any) -> list[Any]:
        """Publish only facts with exact current canonical endpoint bindings and retained support; never name-match candidates."""
        from modules.knowledge.temporal.adapter import TemporalSearchResult
        endpoint = {item.graph_entity_uuid: item for item in bindings}
        result = []
        for edge in raw:
            left, right = endpoint.get(str(edge.source_node_uuid)), endpoint.get(str(edge.target_node_uuid))
            if left is None or right is None:
                continue
            allowed = set(edge.episodes)
            selected = [row for row in rows if str(row.episode_id) in allowed and not row.tombstoned]
            if len(selected) != len(allowed) or any(row.status != "synchronized" for row in selected):
                continue
            refs = tuple(item for item in evidence if item.document_version_id in {row.document_version_id for row in selected})
            if not refs:
                continue
            result.append(TemporalSearchResult(fact=edge.fact, valid_from=edge.valid_at, valid_to=edge.invalid_at,
                                               observed_at=min(row.created_at for row in selected),
                                               episode_ids=tuple(sorted(allowed)),
                                               entity_ids=(left.canonical_entity_id, right.canonical_entity_id), evidence=refs))
        return result

    async def fence_check() -> None:
        """ Before every gateway send: original fence equality plus the live partition/intent lease."""
        await _lease(factory, adm, operation.id, token)

    @asynccontextmanager
    async def fence() -> AsyncIterator[None]:
        """Reuse the caller's held source/document transaction; no nested owner-lock acquisition during recovery."""
        await _lease(factory, adm, operation.id, token)
        yield

    async def record(receipt: GraphWriteReceipt) -> None:
        """Commit one prewrite/readback receipt; propagated absence must retain its exact durable owner proof chain."""
        if receipt.rebuild_absence_observed is not None:
            identifiers = receipt.entity_ids if receipt.rebuild_absence_observed == "node" else receipt.fact_ids
            if len(identifiers) != 1:
                raise GraphOperationError("graph_rebuild_absence_marker_invalid")
            await authorize_rebuild_absence(receipt.rebuild_absence_observed, identifiers[0])
        await _record(factory, adm, operation.id, token, receipt)

    active_dispatch: DispatchOwnership | None = None

    async def dispatch(ownership: DispatchOwnership) -> None:
        """Persist exact dedicated server/client command ownership before any graph send, rejecting reassignment during ambiguity."""
        nonlocal active_dispatch
        await _lease(factory, adm, operation.id, token)
        async with factory() as journal:
            await _admit(journal, adm)
            current = await _get(journal, GraphOperation, operation.id, adm, lock=True)
            assert current is not None
            if ownership.operation_id != current.id or ownership.group_id != str(mapping.partition_id):
                raise GraphOperationError("graph_dispatch_not_owned")
            uncertain = await journal.scalar(select(GraphDispatch.id).where(
                GraphDispatch.workspace_id == adm.scope.workspace_id, GraphDispatch.operation_id == current.id, GraphDispatch.completed_at.is_(None),
                GraphDispatch.cessation_verified_at.is_(None)).limit(1))
            if uncertain is not None:
                raise GraphOperationError("graph_previous_dispatch_not_ceased")
            journal.add(GraphDispatch(workspace_id=adm.scope.workspace_id, operation_id=current.id, group_id=ownership.group_id,
                server_run_id=ownership.server_run_id, client_id=ownership.client_id, lease_owner=token))
            current.dispatch_server_run_id, current.dispatch_client_id = ownership.server_run_id, ownership.client_id
            current.dispatched_at = datetime.now(UTC)
            current.dispatch_deadline = datetime.now(UTC) + timedelta(seconds=120)
            current.cessation_verified_at, current.cessation_reason = None, None
            await _commit(journal, adm)
            active_dispatch = ownership

    async def dispatch_completed(ownership: DispatchOwnership) -> None:
        """Record exact synchronous reply completion after transport closure; never infer canonical synchronization."""
        nonlocal active_dispatch
        await _lease(factory, adm, operation.id, token)
        async with factory() as journal:
            await _admit(journal, adm)
            row = await journal.scalar(select(GraphDispatch).where(
                GraphDispatch.workspace_id == adm.scope.workspace_id,
                GraphDispatch.operation_id == ownership.operation_id,
                GraphDispatch.group_id == ownership.group_id,
                GraphDispatch.server_run_id == ownership.server_run_id,
                GraphDispatch.client_id == ownership.client_id).with_for_update())
            if row is None or row.lease_owner != token:
                raise GraphOperationError("graph_dispatch_completion_not_owned")
            row.completed_at = datetime.now(UTC)
            await _commit(journal, adm)
            active_dispatch = None

    async def authorize_cessation(ownership: DispatchOwnership) -> None:
        """Authorize exact journal-owned CLIENT KILL after exclusive local worker takeover; never accept arbitrary numeric IDs."""
        await _lease(factory, adm, operation.id, token)
        async with factory() as check:
            await _admit(check, adm)
            row = await check.scalar(select(GraphDispatch).where(
                GraphDispatch.workspace_id == adm.scope.workspace_id,
                GraphDispatch.operation_id == ownership.operation_id,
                GraphDispatch.group_id == ownership.group_id,
                GraphDispatch.server_run_id == ownership.server_run_id,
                GraphDispatch.client_id == ownership.client_id))
            if (ownership.operation_id != operation.id or ownership.group_id != str(mapping.partition_id)
                    or row is None):
                raise GraphOperationError("graph_cessation_not_owned")

    async def node_authorization(receipts: tuple[GraphWriteReceipt, ...], actions: tuple[CanonicalNodeRecoveryAction, ...]) -> None:
        """Prove current canonical seeds/manual preservation and exact owned aggregate before every node decision."""
        await _recovery_authorization(factory, adm, operation, mapping, token, receipts, active_dispatch)
        current_bindings = tuple(replace(binding, mapping_revision=operation.desired_revision)
            for binding in await _bindings(session, mapping, live_support, scope=adm.scope,
                                           multi_workspace_enabled=adm.multi)) if live_support else ()
        current_by_id = {item.graph_entity_uuid: item for item in current_bindings}
        for action in actions:
            current = current_by_id.get(action.graph_entity_uuid)
            if action.action == "delete_for_rebuild":
                episodes = {episode for link in action.expected_incident_links for episode in link.episode_ids}
                episodes.update(link.source_node_id for link in action.expected_incident_links if link.relationship_type == "MENTIONS")
                expected_mappings = {row.id for row in rows if str(row.episode_id) in episodes and row.id != mapping.id and not row.tombstoned}
                if (set(action.rebuild_mapping_ids) != expected_mappings or
                        not episodes <= {str(row.episode_id) for row in rows} or
                        any(link.relationship_type != "MENTIONS" and not link.episode_ids for link in action.expected_incident_links)):
                    raise GraphOperationError("graph_node_rebuild_support_unproved")
                await _authorize_scheduled_rebuild(factory, adm, operation, mapping, action.rebuild_mapping_ids,
                    {("node", action.graph_entity_uuid)} | {("fact" if link.relationship_type != "MENTIONS" else "mention", link.edge_id)
                        for link in action.expected_incident_links})
            elif action.action == "replace_from_current_support":
                if current is None or action.replacement_binding != current:
                    raise GraphOperationError("graph_node_current_seed_unproved")
            elif action.action == "retain":
                if current is None or mapping.tombstoned:
                    raise GraphOperationError("graph_node_retained_fields_unproved")
                intended = {value for receipt in receipts for identifier, value in receipt.intended_entity_state_fingerprints
                            if identifier == action.graph_entity_uuid}
                if action.expected_current_state_fingerprint not in intended:
                    raise GraphOperationError("graph_node_retained_fields_unproved")
            elif current is not None or any(
                    (link.relationship_type == "MENTIONS" and link.source_node_id != str(mapping.episode_id))
                    or (link.relationship_type != "MENTIONS" and (not link.episode_ids or
                        set(link.episode_ids) != {str(mapping.episode_id)})) for link in action.expected_incident_links):
                raise GraphOperationError("graph_node_independent_support")

    async def fact_authorization(receipts: tuple[GraphWriteReceipt, ...], actions: tuple[ExactFactRecoveryAction, ...]) -> None:
        """Require current exact surviving mapping support and canonical replacement proof, never certify old shared fields by subtraction."""
        await _recovery_authorization(factory, adm, operation, mapping, token, receipts, active_dispatch)
        latest = {state.fact_id: state for receipt in receipts for state in receipt.intended_fact_support}
        for action in actions:
            support = latest.get(action.fact_id)
            remaining = set(support.episode_ids) - {str(mapping.episode_id)} if support else set()
            if action.action == "delete_for_rebuild":
                historical_support = {episode for receipt in receipts for state in receipt.intended_fact_support
                    if state.fact_id == action.fact_id for episode in state.episode_ids} - {str(mapping.episode_id)}
                expected_mappings = {row.id for row in rows if str(row.episode_id) in historical_support and not row.tombstoned}
                if set(action.rebuild_mapping_ids) != expected_mappings:
                    raise GraphOperationError("graph_fact_rebuild_support_unproved")
                await _authorize_scheduled_rebuild(factory, adm, operation, mapping, action.rebuild_mapping_ids, {("fact", action.fact_id)})
            elif action.action == "delete_unsupported":
                if remaining:
                    terminal = {str(row.episode_id) for row in rows if row.tombstoned and row.external_state == "absent"
                        and row.applied_revision == row.desired_revision and row.applied_digest == row.desired_digest
                        and row.error_code is None}
                    if not remaining <= terminal:
                        raise GraphOperationError("graph_fact_surviving_support")
                    await authorize_rebuild_absence("fact", action.fact_id)
            elif action.replacement is None or set(action.replacement.episode_ids) != remaining:
                raise GraphOperationError("graph_fact_current_support_unproved")
            else:
                await _validate_fact_replacement(session, adm, mapping, bindings, rows, action)

    async def authorize_aggregate(aggregate: RecoveryReceiptAggregate,
                                  receipts: tuple[GraphWriteReceipt, ...], mode: str) -> None:
        """Certify a complete immutable ledger prefix and actual witnesses without waiving graph state comparisons."""
        await _lease(factory, adm, operation.id, token)
        async with factory() as check:
            await _admit(check, adm)
            expected_receipts, expected_aggregate = await _receipt_inventory(check, operation.id, aggregate.ledger_count, adm=adm,
                witness_sequences=aggregate.witness_sequences)
        if (aggregate != expected_aggregate or receipts != expected_receipts
                or aggregate.operation_id != operation.id or aggregate.lease_token != operation.receipt_token
                or aggregate.group_id != str(mapping.partition_id) or aggregate.mapping_revision != operation.desired_revision):
            raise GraphOperationError("graph_receipt_aggregate_not_owned")

    async def authorize_rebuild_absence(kind: str, effect_id: str) -> None:
        """Consume an original destructive journal through a durable dependency only after exact graph absence is observed."""
        await _lease(factory, adm, operation.id, token)
        async with factory() as check:
            await _admit(check, adm)
            # Same-operation node cleanup can delete an incident fact before its
            # separate fact pass. It retains its own receipt identity throughout.
            source_ids = [operation.id]
            cursor = None
            while True:
                query = select(GraphRebuildDependency).where(
                    GraphRebuildDependency.workspace_id == adm.scope.workspace_id,
                    GraphRebuildDependency.mapping_id == mapping.id,
                    GraphRebuildDependency.source_generation == mapping.source_generation)
                if cursor is not None:
                    query = query.where(GraphRebuildDependency.operation_id > cursor)
                dependencies = (await check.scalars(query.order_by(GraphRebuildDependency.operation_id).limit(100))).all()
                source_ids.extend(row.operation_id for row in dependencies if [kind, effect_id] in row.effect_ids)
                for source_id in source_ids:
                    source_operation = await _get(check, GraphOperation, source_id, adm)
                    if source_operation is None or source_operation.partition_id != mapping.partition_id:
                        continue
                    if source_id != operation.id and source_operation.cleanup_completed_at is None:
                        continue
                    unresolved = (await check.scalars(select(GraphDispatch).where(
                        GraphDispatch.workspace_id == adm.scope.workspace_id, GraphDispatch.operation_id == source_id,
                        GraphDispatch.completed_at.is_(None), GraphDispatch.cessation_verified_at.is_(None)).limit(2))).all()
                    if any(source_id != operation.id or active_dispatch is None or row.lease_owner != token
                            or row.server_run_id != active_dispatch.server_run_id or row.client_id != active_dispatch.client_id
                            for row in unresolved):
                        continue
                    sequence = 0
                    while True:
                        receipts = (await check.scalars(select(GraphReceipt).where(
                            GraphReceipt.workspace_id == adm.scope.workspace_id,
                            GraphReceipt.operation_id == source_id, GraphReceipt.sequence >= sequence).order_by(GraphReceipt.sequence).limit(100))).all()
                        for row in receipts:
                            receipt = TypeAdapter(GraphWriteReceipt).validate_python(row.payload)
                            if (receipt.phase != "cleanup_write_intent" or receipt.group_id != str(mapping.partition_id)
                                    or receipt.rebuild_absence_observed is not None):
                                continue
                            existed_nodes = {identifier for identifier, existed, fingerprint in receipt.prior_entity_state_fingerprints
                                if existed and fingerprint is not None}
                            deleted_nodes = {identifier for identifier, fingerprint in receipt.intended_entity_state_fingerprints
                                if fingerprint is None and identifier in existed_nodes}
                            if (kind == "node" and effect_id in deleted_nodes or kind == "fact" and (
                                    any(state.fact_id == effect_id and not state.episode_ids for state in receipt.intended_fact_support)
                                    and any(state.fact_id == effect_id and state.existed for state in receipt.existing_fact_states)
                                    or deleted_nodes and any(link.edge_id == effect_id for link in receipt.incident_links))):
                                return
                        if not receipts or len(receipts) < 100:
                            break
                        sequence = receipts[-1].sequence + 1
                # Own journal is visited once; later iterations retain only
                # this bounded dependency page, never the lifetime source list.
                source_ids = []
                if len(dependencies) < 100:
                    break
                cursor = dependencies[-1].operation_id
        raise GraphOperationError("graph_rebuild_absence_proof_missing")

    @overload
    def model_policy(alias: str) -> GraphModelPolicy: ...

    @overload
    def model_policy(alias: str, *, optional: Literal[True]) -> GraphModelPolicy | None: ...

    def model_policy(alias: str, *, optional: bool = False) -> GraphModelPolicy | None:
        """Resolve owner-configured aliases with current privacy policy; absent required aliases block egress."""
        selected = config.aliases.get(alias)
        if selected is None:
            if optional:
                return None
            if cleanup:
                # Empty ModelMapping is the gateway's actual disabled model
                # contract; authorize rejects inference for this cleanup scope.
                dimensions = mapping.embedding_identity.get("dimensions") if alias == "embedding" else None
                return GraphModelPolicy(alias, ModelMapping(), RequestPolicy(**bound), dimensions=dimensions)
            raise GraphOperationError("graph_model_alias_unconfigured")
        dimensions = (mapping.embedding_identity.get("dimensions") or settings.graph_embedding_dimensions) if alias == "embedding" else None
        return GraphModelPolicy(alias, selected, policy, dimensions=dimensions)

    graph_episode_ids = tuple(str(row.episode_id) for row in rows
                              if row.external_state != "absent" or row.id == mapping.id)
    history = tuple(str(row.episode_id) for row in rows if row.status == "synchronized" and not row.tombstoned)[:10]
    receipt_history, receipt_aggregate = await _receipt_inventory(session, operation.id, adm=adm)
    context = OperationAuthorization(
        group_id=str(mapping.partition_id), source_id=mapping.source_id,
        source_generation=mapping.source_generation, operation_id=operation.id, lease_token=operation.receipt_token,
        evidence=evidence, fence=fence, authorize=authorize, canonicalize=canonicalize,
        validate_partition=validate_partition, record_write_intent=record,
        reasoning=model_policy("reasoning-large"), small_reasoning=model_policy("reasoning-small"),
        embedding=model_policy("embedding"), reranking=model_policy("reranker", optional=True),
        authorize_node_recovery=node_authorization, authorize_fact_recovery=fact_authorization,
        canonical_bindings=bindings, mapping_revision=operation.desired_revision,
        partition_episode_ids=graph_episode_ids, history_episode_ids=history,
        partition_inventory_complete=True, receipt_history=receipt_history,
        receipt_aggregate=receipt_aggregate, authorize_receipt_aggregate=authorize_aggregate,
        record_dispatch_ownership=dispatch, record_dispatch_completion=dispatch_completed,
        authorize_dispatch_cessation=authorize_cessation,
        authorize_rebuild_absence=authorize_rebuild_absence,
    )
    gateway = ModelGateway(redis, config.omniroute_base_url, config.omniroute_api_key,
                           config.endpoint_destination_id or "", timeout_seconds=min(config.request_timeout_seconds, 30),
                           scope=adm.scope, gateway_identity=config.gateway_identity,
                           configuration_revision=config.configuration_revision, before_send=fence_check,
                           approved_endpoint_cidrs=config.endpoint_allowed_cidrs)
    return context, gateway, rows


async def _recovery_authorization(factory: async_sessionmaker[AsyncSession], adm: _Admission, operation: GraphOperation,
                                   mapping: GraphMapping, token: UUID, receipts: tuple[GraphWriteReceipt, ...],
                                   active_dispatch: DispatchOwnership | None = None) -> None:
    """Compare each supplied receipt with the complete committed ledger and require concrete prior dispatch cessation."""
    await _lease(factory, adm, operation.id, token)
    async with factory() as check:
        await _admit(check, adm)
        missing = list(receipts)
        sequence = 0
        while missing:
            page = (await check.scalars(select(GraphReceipt).where(
                GraphReceipt.workspace_id == adm.scope.workspace_id, GraphReceipt.operation_id == operation.id,
                GraphReceipt.sequence >= sequence).order_by(GraphReceipt.sequence).limit(100))).all()
            if not page:
                break
            originals = [TypeAdapter(GraphWriteReceipt).validate_python(row.payload) for row in page]
            missing = [receipt for receipt in missing if receipt not in originals]
            sequence = page[-1].sequence + 1
        unresolved = (await check.scalars(select(GraphDispatch).where(
            GraphDispatch.workspace_id == adm.scope.workspace_id, GraphDispatch.operation_id == operation.id,
            GraphDispatch.completed_at.is_(None), GraphDispatch.cessation_verified_at.is_(None)))).all()
        # Only the exact current scope may be live during its recovery callback;
        # an earlier ambiguous scope with the same task token still blocks.
        prior_uncertain = any(active_dispatch is None or row.lease_owner != token
            or row.server_run_id != active_dispatch.server_run_id or row.client_id != active_dispatch.client_id
            for row in unresolved)
        if (prior_uncertain or missing
                or any(receipt.operation_id != operation.id or receipt.lease_token != operation.receipt_token
                       or receipt.group_id != str(mapping.partition_id) for receipt in receipts)):
            raise GraphOperationError("graph_recovery_owner_proof_missing")


async def _validate_fact_replacement(session: AsyncSession, adm: _Admission, mapping: GraphMapping,
                                     bindings: tuple[CanonicalEntityBinding, ...], rows: list[GraphMapping],
                                     action: ExactFactRecoveryAction) -> None:
    """Authorize a fresh canonical relationship snapshot over exact mapped endpoints and survivor evidence."""
    endpoints = {binding.graph_entity_uuid: binding.canonical_entity_id for binding in bindings}
    left, right = endpoints.get(action.source_node_id), endpoints.get(action.target_node_id)
    if left is None or right is None:
        raise GraphOperationError("graph_candidate_fact_requires_current_owner_proof")
    snapshot = action.replacement
    assert snapshot is not None
    relation = await _current_relationship_snapshot(session, adm, left, right, snapshot.name)
    version_ids = {row.document_version_id for row in rows if str(row.episode_id) in snapshot.episode_ids and not row.tombstoned}
    if not relation or not relation.relationship.evidence or any(
        UUID(str(item["document_version_id"])) not in version_ids for item in relation.supports
    ):
        raise GraphOperationError("graph_fact_current_evidence_unproved")
    seeds = {binding.graph_entity_uuid: binding for binding in bindings}
    left_seed, right_seed = seeds[action.source_node_id], seeds[action.target_node_id]
    expected_text = f"{left_seed.node_name} {relation.relationship.type} {right_seed.node_name}"
    if (not left_seed.node_name or not right_seed.node_name or snapshot.fact != expected_text
            or snapshot.valid_at != relation.relationship.valid_from
            or snapshot.invalid_at != relation.relationship.valid_to or snapshot.expired_at is not None
            or dict(snapshot.attributes) != {"canonical_relationship_id": str(relation.relationship.id),
                "canonical_snapshot_digest": relation.digest}):
        raise GraphOperationError("graph_fact_current_snapshot_unproved")


async def _current_relationship_snapshot(session: AsyncSession, adm: _Admission, left: UUID, right: UUID, kind: str | None = None) -> relationships.RelationshipSnapshot | None:
    """Page owner relationships and lock the one unambiguous exact canonical endpoint match; no name inference."""
    cursor, found = None, None
    while True:
        page = await relationships.list_relationships(
            session, limit=100, cursor=cursor, entity_id=left,
            scope=adm.scope, multi_workspace_enabled=adm.multi,
        )
        for relation in page.items:
            if relation.source_entity_id == left and relation.target_entity_id == right and (kind is None or relation.type == kind):
                if found is not None:
                    raise GraphOperationError("graph_fact_canonical_mapping_ambiguous")
                found = relation.id
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    if found is None:
        raise GraphOperationError("graph_fact_canonical_mapping_unavailable")
    await relationships.lock_relationship_ids(
        session, [found], scope=adm.scope, multi_workspace_enabled=adm.multi,
    )
    return await relationships.get_relationship_snapshot(
        session, found, scope=adm.scope, multi_workspace_enabled=adm.multi,
    )


async def _fresh_fact_action(factory: async_sessionmaker[AsyncSession], adm: _Admission, session: AsyncSession,
                              ctx: dict[str, object], operation: GraphOperation, mapping: GraphMapping, token: UUID,
                              context: OperationAuthorization, rows: list[GraphMapping], fact_id: str,
                              source_node_id: str, target_node_id: str, expected: str,
                              survivors: set[str]) -> ExactFactRecoveryAction:
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
    relation = await _current_relationship_snapshot(session, adm, left.canonical_entity_id, right.canonical_entity_id)
    if relation is None or not relation.relationship.evidence:
        raise GraphOperationError("graph_fact_current_evidence_unproved")
    versions = {row.document_version_id for row in rows if str(row.episode_id) in survivors and not row.tombstoned}
    supported_versions = {UUID(str(item["document_version_id"])) for item in relation.supports}
    if len(versions) != len(survivors) or supported_versions != versions:
        raise GraphOperationError("graph_fact_current_support_unproved")
    value = f"{left.node_name} {relation.relationship.type} {right.node_name}"
    model = context.embedding
    dimensions = model.dimensions
    if not model.mapping.model or dimensions is None:
        raise GraphOperationError("graph_embedding_identity_unconfigured")
    identity = mapping.embedding_identity
    if identity and (identity.get("model") != model.mapping.model or identity.get("version") != model.mapping.version
            or identity.get("dimensions") != dimensions):
        raise GraphOperationError("graph_embedding_identity_changed_requires_rebuild")
    settings = cast(Settings, ctx["settings"])
    redis = cast(Redis, ctx["redis"])
    config = await settings_public.get_ai_execution_config(session, settings, redis, scope=adm.scope)
    async def before_send() -> None:
        """Fence a fresh surviving-fact embedding against policy/model drift and deleted canonical evidence."""
        await _lease(factory, adm, operation.id, token)
        current_source = await sources.get_source(
            session, mapping.source_id, scope=adm.scope, multi_workspace_enabled=adm.multi,
        )
        current_relation = await relationships.get_relationship_snapshot(
            session, relation.relationship.id, scope=adm.scope, multi_workspace_enabled=adm.multi,
        )
        async with factory() as check:
            await _admit(check, adm)
            current_config = await settings_public.get_ai_execution_config(check, settings, redis, scope=adm.scope)
        if (current_source is None or current_source.status != "active" or current_source.generation != mapping.source_generation
                or current_relation is None or current_relation.digest != relation.digest
                or current_config.configuration_revision != config.configuration_revision
                or current_config.aliases.get("embedding") != model.mapping):
            raise PrivacyPolicyDenied("Current surviving fact support or model policy changed")

    gateway = ModelGateway(redis, config.omniroute_base_url, config.omniroute_api_key,
        config.endpoint_destination_id or "", timeout_seconds=min(config.request_timeout_seconds, 30),
        scope=adm.scope, gateway_identity=config.gateway_identity,
        configuration_revision=config.configuration_revision, before_send=before_send, approved_endpoint_cidrs=config.endpoint_allowed_cidrs)

    raw = await gateway.embed(model.alias, model.mapping, model.policy, [value], before_send=before_send)
    data = raw.get("data") if isinstance(raw, dict) else None
    vector = data[0].get("embedding") if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict) and data[0].get("index") == 0 else None
    if (not isinstance(vector, list) or len(vector) != dimensions or any(isinstance(item, bool) or
            not isinstance(item, int | float) or not math.isfinite(item) for item in vector) or not any(vector)):
        raise GraphOperationError("graph_fact_embedding_invalid")
    replacement = ExactFactReplacement(fact_id=fact_id, group_id=context.group_id,
        source_node_id=source_node_id, target_node_id=target_node_id, name=relation.relationship.type,
        fact=value, episode_ids=tuple(sorted(survivors)), created_at=operation.replacement_created_at,
        reference_time=max(item.observed_at for item in relation.relationship.evidence),
        valid_at=relation.relationship.valid_from, invalid_at=relation.relationship.valid_to, expired_at=None,
        fact_embedding=tuple(float(item) for item in vector),
        attributes=(("canonical_relationship_id", str(relation.relationship.id)), ("canonical_snapshot_digest", relation.digest)))
    return ExactFactRecoveryAction(fact_id, source_node_id, target_node_id, expected,
        "replace_from_current_support", replacement)


async def _finish(factory: async_sessionmaker[AsyncSession], adm: _Admission, operation_id: UUID, token: UUID,
                   status: str, error_code: str | None, *, external_state: str | None = None,
                   retry_upsert: bool = False) -> None:
    """Finish only the current lease/revision; durable unknown blocks the partition until exact cessation/recovery."""
    async with factory() as session:
        await _admit(session, adm)
        locator = await _get(session, GraphOperation, operation_id, adm)
        if locator is None:
            return
        # Match claim order: partition precedes intent and mapping. Reversing
        # these rows would deadlock recovery against a concurrent claimant.
        partition = await _get(session, GraphPartition, locator.partition_id, adm, lock=True)
        operation = await _get(session, GraphOperation, operation_id, adm, lock=True)
        assert operation is not None
        if operation.lease_owner != token:
            return
        mapping = await _get(session, GraphMapping, operation.mapping_id, adm, lock=True)
        assert mapping is not None
        current_revision = mapping.desired_revision == operation.desired_revision and mapping.desired_digest == operation.desired_digest
        operation.status, operation.error_code = status, error_code
        operation.lease_owner, operation.lease_expires_at = None, None
        operation.next_attempt_at = datetime.now(UTC) + timedelta(seconds=min(300, 5 * 2 ** min(operation.attempts, 5)))
        assert partition is not None
        if partition.lease_token == token:
            assert partition is not None
            partition.lease_token, partition.lease_expires_at = None, None
        if status == "reconcile_needed":
            assert operation is not None
            assert partition is not None
            partition.uncertain_operation_id = operation.id
            if current_revision:
                assert mapping is not None
                mapping.status = "tombstoned" if mapping.tombstoned else "reconcile_needed"
        elif partition.uncertain_operation_id == operation.id:
            assert partition is not None
            partition.uncertain_operation_id = None
        if external_state and current_revision:
            assert mapping is not None
            mapping.external_state = external_state
        if status == "succeeded" and current_revision:
            assert mapping is not None
            assert operation is not None
            mapping.applied_revision, mapping.applied_digest = operation.desired_revision, operation.desired_digest
            mapping.status = "tombstoned" if mapping.tombstoned else "synchronized"
            mapping.applied_at = datetime.now(UTC)
        elif current_revision and status != "reconcile_needed" and not mapping.tombstoned:
            # An older intent may converge after a correction queued a newer
            # desired revision. Its success cannot certify that new revision.
            assert mapping is not None
            mapping.status = "pending" if status == "succeeded" else status
        if current_revision:
            assert mapping is not None
            mapping.error_code = error_code
        assert mapping is not None
        assert operation is not None
        if (retry_upsert and not mapping.tombstoned and mapping.desired_revision == operation.desired_revision
                and mapping.desired_digest == operation.desired_digest):
            # Cleanup completion and the replacement attempt publish together;
            # an absent projection is never briefly advertised synchronized.
            assert mapping is not None
            mapping.status, mapping.applied_revision, mapping.applied_digest = "pending", 0, None
            await public._queue(session, mapping, "upsert")
        await _commit(session, adm, [await public.graph_status_change(session, mapping.id, scope=adm.scope)])


async def _publish_clean_projection(factory: async_sessionmaker[AsyncSession], adm: _Admission, operation: GraphOperation,
                                     token: UUID) -> bool:
    """Publish proved absence for the current desired intent after every original mapping journal is cleaned.

    Caller owns partition and source/document fences and has checked its own
    exact dispatch cessation. This resets physical model identity availability
    without claiming canonical synchronization or releasing the worker lease.
    """
    async with factory() as session:
        await _admit(session, adm)
        partition = await _get(session, GraphPartition, operation.partition_id, adm, lock=True)
        current = await _get(session, GraphOperation, operation.id, adm, lock=True)
        mapping = await _get(session, GraphMapping, operation.mapping_id, adm, lock=True)
        assert current is not None
        assert partition is not None
        if partition.lease_token != token or current.lease_owner != token:
            raise GraphOperationError("graph_clean_publication_lease_lost")
        assert mapping is not None
        if mapping.desired_revision != operation.desired_revision or mapping.desired_digest != operation.desired_digest:
            return False
        mapping.external_state = "absent"
        await _commit(session, adm, [await public.graph_status_change(session, mapping.id, scope=adm.scope)])
        return True


async def _join_local_cleanup(awaitable: Awaitable[Any]) -> Any:
    """Join owner publication/teardown despite repeated task cancellation.

    The child remains awaited by the heavy owner until completion and exceptions
    propagate. This is a cancellation join, not a hard wall-clock thread bound;
    cleanup may exceed the cooperative operation deadline. Only cleanup and
    uncertainty publication use this helper, never new inference work.
    """
    task = asyncio.ensure_future(awaitable)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Repeated ARQ/deadline cancellation must not detach local cleanup
            # while another worker can acquire the database capacity key.
            continue
    return task.result()


async def process_graph_operation(ctx: dict[str, object], operation_id_value: str) -> None:
    """Execute one claimed projection while capacity owns publication and local teardown.

    Source fences and exact receipts govern remote recovery. Cancellation records
    uncertainty and joins local cleanup before releasing the heavy transaction;
    a lost database connection can release capacity earlier and proves no remote
    cessation. Contention retries without a domain claim.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    operation_id = UUID(operation_id_value)
    token = None
    # Fence first: durable lineage + account/workspace/membership admission and the
    # knowledge.temporal gate come before the capacity slot, the claim or any domain lock.
    # A denied subject is skipped with no ACK, retry or rebase onto fresh authority.
    adm = await _admit_job(factory, cast(Settings, ctx["settings"]), operation_id, [])
    if adm is None:
        return
    graph = TemporalGraph(GraphConfiguration.from_settings(cast(Settings, ctx["settings"])))
    interrupted: BaseException | None = None
    try:
        async with heavy_job_slot(factory, timeout_seconds=OWNER_BUDGET_SECONDS):
            try:
                token = await _claim(factory, adm, operation_id)
                if token is None:
                    return
                async with factory() as before_graph:
                    # Compare the original again immediately before the first graph contact.
                    await _admit(before_graph, adm)
                state = await graph.initialize()
                async with factory() as dependency:
                    await _admit(dependency, adm)
                    current = await _get(dependency, GraphOperation, operation_id, adm, lock=True)
                    assert current is not None
                    fingerprint = await _dependency_fingerprint(dependency, ctx, state, adm, current.partition_id)
                    current.dependency_fingerprint = fingerprint
                    await _commit(dependency, adm)
                if state != GraphState.READY:
                    await _finish(factory, adm, operation_id, token, "blocked", "graph_" + str(state))
                    return
                async with factory() as session:
                    await _admit(session, adm)
                    operation = await _get(session, GraphOperation, operation_id, adm)
                    assert operation is not None
                    mapping = await _get(session, GraphMapping, operation.mapping_id, adm)
                    assert mapping is not None
                    ancestor = await session.scalar(select(GraphOperation.id).where(
                        GraphOperation.workspace_id == adm.scope.workspace_id,
                        GraphOperation.mapping_id == mapping.id, GraphOperation.id != operation.id,
                        GraphOperation.cleanup_completed_at.is_(None),
                        (select(GraphReceipt.id).where(GraphReceipt.workspace_id == adm.scope.workspace_id,
                            GraphReceipt.operation_id == GraphOperation.id).exists())
                        | GraphOperation.dispatched_at.is_not(None)
                    ).order_by(GraphOperation.created_at.desc(), GraphOperation.id.desc()).limit(1))
                    # Establish cessation/absence before model identity checks. A
                    # cleaned old identity must not prevent a new model rebuild;
                    # inference receives a separate current-policy scope below.
                    context, gateway, rows = await _context(factory, adm, session, ctx, operation, mapping, token,
                                                           cleanup=True)
                    prior_dispatches = (await session.scalars(select(GraphDispatch).where(
                        GraphDispatch.workspace_id == adm.scope.workspace_id,
                        GraphDispatch.operation_id == operation.id, GraphDispatch.completed_at.is_(None),
                        GraphDispatch.cessation_verified_at.is_(None)).order_by(GraphDispatch.id))).all()
                    for prior_dispatch in prior_dispatches:
                        assert mapping is not None
                        assert operation is not None
                        previous = DispatchOwnership(operation.id, str(mapping.partition_id),
                                                     prior_dispatch.server_run_id, prior_dispatch.client_id)
                        if not await graph.verify_dispatch_cessation(previous, context):
                            raise GraphOperationUnknown("graph_prior_dispatch_still_unknown")
                        async with factory() as stopped:
                            await _admit(stopped, adm)
                            assert operation is not None
                            current = await _get(stopped, GraphOperation, operation.id, adm, lock=True)
                            assert current is not None
                            current.cessation_verified_at = datetime.now(UTC)
                            current.cessation_reason = "owned_sync_client_killed"
                            journal = await _get(stopped, GraphDispatch, prior_dispatch.id, adm, lock=True)
                            assert journal is not None
                            journal.cessation_verified_at = datetime.now(UTC)
                            journal.cessation_reason = "owned_sync_client_killed"
                            await _commit(stopped, adm)
                    if context.receipt_history:
                        await _recover(graph, factory, adm, session, ctx, operation, mapping, token, context, rows)
                        return
                    if ancestor is not None:
                        await _recover_ancestor(graph, factory, adm, session, ctx, ancestor, operation, mapping, token)
                        return
                    if not await _publish_clean_projection(factory, adm, operation, token):
                        assert operation is not None
                        await _finish(factory, adm, operation.id, token, "succeeded", None)
                        return
                    await session.refresh(mapping)
                    assert mapping is not None
                    if mapping.tombstoned:
                        assert operation is not None
                        await _finish(factory, adm, operation.id, token, "succeeded", None, external_state="absent")
                        return
                    context, gateway, rows = await _context(factory, adm, session, ctx, operation, mapping, token, cleanup=False)
                    assert mapping is not None
                    data = await documents.read_extraction_input(
                        session, mapping.document_version_id, scope=adm.scope, multi_workspace_enabled=adm.multi,
                    )
                    if data is None:
                        raise GraphOperationError("graph_document_no_longer_ready")
                    content = "\n\n".join(chunk.content for chunk in data.chunks)
                    assert operation is not None
                    request = EpisodeRequest(mapping.episode_id, str(mapping.partition_id), "Evidence-backed document",
                                             content, data.observed_at, context.evidence,
                                             tuple(binding.canonical_entity_id for binding in context.canonical_bindings),
                                             operation.desired_revision, context.canonical_bindings)
                    async with graph.dispatch_session(context):
                        await graph.upsert_episode(request, context, gateway)
                    # The awaited synchronous command transport completed; publication still
                    # needs owner locks and revision checks after remote inference.
                    await entities.lock_entity_ids(
                        session, [binding.canonical_entity_id for binding in context.canonical_bindings],
                        scope=adm.scope, multi_workspace_enabled=adm.multi)
                    event_refs = await timeline.temporal_event_refs(
                        session, [mapping.document_version_id], scope=adm.scope, multi_workspace_enabled=adm.multi)
                    await timeline.lock_event_ids(
                        session, sorted({item["event_id"] for item in event_refs}, key=str),
                        scope=adm.scope, multi_workspace_enabled=adm.multi)
                    current_bindings = tuple(replace(binding, mapping_revision=operation.desired_revision)
                        for binding in await _bindings(session, mapping,
                            [item for item in await session.scalars(select(GraphSupport).where(
                                GraphSupport.workspace_id == adm.scope.workspace_id,
                                GraphSupport.mapping_id == mapping.id, GraphSupport.removed.is_(False))) ],
                            scope=adm.scope, multi_workspace_enabled=adm.multi))
                    if current_bindings != context.canonical_bindings:
                        raise GraphOperationUnknown("graph_canonical_revision_changed_before_publication")
                    # Source/document/entity/event fences remain held while the
                    # independent projection transaction publishes its revision.
                    # Releasing them first lets a correction invalidate this result.
                    await _finish(factory, adm, operation.id, token, "succeeded", None, external_state="present")
            except (GraphOperationUnknown, asyncio.CancelledError, TimeoutError) as exc:
                if isinstance(exc, (asyncio.CancelledError, TimeoutError)):
                    interrupted = exc
                if token:
                    await _join_local_cleanup(_finish(factory, adm, operation_id, token, "reconcile_needed", "graph_outcome_unknown"))
            except (GraphOperationError, ValueError, LookupError, PrivacyPolicyDenied) as exc:
                if token:
                    await _join_local_cleanup(_finish(factory, adm, operation_id, token, "blocked", str(exc) if str(exc).startswith("graph_") else "graph_owner_proof_unavailable"))
            finally:
                await _join_local_cleanup(graph.close())
    except HeavyWorkBusy as exc:
        raise Retry(defer=HEAVY_RETRY_DEFER_SECONDS) from exc
    except RemoteHeavyWorkBlocked as exc:
        raise Retry(defer=30) from exc
    except HTTPException:
        # A drift raised while handling cancellation must not turn the cancel into a normal completion.
        if interrupted is not None:
            raise interrupted from None
        # Original workspace authority was revoked or changed: no further effect, ACK or rebase.
        # The lease expires; legitimately admitted recovery handles any unknown outcome later.
        return
    except HeavyLeaseLost:
        # The cancellation handler already fenced the graph outcome before its
        # awaited local cleanup; ownership loss itself is not remote cessation.
        return


async def _recover(graph: TemporalGraph, factory: async_sessionmaker[AsyncSession], adm: _Admission, session: AsyncSession,
                    ctx: dict[str, object], operation: GraphOperation, mapping: GraphMapping, token: UUID,
                    context: OperationAuthorization, rows: list[GraphMapping], *, cleanup_only: bool = False) -> None:
    """Inspect an original journal and recover typed effects under current owner fences.

    A fully matching current upsert may publish its exact readback. Ancestor
    cleanup always removes that original episode and records cleanup completion,
    even when its old fields match the current desired snapshot. Shared effects
    use fresh canonical replacements or durable dependent rebuild obligations;
    every adapter outcome must converge before terminal delete/retry publication.
    """
    receipts = context.receipt_history
    latest_nodes = {identifier: state for receipt in receipts for identifier, state in receipt.intended_entity_state_fingerprints}
    bindings = {binding.graph_entity_uuid: binding for binding in context.canonical_bindings}
    actions = tuple(CanonicalNodeRecoveryAction(identifier, fingerprint,
                    "replace_from_current_support" if identifier in bindings else "delete_orphan",
                    replacement_binding=bindings.get(identifier), replacement_created_at=operation.replacement_created_at
                    if identifier in bindings else None)
                    for identifier, fingerprint in sorted(latest_nodes.items()))
    async with graph.dispatch_session(context):
        inspection = await graph.inspect_write_receipt(receipts, context, actions)
    # A fully matching interrupted upsert has already converged. Publish its
    # exact readback rather than erase/re-extract an unchanged desired snapshot.
    bulk = next((receipt for receipt in reversed(receipts) if receipt.phase == "bulk_write_intent"), None)
    if not cleanup_only and bulk is not None and not mapping.tombstoned and not any(receipt.phase == "cleanup_write_intent" for receipt in receipts):
        intended_nodes = {identifier: fingerprint for receipt in receipts
            for identifier, fingerprint in receipt.intended_entity_state_fingerprints}
        intended_facts = {state.fact_id: state for receipt in receipts for state in receipt.intended_fact_support}
        intended_mentions = {identifier for receipt in receipts for identifier in receipt.mention_ids}
        current_facts = {state.fact_id: state for state in inspection.current_fact_states}
        normalized_bindings = tuple(replace(binding, node_name=None, node_summary=None) for binding in context.canonical_bindings)
        if (mapping.desired_revision == operation.desired_revision and mapping.desired_digest == operation.desired_digest
                and tuple(bulk.canonical_bindings) == normalized_bindings and inspection.episode_present
                and inspection.episode_state_fingerprint == bulk.episode_state_fingerprint
                and dict(inspection.current_entity_state_fingerprints) == intended_nodes
                and set(current_facts) == set(intended_facts)
                and all(current_facts[identifier].state_fingerprint == state.state_fingerprint
                    for identifier, state in intended_facts.items())
                and set(inspection.mention_ids_present) == intended_mentions
                and {link.edge_id for link in inspection.current_mention_links} == intended_mentions
                and all(link.relationship_type == "MENTIONS" and link.source_node_id == str(mapping.episode_id)
                    and link.target_node_id in intended_nodes for link in inspection.current_mention_links)):
            await context.validate_partition("upsert", str(mapping.episode_id))
            await _finish(factory, adm, operation.id, token, "succeeded", None, external_state="present")
            return
    async with factory() as refresh:
        await _admit(refresh, adm)
        receipts, aggregate = await _receipt_inventory(refresh, operation.id, adm=adm, inspection=inspection)
    context = replace(context, receipt_history=receipts, receipt_aggregate=aggregate)
    node_fingerprints = dict(inspection.current_entity_state_fingerprints)
    actual_actions = tuple(replace(action, expected_current_state_fingerprint=node_fingerprints.get(action.graph_entity_uuid),
                                    expected_incident_links=tuple(link for link in inspection.incident_links
                                      if action.graph_entity_uuid in {link.source_node_id, link.target_node_id})) for action in actions)
    prepared_actions = []
    for action in actual_actions:
        episodes = {episode for link in action.expected_incident_links for episode in link.episode_ids}
        episodes.update(link.source_node_id for link in action.expected_incident_links if link.relationship_type == "MENTIONS")
        if action.action == "delete_orphan" and episodes - {str(mapping.episode_id)}:
            dependencies = await _mark_dependents_for_rebuild(factory, adm, operation, mapping, token, rows, episodes,
                {("node", action.graph_entity_uuid)} | {("fact" if link.relationship_type != "MENTIONS" else "mention", link.edge_id)
                    for link in action.expected_incident_links})
            action = replace(action, action="delete_for_rebuild", rebuild_mapping_ids=dependencies)
        prepared_actions.append(action)
    actual_actions = tuple(prepared_actions)
    # A target-only orphan still has its own episode links. Delete the episode
    # first; its persisted cleanup lets the next bounded pass remove that node.
    repaired_actions = tuple(action for action in actual_actions if action.action != "delete_orphan" or not action.expected_incident_links)
    if repaired_actions:
        async with graph.dispatch_session(context):
            node_outcome = await graph.reconcile_canonical_nodes(receipts, repaired_actions, context)
        if not node_outcome.converged or node_outcome.unresolved_ids:
            await _finish(factory, adm, operation.id, token, "reconcile_needed", "graph_node_recovery_pending")
            return
    async with factory() as refresh:
        await _admit(refresh, adm)
        receipts, aggregate = await _receipt_inventory(refresh, operation.id, adm=adm)
    context = replace(context, receipt_history=receipts, receipt_aggregate=aggregate, node_recovery_actions=actual_actions)
    # Node repair may remove an incident fact or change its fingerprint. Build
    # every subsequent fact action from a new exact readback of the new journal.
    async with graph.dispatch_session(context):
        inspection = await graph.inspect_write_receipt(receipts, context, actual_actions)
    async with factory() as refresh:
        await _admit(refresh, adm)
        receipts, aggregate = await _receipt_inventory(refresh, operation.id, adm=adm, inspection=inspection)
    final_node_states = {identifier: fingerprint for receipt in receipts
        for identifier, fingerprint in receipt.intended_entity_state_fingerprints}
    inspected_nodes = dict(inspection.current_entity_state_fingerprints)
    if any(action.graph_entity_uuid not in final_node_states or
            inspected_nodes.get(action.graph_entity_uuid) != final_node_states[action.graph_entity_uuid]
            for action in repaired_actions):
        await _finish(factory, adm, operation.id, token, "reconcile_needed", "graph_node_final_readback_changed")
        return
    latest_support = {state.fact_id: state for receipt in receipts for state in receipt.intended_fact_support}
    current_facts = {state.fact_id: state for state in inspection.current_fact_states}
    fact_actions = []
    for fact_id, state in sorted(latest_support.items()):
        remaining = set(state.episode_ids) - {str(mapping.episode_id)}
        current = current_facts.get(fact_id)
        if current is None and remaining:
            # A different owned rebuild may already have physically removed
            # this fact. Consume that exact deletion proof before discarding
            # terminal owners from the historical support claim.
            assert context.authorize_rebuild_absence is not None
            await context.authorize_rebuild_absence("fact", fact_id)
            terminal = {str(row.episode_id) for row in rows if row.tombstoned and row.external_state == "absent"
                and row.applied_revision == row.desired_revision and row.applied_digest == row.desired_digest
                and row.error_code is None}
            remaining -= terminal
        expected = current.state_fingerprint if current else next((item.state_fingerprint for receipt in reversed(receipts)
            for item in receipt.existing_fact_states if item.fact_id == fact_id), state.state_fingerprint)
        if not remaining:
            fact_actions.append(ExactFactRecoveryAction(fact_id, state.source_node_id, state.target_node_id,
                                                        expected, "delete_unsupported"))
        else:
            try:
                fact_actions.append(await _fresh_fact_action(factory, adm, session, ctx, operation, mapping, token,
                    context, rows, fact_id, state.source_node_id, state.target_node_id, expected, remaining))
            except GraphOperationError as exc:
                if str(exc) not in {"graph_candidate_fact_requires_rebuild", "graph_fact_canonical_mapping_unavailable",
                        "graph_fact_canonical_mapping_ambiguous", "graph_fact_current_support_unproved",
                        "graph_embedding_identity_changed_requires_rebuild"}:
                    raise
                dependencies = await _mark_dependents_for_rebuild(factory, adm, operation, mapping, token, rows, remaining,
                    {("fact", fact_id)})
                fact_actions.append(ExactFactRecoveryAction(fact_id, state.source_node_id, state.target_node_id,
                    expected, "delete_for_rebuild", rebuild_mapping_ids=dependencies))
    context = replace(context, receipt_history=receipts, receipt_aggregate=aggregate, node_recovery_actions=actual_actions,
                      fact_recovery_actions=tuple(fact_actions))
    applicable = receipts
    if fact_actions:
        async with graph.dispatch_session(context):
            fact_outcome = await graph.reconcile_fact_recovery(applicable, tuple(fact_actions), context)
        if not fact_outcome.converged or fact_outcome.unresolved_ids:
            await _finish(factory, adm, operation.id, token, "reconcile_needed", "graph_fact_recovery_pending")
            return
        async with factory() as refresh:
            await _admit(refresh, adm)
            history, aggregate = await _receipt_inventory(refresh, operation.id, adm=adm)
            context = replace(context, receipt_history=history, receipt_aggregate=aggregate)
    canonical_only = all(receipt.phase == "canonical_node_write_intent" or
        (receipt.phase == "cleanup_write_intent" and not receipt.prior_episode_state_fingerprint)
        for receipt in receipts)
    if canonical_only and not inspection.episode_present and not inspection.fact_ids_present and not inspection.mention_ids_present:
        succeeded, removed = True, True
    else:
        async with graph.dispatch_session(context):
            outcome = await graph.delete_episode(mapping.episode_id, context, True, True)
        succeeded, removed = outcome.outcome == "succeeded", outcome.removed
    if not succeeded:
        await _finish(factory, adm, operation.id, token, "reconcile_needed", "graph_current_recovery_pending",
                      external_state="absent" if removed else "unknown")
        return
    async with factory() as journal:
        await _admit(journal, adm)
        op_row = await _get(journal, GraphOperation, operation.id, adm, lock=True)
        assert op_row is not None
        op_row.cleanup_completed_at = datetime.now(UTC)
        await _commit(journal, adm)
    if cleanup_only:
        return
    if mapping.tombstoned:
        await _finish(factory, adm, operation.id, token, "succeeded", None, external_state="absent")
    else:
        # Cleanup converged; retain stable episode UUID and queue the current desired
        # canonical state under a new attempt, keeping every prior receipt immutable.
        await _finish(factory, adm, operation.id, token, "succeeded", None, external_state="absent", retry_upsert=True)


async def _recover_ancestor(graph: TemporalGraph, factory: async_sessionmaker[AsyncSession], adm: _Admission, session: AsyncSession,
                            ctx: dict[str, object], ancestor_id: UUID, intent: GraphOperation,
                            mapping: GraphMapping, token: UUID) -> None:
    """Clean one original operation journal per job without copying its receipt identity into a newer intent.

    Current partition ownership fences old tasks; the original receipt token,
    revision and immutable IDs remain unchanged. A durable completion marker
    advances lifetime history across retries instead of loading it in memory.
    """
    async with factory() as claim:
        await _admit(claim, adm)
        partition = await _get(claim, GraphPartition, mapping.partition_id, adm, lock=True)
        ancestor = await _get(claim, GraphOperation, ancestor_id, adm, lock=True)
        assert ancestor is not None
        assert partition is not None
        if partition.lease_token != token or ancestor.cleanup_completed_at is not None:
            return
        ancestor.lease_owner, ancestor.lease_expires_at = token, partition.lease_expires_at
        await _commit(claim, adm)
    mapping_id, intent_id = mapping.id, intent.id
    session.expire_all()
    await _admit(session, adm)
    fresh_ancestor = await _get(session, GraphOperation, ancestor_id, adm)
    fresh_mapping = await _get(session, GraphMapping, mapping_id, adm)
    assert fresh_ancestor is not None and fresh_mapping is not None
    ancestor, mapping = fresh_ancestor, fresh_mapping
    context, _gateway, rows = await _context(factory, adm, session, ctx, ancestor, mapping, token, cleanup=True)
    prior = (await session.scalars(select(GraphDispatch).where(
        GraphDispatch.workspace_id == adm.scope.workspace_id, GraphDispatch.operation_id == ancestor_id,
        GraphDispatch.completed_at.is_(None), GraphDispatch.cessation_verified_at.is_(None)))).all()
    for dispatch in prior:
        assert ancestor is not None
        ownership = DispatchOwnership(ancestor.id, str(mapping.partition_id), dispatch.server_run_id, dispatch.client_id)
        if not await graph.verify_dispatch_cessation(ownership, context):
            raise GraphOperationUnknown("graph_ancestor_dispatch_unknown")
        async with factory() as journal:
            await _admit(journal, adm)
            stopped = await _get(journal, GraphDispatch, dispatch.id, adm, lock=True)
            assert stopped is not None
            stopped.cessation_verified_at, stopped.cessation_reason = datetime.now(UTC), "owned_sync_client_killed"
            await _commit(journal, adm)
    if context.receipt_history:
        await _recover(graph, factory, adm, session, ctx, ancestor, mapping, token, context, rows, cleanup_only=True)
    else:
        # Dispatch setup may create indexes, but every logical node/episode/fact
        # write awaits its durable prewrite receipt. No receipt plus exact prior
        # cessation proves this original actor owns no logical cleanup effects.
        async with factory() as journal:
            await _admit(journal, adm)
            assert ancestor is not None
            cleaned = await _get(journal, GraphOperation, ancestor.id, adm, lock=True)
            assert cleaned is not None
            cleaned.cleanup_completed_at = datetime.now(UTC)
            await _commit(journal, adm)
    async with factory() as progress:
        await _admit(progress, adm)
        completed = await _get(progress, GraphOperation, ancestor_id, adm)
        assert completed is not None
        succeeded = completed.cleanup_completed_at is not None
    if not succeeded:
        raise GraphOperationUnknown("graph_ancestor_cleanup_pending")
    await _finish(factory, adm, intent_id, token, "reconcile_needed", "graph_ancestor_cleanup_continuation")


async def _recovery_workspaces(redis: ArqRedis, factory: async_sessionmaker[AsyncSession]) -> list[UUID]:
    """ Page workspaces that currently have due work, fairly, by a keyset cursor over workspace IDs.

    Only identifiers are read, in a read transaction that is rolled back. A denied or disabled
    workspace therefore costs one slot per page and cannot monopolize the oldest-first order.
    If Redis cannot supply the cursor the scan simply restarts from the first workspace.
    # ponytail: restart-from-first on Redis loss can re-visit denied head workspaces; page is bounded.
    """
    after: UUID | None = None
    try:
        raw = await redis.get(RECOVERY_CURSOR_KEY)
        after = UUID(raw.decode() if isinstance(raw, bytes) else str(raw)) if raw else None
    except (RedisError, ValueError, TypeError, OSError):
        after = None
    now = datetime.now(UTC)
    due = or_(
        and_(GraphOperation.status == "blocked", GraphOperation.next_attempt_at <= now),
        and_(GraphOperation.status.in_(["pending", "reconcile_needed", "running"]),
             GraphOperation.next_attempt_at <= now,
             or_(GraphOperation.lease_expires_at.is_(None), GraphOperation.lease_expires_at <= now)),
    )
    candidates = select(GraphOperation.workspace_id.label("workspace_id")).where(due).union(
        select(GraphReconcileRun.workspace_id.label("workspace_id")).where(
            GraphReconcileRun.status.in_(["pending", "running"]))).subquery()
    page: list[UUID] = []
    for lower in ((after, None) if after is not None else (None,)):
        async with factory() as session:
            query = select(candidates.c.workspace_id).order_by(candidates.c.workspace_id).limit(RECOVERY_WORKSPACE_PAGE)
            if lower is not None:
                query = query.where(candidates.c.workspace_id > lower)
            page = list((await session.scalars(query)).all())
            await session.rollback()
        if page:
            break
    try:
        if page:
            await redis.set(RECOVERY_CURSOR_KEY, str(page[-1]), ex=3600)
        else:
            await redis.delete(RECOVERY_CURSOR_KEY)
    except (RedisError, OSError):
        pass  # the next pass restarts from the first workspace
    return page


async def recover_graph_work(ctx: dict[str, object]) -> int:
    """ Recover due intents/runs one admitted workspace per transaction; Redis loss never deletes work.

    Each workspace is admitted through the access fence before any lock, then handled in its own
    short transactions (25 due rows). Denied, disabled or revoked workspaces are skipped without
    mutation. The graph is probed only after a workspace with blocked work is admitted.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    redis = cast(ArqRedis, ctx["redis"])
    settings = cast(Settings, ctx["settings"])
    dependency_state: GraphState | None = None
    enqueued = 0
    for workspace_id in await _recovery_workspaces(redis, factory):
        adm = await admit_workspace(factory, settings, workspace_id)
        if adm is None:
            continue
        try:
            now = datetime.now(UTC)
            async with factory() as session:
                await _admit(session, adm)
                blocked = (await session.scalars(select(GraphOperation.id).where(
                    GraphOperation.workspace_id == workspace_id,
                    GraphOperation.status == "blocked", GraphOperation.next_attempt_at <= now,
                ).order_by(GraphOperation.next_attempt_at, GraphOperation.id).limit(25))).all()
                await session.rollback()
            if blocked and dependency_state is None:
                probe = TemporalGraph(GraphConfiguration.from_settings(settings))
                try:
                    dependency_state = await probe.initialize()
                finally:
                    await probe.close()
            async with factory() as session:
                await _admit(session, adm)
                for blocked_id in blocked:
                    # Retry admission uses the same row order as execution. Mapping is
                    # deliberately untouched; any uncertainty retains its exact ledger.
                    locator = await _get(session, GraphOperation, blocked_id, adm)
                    assert locator is not None and dependency_state is not None
                    fingerprint = await _dependency_fingerprint(session, ctx, dependency_state, adm, locator.partition_id)
                    if locator.dependency_fingerprint == fingerprint:
                        continue
                    partition = await _get(session, GraphPartition, locator.partition_id, adm, lock=True)
                    operation = await _get(session, GraphOperation, blocked_id, adm, lock=True)
                    assert operation is not None
                    assert partition is not None
                    if operation.status == "blocked" and (partition.lease_expires_at is None or partition.lease_expires_at <= now):
                        operation.status, operation.attempts = "pending", 0
                        operation.dependency_fingerprint = fingerprint
                        mapping = await _get(session, GraphMapping, operation.mapping_id, adm, lock=True)
                        assert mapping is not None
                        if mapping.desired_revision == operation.desired_revision and mapping.desired_digest == operation.desired_digest:
                            mapping.status, mapping.error_code = "pending", None
                            await _commit(session, adm, [await public.graph_status_change(session, mapping.id, scope=adm.scope)])
                operations = (await session.scalars(select(GraphOperation.id).where(
                    GraphOperation.workspace_id == workspace_id,
                    GraphOperation.status.in_(["pending", "reconcile_needed", "running"]),
                    GraphOperation.next_attempt_at <= now,
                    (GraphOperation.lease_expires_at.is_(None)) | (GraphOperation.lease_expires_at <= now),
                ).order_by(GraphOperation.next_attempt_at, GraphOperation.id).limit(25))).all()
                runs = (await session.scalars(select(GraphReconcileRun.id).where(
                    GraphReconcileRun.workspace_id == workspace_id,
                    GraphReconcileRun.status.in_(["pending", "running"]))
                    .order_by(GraphReconcileRun.created_at).limit(25).with_for_update(skip_locked=True))).all()
                for run_id in runs:
                    await public.reconcile_slice(session, run_id, scope=adm.scope, multi_workspace_enabled=adm.multi)
                await _commit(session, adm)
        except HTTPException:
            continue  # authority changed mid-pass: skip this workspace, no rebase
        for operation_id in operations:
            await redis.enqueue_job("process_graph_operation", str(operation_id), _job_id="graph:" + str(operation_id))
        enqueued += len(operations)
    return enqueued
