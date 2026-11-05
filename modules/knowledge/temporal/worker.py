"""Durable temporal execution with full owner fences, ordered receipts and exclusive graph dispatch."""

import asyncio
import hashlib
import json
import math
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast, overload
from uuid import UUID, uuid5

from arq.connections import ArqRedis
from fastapi import HTTPException
from pydantic import TypeAdapter
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.errors import ConflictError, DomainError
from core.heavy_work import (
    HEAVY_RETRY_DEFER_SECONDS,
    HeavyLeaseLost,
    HeavyWorkBusy,
    RemoteHeavyWorkBlocked,
    heavy_job_slot,
)
from core.heavy_work.slots import (
    HEAVY_BLOCKED_BASE_SECONDS,
    HEAVY_BLOCKED_CAP_SECONDS,
    HEAVY_RETRY_CAP_SECONDS,
)
from core.ids import uuid7
from core.job_denial import admit_retry_stale, denial_code, terminalize
from core.model_gateway.client import ModelGateway, PrivacyPolicyDenied
from core.model_gateway.schemas import ModelMapping, RequestPolicy
from core.realtime import commit_with_replay
from core.worker_context import worker_session_factory
from core.worker_retry import retry_with_backoff
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
    """One invocation's original authority, carried through every short transaction and callback.

    The scope and fence are captured once by admit_workspace before any claim or domain lock.
    Every later session re-reads the fence without locking and requires exact equality; a
    revoked or changed original aborts that subject (no ACK, no rebase onto fresh authority).
    """

    scope: InternalJobScope
    multi: bool
    fence: AccessFence


async def _admit(session: AsyncSession, adm: _Admission) -> None:
    """Compare current workspace authority with the original invocation fence (nonlocking, 409 on drift).

    Nonlocking on purpose: the outer job transaction may already hold the admission locks
    (Sources.lock_source) while nested journal sessions run; a locking call here would
    wait on its own invocation.
    """
    current = await workspaces.read_access_fence(
        session, scope=adm.scope, multi_workspace_enabled=adm.multi
    )
    if current != adm.fence:
        raise ConflictError("Workspace authority changed")


async def _commit(
    session: AsyncSession, adm: _Admission, drafts: list[Any] | tuple[Any, ...] = ()
) -> None:
    """Commit domain rows (and optional replay drafts) only if the original fence still holds."""
    await commit_with_replay(
        session, drafts, scope=adm.scope, multi_workspace_enabled=adm.multi, access_fence=adm.fence
    )


async def _get(
    session: AsyncSession, model: Any, identity: UUID | int, adm: _Admission, *, lock: bool = False
) -> Any:
    """Read one temporal root by ID inside the admitted workspace; a foreign ID is simply absent."""
    query = select(model).where(model.id == identity, model.workspace_id == adm.scope.workspace_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return await session.scalar(query)


async def admit_workspace(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    workspace_id: UUID,
    denial: list[str] | None = None,
) -> _Admission | None:
    """Resolve the workspace's durable owner, lock admission FIRST and capture one original fence.

    No other lock is taken before this fence. Returns None (skip, no mutation) when the owner
    lineage, account/membership admission or the per-workspace knowledge.temporal module gate denies.
    A typed denial (409 only after one re-resolve) appends its terminal code to ``denial`` so a
    caller owning a durable row can end it. The locks are released before any claim, graph or
    model I/O; later effects only compare.
    """
    multi = bool(settings.multi_workspace_enabled)

    async def attempt() -> _Admission | None:
        """Resolve the workspace owner, admit the internal job and read the module gate once.

        Returns the captured admission, or None when ``knowledge.temporal`` is disabled; the
        session is always rolled back (read-only). Admission denials propagate to the caller.
        """
        async with factory() as session:
            try:
                owner = await workspaces.resolve_workspace_owner_context(
                    session,
                    workspace_id,
                    multi_workspace_enabled=multi,
                )
                if owner is None:
                    return None
                scope = InternalJobScope(
                    workspace_id=workspace_id,
                    actor_user_id=owner.user_id,
                    membership_revision=owner.membership_revision,
                )
                fence = await workspaces.authorize_internal_job(
                    session, scope=scope, multi_workspace_enabled=multi
                )
                enabled = await settings_public.module_is_enabled(
                    session,
                    "knowledge.temporal",
                    scope=scope,
                    multi_workspace_enabled=multi,
                )
            finally:
                await session.rollback()
        return _Admission(scope, multi, fence) if enabled else None

    try:
        return await admit_retry_stale(attempt)
    except (HTTPException, DomainError) as exc:
        code = denial_code(exc)
        if code is None:
            raise
        if denial is not None:
            denial.append(code)
        return None


async def _admit_job(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    operation_id: UUID,
    denial: list[str] | None = None,
) -> _Admission | None:
    """Admit one queued operation: validate its durable workspace/partition/mapping lineage, then the fence."""
    async with factory() as session:
        row = (
            await session.execute(
                select(
                    GraphOperation.workspace_id,
                    GraphOperation.mapping_id,
                    GraphOperation.partition_id,
                ).where(GraphOperation.id == operation_id)
            )
        ).one_or_none()
        lineage = (
            await session.scalar(
                select(GraphMapping.id)
                .join(
                    GraphPartition,
                    GraphPartition.id == GraphMapping.partition_id,
                )
                .where(
                    GraphMapping.id == row.mapping_id,
                    GraphMapping.partition_id == row.partition_id,
                    GraphMapping.workspace_id == row.workspace_id,
                    GraphPartition.workspace_id == row.workspace_id,
                )
            )
            if row is not None
            else None
        )
        await session.rollback()
    if row is None or lineage is None:
        return None
    adm = await admit_workspace(factory, settings, row.workspace_id, denial)
    if adm is None and denial:
        # Only never-dispatched, unleased work is ended; dispatched rows keep unknown-outcome recovery.
        await terminalize(
            factory,
            GraphOperation,
            operation_id,
            row.workspace_id,
            denial[0],
            from_status=("pending", "blocked"),
            extra_where=(
                GraphOperation.dispatched_at.is_(None),
                GraphOperation.lease_owner.is_(None),
            ),
        )
    return adm


async def _dependency_fingerprint(
    session: AsyncSession,
    ctx: dict[str, object],
    graph_state: GraphState,
    adm: _Admission,
    partition_id: UUID | None = None,
) -> str:
    """Hash nonsecret policy/model and graph configuration so blocked work retries only after an actual dependency change."""
    settings = cast(Settings, ctx["settings"])
    config = await settings_public.get_ai_execution_config(
        session,
        settings,
        cast(Redis, ctx["redis"]),
        scope=adm.scope,
    )
    owner_state = []
    if partition_id is not None:
        rows = (
            await session.scalars(
                select(GraphMapping)
                .where(
                    GraphMapping.workspace_id == adm.scope.workspace_id,
                    GraphMapping.partition_id == partition_id,
                )
                .order_by(GraphMapping.id)
                .limit(101)
            )
        ).all()
        owner_state = [
            [
                str(row.id),
                row.desired_revision,
                row.desired_digest,
                row.tombstoned,
                row.external_state,
                row.embedding_identity,
            ]
            for row in rows
        ]
        partition = await _get(session, GraphPartition, partition_id, adm)
        source = (
            await sources.get_source_fence(
                session,
                partition.source_id,
                scope=adm.scope,
                multi_workspace_enabled=adm.multi,
            )
            if partition is not None
            else None
        )
        owner_state.append(
            [source.status, source.generation, source.local_only]
            if source
            else ["source_unavailable"]
        )
    return public.digest(
        [
            str(graph_state),
            owner_state,
            settings.graph_enabled,
            settings.graph_host,
            settings.graph_port,
            settings.graph_database,
            settings.graph_embedding_dimensions,
            config.configuration_revision,
            config.gateway_identity,
            config.defaults.model_dump(mode="json"),
            config.privacy.model_dump(mode="json"),
            config.endpoint_destination_id,
        ]
    )


async def _mark_dependents_for_rebuild(
    factory: async_sessionmaker[AsyncSession],
    adm: _Admission,
    operation: GraphOperation,
    mapping: GraphMapping,
    token: UUID,
    rows: list[GraphMapping],
    episode_ids: set[str],
    effects: set[tuple[str, str]],
) -> tuple[UUID, ...]:
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
        if current_operation is None:
            raise RuntimeError("invariant violated: current_operation must not be None")
        if partition is None:
            raise RuntimeError("invariant violated: partition must not be None")
        if partition.lease_token != token or current_operation.lease_owner != token:
            raise GraphOperationError("graph_rebuild_dependency_lease_lost")
        drafts = []
        for mapping_id in mapping_ids:
            dependent = await _get(journal, GraphMapping, mapping_id, adm, lock=True)
            if (
                dependent is None
                or dependent.partition_id != mapping.partition_id
                or dependent.tombstoned
            ):
                raise GraphOperationError("graph_rebuild_dependency_changed")
            dependency = await journal.scalar(
                select(GraphRebuildDependency)
                .where(
                    GraphRebuildDependency.workspace_id == adm.scope.workspace_id,
                    GraphRebuildDependency.operation_id == operation.id,
                    GraphRebuildDependency.mapping_id == mapping_id,
                )
                .with_for_update()
            )
            if dependency is None:
                journal.add(
                    GraphRebuildDependency(
                        workspace_id=adm.scope.workspace_id,
                        operation_id=operation.id,
                        mapping_id=mapping_id,
                        source_generation=mapping.source_generation,
                        effect_ids=[list(item) for item in sorted(effects)],
                    )
                )
                # An explicit new attempt owns no copied receipts. Its ancestor
                # cleanup will consume the original destructive proof below.
                journal.add(
                    GraphOperation(
                        workspace_id=adm.scope.workspace_id,
                        mapping_id=mapping_id,
                        partition_id=mapping.partition_id,
                        kind="upsert",
                        desired_revision=dependent.desired_revision,
                        desired_digest=dependent.desired_digest,
                        next_attempt_at=datetime.now(UTC),
                        replacement_created_at=datetime.now(UTC),
                    )
                )
            else:
                dependency.effect_ids = [
                    list(item)
                    for item in sorted({tuple(item) for item in dependency.effect_ids} | effects)
                ]
                if len(dependency.effect_ids) > 100:
                    raise GraphOperationError("graph_rebuild_dependency_effects_over_bound")
            dependent.status, dependent.external_state = "pending", "unknown"
            dependent.error_code = "graph_dependent_rebuild_pending"
            drafts.append(await public.graph_status_change(journal, mapping_id, scope=adm.scope))
        await _commit(journal, adm, drafts)
    return mapping_ids


async def _authorize_scheduled_rebuild(
    factory: async_sessionmaker[AsyncSession],
    adm: _Admission,
    operation: GraphOperation,
    mapping: GraphMapping,
    mapping_ids: tuple[UUID, ...],
    effects: set[tuple[str, str]],
) -> None:
    """Require committed original-operation links for every affected owner mapping and exact destructive effect."""
    if not mapping_ids:
        raise GraphOperationError("graph_rebuild_dependencies_missing")
    async with factory() as check:
        await _admit(check, adm)
        dependencies = (
            await check.scalars(
                select(GraphRebuildDependency).where(
                    GraphRebuildDependency.workspace_id == adm.scope.workspace_id,
                    GraphRebuildDependency.operation_id == operation.id,
                    GraphRebuildDependency.mapping_id.in_(mapping_ids),
                )
            )
        ).all()
        if {row.mapping_id for row in dependencies} != set(mapping_ids) or any(
            row.source_generation != mapping.source_generation
            or not effects <= {tuple(item) for item in row.effect_ids}
            for row in dependencies
        ):
            raise GraphOperationError("graph_rebuild_dependencies_uncommitted")


async def _claim(  # noqa: C901
    factory: async_sessionmaker[AsyncSession], adm: _Admission, operation_id: UUID
) -> UUID | None:
    """Claim partition then intent in one short transaction; expired uncertain writers never become new extraction."""
    async with factory() as session:
        await _admit(session, adm)
        locator = await _get(session, GraphOperation, operation_id, adm)
        if locator is None:
            return None
        partition = await _get(session, GraphPartition, locator.partition_id, adm, lock=True)
        operation = await _get(session, GraphOperation, operation_id, adm, lock=True)
        now = datetime.now(UTC)
        if operation is None:
            raise RuntimeError("invariant violated: operation must not be None")
        if operation.status in {"succeeded", "failed"} or operation.next_attempt_at > now:
            return None
        if partition is None:
            raise RuntimeError("invariant violated: partition must not be None")
        if partition.lease_expires_at and partition.lease_expires_at > now:
            return None
        if partition.uncertain_operation_id and partition.uncertain_operation_id != operation.id:
            return None
        desired = await _get(session, GraphMapping, operation.mapping_id, adm, lock=True)
        if desired is None:
            raise RuntimeError("invariant violated: desired must not be None")
        if (
            desired.desired_revision != operation.desired_revision
            or desired.desired_digest != operation.desired_digest
        ) and operation.dispatched_at is None:
            if operation is None:
                raise RuntimeError("invariant violated: operation must not be None")
            operation.status, operation.error_code = "succeeded", "superseded_before_dispatch"
            await _commit(session, adm)
            return None
        if operation.attempts >= 5 and not partition.uncertain_operation_id:
            if operation is None:
                raise RuntimeError("invariant violated: operation must not be None")
            operation.status, operation.error_code = "failed", "attempts_exhausted"
            mapping = await _get(session, GraphMapping, operation.mapping_id, adm, lock=True)
            if mapping is None:
                raise RuntimeError("invariant violated: mapping must not be None")
            if (
                mapping.desired_revision == operation.desired_revision
                and mapping.desired_digest == operation.desired_digest
            ):
                if mapping is None:
                    raise RuntimeError("invariant violated: mapping must not be None")
                mapping.status, mapping.error_code = "failed", "attempts_exhausted"
            await _commit(
                session,
                adm,
                [await public.graph_status_change(session, mapping.id, scope=adm.scope)],
            )
            return None
        token = uuid7()
        partition.lease_token, partition.lease_expires_at = (
            token,
            now + timedelta(seconds=LEASE_SECONDS),
        )
        operation.lease_owner, operation.lease_expires_at = token, partition.lease_expires_at
        if operation is None:
            raise RuntimeError("invariant violated: operation must not be None")
        operation.status = "running"
        operation.attempts += 1
        await _commit(session, adm)
        return token


async def _lease(
    factory: async_sessionmaker[AsyncSession], adm: _Admission, operation_id: UUID, token: UUID
) -> None:
    """Recheck live exact partition/intent ownership; stale results cannot publish or authorize writes."""
    async with factory() as session:
        await _admit(session, adm)
        operation = await _get(session, GraphOperation, operation_id, adm)
        partition = (
            await _get(session, GraphPartition, operation.partition_id, adm) if operation else None
        )
        now = datetime.now(UTC)
        if (
            operation is None
            or partition is None
            or operation.lease_owner != token
            or partition.lease_token != token
            or operation.lease_expires_at is None
            or operation.lease_expires_at <= now
            or partition.lease_expires_at is None
            or partition.lease_expires_at <= now
        ):
            raise GraphOperationError("graph_partition_lease_lost")


async def _receipts(
    session: AsyncSession, operation_id: UUID, adm: _Admission
) -> tuple[GraphWriteReceipt, ...]:
    """Return actual immutable witnesses covering every distinct journal obligation; never truncate effects."""
    receipts, _ = await _receipt_inventory(session, operation_id, adm=adm)
    return receipts


@dataclass
class _WitnessIndex:
    """Incrementally index an append-only receipt ledger into bounded witness candidates.

    ``first``/``latest`` hold the earliest/latest receipt per phase and physical effect,
    ``matching`` the latest receipt whose fingerprint equals the live readback, and
    ``obligations`` the first receipt creating a shared/stale fact obligation. ``effects``
    accumulates the (entity, mention, fact, incident-link) id union for the aggregate.
    """

    inspection: GraphReceiptInspection | None
    witness_sequences: tuple[int, ...]
    digest: Any = field(default_factory=hashlib.sha256)
    first: dict[str, tuple[int, GraphWriteReceipt]] = field(default_factory=dict)
    latest: dict[str, tuple[int, GraphWriteReceipt]] = field(default_factory=dict)
    matching: dict[str, tuple[int, GraphWriteReceipt]] = field(default_factory=dict)
    obligations: dict[str, tuple[int, GraphWriteReceipt]] = field(default_factory=dict)
    extras: dict[int, GraphWriteReceipt] = field(default_factory=dict)
    effects: list[set[str]] = field(default_factory=lambda: [set(), set(), set(), set()])

    def __post_init__(self) -> None:
        """Precompute the live node and fact fingerprints the readback offers for matching."""
        inspection = self.inspection
        self.current_nodes = (
            dict(inspection.current_entity_state_fingerprints) if inspection else {}
        )
        self.current_facts = (
            {item.fact_id: item.state_fingerprint for item in inspection.current_fact_states}
            if inspection
            else {}
        )

    def add(self, cursor: int, payload: Any, receipt: GraphWriteReceipt) -> None:
        """Fold one receipt (at ledger position ``cursor``) into the digest, witnesses and effect union.

        Raises ``GraphOperationError`` when the effect union exceeds 100 ids.
        """
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        self.digest.update(f"{cursor}:".encode() + encoded.encode() + b"\n")
        entry = (cursor, receipt)
        keys = {"phase:" + receipt.phase}
        keys.update(
            "effect:" + identifier
            for identifier in (
                *receipt.entity_ids,
                *receipt.mention_ids,
                *receipt.fact_ids,
                *(link.edge_id for link in receipt.incident_links),
            )
        )
        for key in keys:
            self.first.setdefault(key, entry)
            self.latest[key] = entry
        if cursor in self.witness_sequences:
            self.extras[cursor] = receipt
        self._note_matching(entry)
        if receipt.phase == "cleanup_write_intent":
            self._note_obligations(entry)
        for destination, values in zip(
            self.effects,
            (
                receipt.entity_ids,
                receipt.mention_ids,
                receipt.fact_ids,
                tuple(link.edge_id for link in receipt.incident_links),
            ),
            strict=True,
        ):
            destination.update(values)
        if len(set().union(*self.effects)) > 100:
            raise GraphOperationError("graph_recovery_effect_inventory_over_bound")

    def _note_matching(self, entry: tuple[int, GraphWriteReceipt]) -> None:
        """Record the receipt as the current-matching witness for every effect/episode it fingerprints like the readback."""
        receipt = entry[1]
        for identifier, value in receipt.intended_entity_state_fingerprints:
            if value is not None and self.current_nodes.get(identifier) == value:
                self.matching["effect:" + identifier] = entry
        for identifier, present, value in receipt.prior_entity_state_fingerprints:
            if present and value is not None and self.current_nodes.get(identifier) == value:
                self.matching["effect:" + identifier] = entry
        states: tuple[ExactFactState | ExactFactSupport, ...] = (
            *receipt.existing_fact_states,
            *receipt.intended_fact_support,
        )
        for state in states:
            if self.current_facts.get(state.fact_id) == state.state_fingerprint:
                self.matching["effect:" + state.fact_id] = entry
        inspection = self.inspection
        if inspection and inspection.episode_state_fingerprint in {
            receipt.episode_state_fingerprint,
            receipt.prior_episode_state_fingerprint,
        }:
            self.matching["episode"] = entry
        if inspection:
            for link in receipt.incident_links:
                if link in inspection.incident_links:
                    self.matching["effect:" + link.edge_id] = entry

    def _note_obligations(self, entry: tuple[int, GraphWriteReceipt]) -> None:
        """Record the first cleanup receipt that leaves a shared-support or stale-validity fact obligation."""
        receipt = entry[1]
        prior_states = {state.fact_id: state for state in receipt.existing_fact_states}
        for intended in receipt.intended_fact_support:
            prior = prior_states.get(intended.fact_id)
            if prior is None or not prior.existed:
                continue
            target = str(receipt.episode_id)
            if (
                target in prior.episode_ids
                and target not in intended.episode_ids
                and intended.episode_ids
            ):
                self.obligations.setdefault("shared:" + intended.fact_id, entry)
            if (
                target not in prior.episode_ids
                and target not in intended.episode_ids
                and (prior.valid_at, prior.invalid_at, prior.expired_at)
                != (intended.valid_at, intended.invalid_at, intended.expired_at)
            ):
                self.obligations.setdefault("stale:" + intended.fact_id, entry)


async def _receipt_inventory(
    session: AsyncSession,
    operation_id: UUID,
    prefix_count: int | None = None,
    *,
    adm: _Admission,
    inspection: GraphReceiptInspection | None = None,
    witness_sequences: tuple[int, ...] = (),
) -> tuple[tuple[GraphWriteReceipt, ...], RecoveryReceiptAggregate | None]:
    """Page all original receipts and select bounded first/latest/current-matching witnesses per physical effect.

    Lifetime mutations are scanned without materializing their payloads. An
    original intermediate receipt is retained when its fingerprint matches the
    current exact readback; first prior and latest intended receipts also remain.
    The prefix digest binds every sequence/payload, including unselected history.
    Caller-supplied extra witness sequences are reread as originals and compared
    by the adapter against actual state; they cannot forge historical fingerprints.
    """
    adapter = TypeAdapter(GraphWriteReceipt)
    cursor = 0
    index = _WitnessIndex(inspection, witness_sequences)
    while prefix_count is None or cursor < prefix_count:
        query = select(GraphReceipt).where(
            GraphReceipt.workspace_id == adm.scope.workspace_id,
            GraphReceipt.operation_id == operation_id,
            GraphReceipt.sequence >= cursor,
        )
        if prefix_count is not None:
            query = query.where(GraphReceipt.sequence < prefix_count)
        rows = (await session.scalars(query.order_by(GraphReceipt.sequence).limit(100))).all()
        if not rows:
            break
        for row in rows:
            if row.sequence != cursor:
                raise GraphOperationError("graph_receipt_inventory_invalid")
            index.add(cursor, row.payload, adapter.validate_python(row.payload))
            cursor += 1
    if prefix_count is not None and cursor != prefix_count:
        raise GraphOperationError("graph_receipt_prefix_missing")
    selected = dict(index.extras)
    selected.update(
        {
            sequence: receipt
            for sequence, receipt in (
                *index.first.values(),
                *index.latest.values(),
                *index.matching.values(),
                *index.obligations.values(),
            )
        }
    )
    entries = tuple(sorted(selected.items()))
    if len(entries) > 512 or len(index.extras) != len(witness_sequences):
        raise GraphOperationError("graph_recovery_witness_inventory_invalid")
    if not entries:
        return (), None
    head = entries[0][1]
    effects = index.effects
    aggregate = RecoveryReceiptAggregate(
        operation_id=head.operation_id,
        group_id=head.group_id,
        lease_token=head.lease_token,
        mapping_revision=head.mapping_revision,
        ledger_count=cursor,
        ledger_digest=index.digest.hexdigest(),
        witness_sequences=tuple(sequence for sequence, _ in entries),
        entity_ids=tuple(sorted(effects[0])),
        mention_ids=tuple(sorted(effects[1])),
        fact_ids=tuple(sorted(effects[2])),
        incident_link_ids=tuple(sorted(effects[3])),
    )
    return tuple(receipt for _, receipt in entries), aggregate


async def _record(
    factory: async_sessionmaker[AsyncSession],
    adm: _Admission,
    operation_id: UUID,
    token: UUID,
    receipt: GraphWriteReceipt,
) -> None:
    """Commit every no-text prewrite receipt independently while source/document locks remain held by its caller."""
    await _lease(factory, adm, operation_id, token)
    async with factory() as session:
        await _admit(session, adm)
        operation = await _get(session, GraphOperation, operation_id, adm, lock=True)
        if operation is None:
            raise RuntimeError("invariant violated: operation must not be None")
        mapping = await _get(session, GraphMapping, operation.mapping_id, adm)
        if mapping is None:
            raise RuntimeError("invariant violated: mapping must not be None")
        if (
            receipt.operation_id != operation.id
            or receipt.lease_token != operation.receipt_token
            or receipt.mapping_revision != operation.desired_revision
            or receipt.episode_id != mapping.episode_id
            or receipt.group_id != str(mapping.partition_id)
        ):
            raise GraphOperationError("graph_receipt_not_owned")
        sequence = await session.scalar(
            select(func.max(GraphReceipt.sequence)).where(
                GraphReceipt.workspace_id == adm.scope.workspace_id,
                GraphReceipt.operation_id == operation.id,
            )
        )
        # Adapter strips all seed/candidate text from receipts; reject accidental leakage
        # instead of relying on a subsequent purge to remove copied narrative fields.
        if any(
            binding.node_name is not None or binding.node_summary is not None
            for binding in receipt.canonical_bindings
        ):
            raise GraphOperationError("graph_receipt_contains_seed_text")
        payload = TypeAdapter(GraphWriteReceipt).dump_python(receipt, mode="json")
        session.add(
            GraphReceipt(
                workspace_id=adm.scope.workspace_id,
                operation_id=operation.id,
                sequence=0 if sequence is None else sequence + 1,
                payload=payload,
            )
        )
        operation.phase = receipt.phase
        mapping.external_state = "unknown"
        await _commit(session, adm)


async def _inventory(
    session: AsyncSession, mapping: GraphMapping
) -> tuple[list[GraphMapping], list[GraphSupport]]:
    """Read every reservation/support in a selected bounded bucket, including historical/unknown rows."""
    rows = list(
        (
            await session.scalars(
                select(GraphMapping)
                .where(
                    GraphMapping.workspace_id == mapping.workspace_id,
                    GraphMapping.partition_id == mapping.partition_id,
                )
                .order_by(GraphMapping.id)
                .limit(101)
            )
        ).all()
    )
    if len(rows) > 100 or len({row.document_version_id for row in rows}) > 100:
        raise GraphOperationError("graph_partition_over_bound")
    supports = list(
        (
            await session.scalars(
                select(GraphSupport)
                .where(
                    GraphSupport.workspace_id == mapping.workspace_id,
                    GraphSupport.mapping_id.in_([row.id for row in rows]),
                )
                .order_by(GraphSupport.mapping_id, GraphSupport.chunk_id)
                .limit(101)
            )
        ).all()
    )
    if len(supports) > 100:
        raise GraphOperationError("graph_partition_support_over_bound")
    return rows, supports


async def _bindings(
    session: AsyncSession,
    mapping: GraphMapping,
    supports: list[GraphSupport],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> tuple[CanonicalEntityBinding, ...]:
    """Resolve exact owner memberships/seeds, assigning stable graph UUIDs only to proven canonical identities."""
    memberships = []
    by_version: dict[UUID, list[UUID]] = {}
    for support in supports:
        if not support.removed:
            by_version.setdefault(support.document_version_id, []).append(support.chunk_id)
    for version_id, chunk_ids in sorted(by_version.items(), key=lambda item: str(item[0])):
        memberships.extend(
            await entities.list_retained_version_membership_refs(
                session,
                version_id,
                chunk_ids,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        )
    if len(memberships) > 100:
        raise GraphOperationError("graph_membership_closure_over_bound")
    seeds = (
        await entities.get_temporal_node_seeds(
            session,
            [item.membership_id for item in memberships],
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if memberships
        else ()
    )
    bound_ids = {seed.entity_id for seed in seeds}
    if bound_ids != {item.entity_id for item in memberships}:
        raise GraphOperationError("graph_canonical_seed_unavailable")
    result = []
    support_by_ref = {
        (row.document_version_id, row.chunk_id): row for row in supports if not row.removed
    }
    for seed in seeds:
        evidence = []
        for member in seed.memberships:
            row = support_by_ref[(member.document_version_id, member.chunk_id)]
            evidence.append(
                EvidenceIdentity(
                    row.source_id,
                    row.source_generation,
                    row.document_id,
                    row.document_version_id,
                    row.chunk_id,
                )
            )
        fields = [("name", seed.name_hash, tuple(seed.name_support_membership_ids))]
        if seed.summary_hash is not None:
            fields.append(
                ("description", seed.summary_hash, tuple(seed.summary_support_membership_ids))
            )
        result.append(
            CanonicalEntityBinding(
                canonical_entity_id=seed.entity_id,
                canonical_revision=seed.revision,
                membership_ids=tuple(member.id for member in seed.memberships),
                evidence=tuple(sorted(set(evidence), key=str)),
                graph_entity_uuid=str(uuid5(mapping.partition_id, str(seed.entity_id))),
                group_id=str(mapping.partition_id),
                mapping_revision=mapping.desired_revision,
                node_name=seed.name,
                node_summary=seed.summary,
                field_support=tuple(fields),
            )
        )
    return tuple(result)


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


async def _finish(  # noqa: C901, PLR0912, PLR0915
    factory: async_sessionmaker[AsyncSession],
    adm: _Admission,
    operation_id: UUID,
    token: UUID,
    status: str,
    error_code: str | None,
    *,
    external_state: str | None = None,
    retry_upsert: bool = False,
) -> None:
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
        if operation is None:
            raise RuntimeError("invariant violated: operation must not be None")
        if operation.lease_owner != token:
            return
        mapping = await _get(session, GraphMapping, operation.mapping_id, adm, lock=True)
        if mapping is None:
            raise RuntimeError("invariant violated: mapping must not be None")
        current_revision = (
            mapping.desired_revision == operation.desired_revision
            and mapping.desired_digest == operation.desired_digest
        )
        operation.status, operation.error_code = status, error_code
        operation.lease_owner, operation.lease_expires_at = None, None
        operation.next_attempt_at = datetime.now(UTC) + timedelta(
            seconds=min(300, 5 * 2 ** min(operation.attempts, 5))
        )
        if partition is None:
            raise RuntimeError("invariant violated: partition must not be None")
        if partition.lease_token == token:
            if partition is None:
                raise RuntimeError("invariant violated: partition must not be None")
            partition.lease_token, partition.lease_expires_at = None, None
        if status == "reconcile_needed":
            if operation is None:
                raise RuntimeError("invariant violated: operation must not be None")
            if partition is None:
                raise RuntimeError("invariant violated: partition must not be None")
            partition.uncertain_operation_id = operation.id
            if current_revision:
                if mapping is None:
                    raise RuntimeError("invariant violated: mapping must not be None")
                mapping.status = "tombstoned" if mapping.tombstoned else "reconcile_needed"
        elif partition.uncertain_operation_id == operation.id:
            if partition is None:
                raise RuntimeError("invariant violated: partition must not be None")
            partition.uncertain_operation_id = None
        if external_state and current_revision:
            if mapping is None:
                raise RuntimeError("invariant violated: mapping must not be None")
            mapping.external_state = external_state
        if status == "succeeded" and current_revision:
            if mapping is None:
                raise RuntimeError("invariant violated: mapping must not be None")
            if operation is None:
                raise RuntimeError("invariant violated: operation must not be None")
            mapping.applied_revision, mapping.applied_digest = (
                operation.desired_revision,
                operation.desired_digest,
            )
            mapping.status = "tombstoned" if mapping.tombstoned else "synchronized"
            mapping.applied_at = datetime.now(UTC)
        elif current_revision and status != "reconcile_needed" and not mapping.tombstoned:
            # An older intent may converge after a correction queued a newer
            # desired revision. Its success cannot certify that new revision.
            if mapping is None:
                raise RuntimeError("invariant violated: mapping must not be None")
            mapping.status = "pending" if status == "succeeded" else status
        if current_revision:
            if mapping is None:
                raise RuntimeError("invariant violated: mapping must not be None")
            mapping.error_code = error_code
        if mapping is None:
            raise RuntimeError("invariant violated: mapping must not be None")
        if operation is None:
            raise RuntimeError("invariant violated: operation must not be None")
        if (
            retry_upsert
            and not mapping.tombstoned
            and mapping.desired_revision == operation.desired_revision
            and mapping.desired_digest == operation.desired_digest
        ):
            # Cleanup completion and the replacement attempt publish together;
            # an absent projection is never briefly advertised synchronized.
            if mapping is None:
                raise RuntimeError("invariant violated: mapping must not be None")
            mapping.status, mapping.applied_revision, mapping.applied_digest = "pending", 0, None
            await public._queue(session, mapping, "upsert")
        await _commit(
            session, adm, [await public.graph_status_change(session, mapping.id, scope=adm.scope)]
        )


async def _publish_clean_projection(
    factory: async_sessionmaker[AsyncSession],
    adm: _Admission,
    operation: GraphOperation,
    token: UUID,
) -> bool:
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
        if current is None:
            raise RuntimeError("invariant violated: current must not be None")
        if partition is None:
            raise RuntimeError("invariant violated: partition must not be None")
        if partition.lease_token != token or current.lease_owner != token:
            raise GraphOperationError("graph_clean_publication_lease_lost")
        if mapping is None:
            raise RuntimeError("invariant violated: mapping must not be None")
        if (
            mapping.desired_revision != operation.desired_revision
            or mapping.desired_digest != operation.desired_digest
        ):
            return False
        mapping.external_state = "absent"
        await _commit(
            session, adm, [await public.graph_status_change(session, mapping.id, scope=adm.scope)]
        )
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


async def _find_ancestor_operation(
    session: AsyncSession,
    adm: _Admission,
    operation: GraphOperation,
    mapping: GraphMapping,
) -> UUID | None:
    """Return the newest earlier uncleaned operation of this mapping that journaled receipts or dispatched.

    Read-only on the caller's session; ordering is newest first (created_at, id).
    """
    ancestor: UUID | None = await session.scalar(
        select(GraphOperation.id)
        .where(
            GraphOperation.workspace_id == adm.scope.workspace_id,
            GraphOperation.mapping_id == mapping.id,
            GraphOperation.id != operation.id,
            GraphOperation.cleanup_completed_at.is_(None),
            (
                select(GraphReceipt.id)
                .where(
                    GraphReceipt.workspace_id == adm.scope.workspace_id,
                    GraphReceipt.operation_id == GraphOperation.id,
                )
                .exists()
            )
            | GraphOperation.dispatched_at.is_not(None),
        )
        .order_by(GraphOperation.created_at.desc(), GraphOperation.id.desc())
        .limit(1)
    )
    return ancestor


async def _verify_prior_dispatches(
    graph: TemporalGraph,
    factory: async_sessionmaker[AsyncSession],
    adm: _Admission,
    session: AsyncSession,
    operation: GraphOperation,
    mapping: GraphMapping,
    context: OperationAuthorization,
) -> None:
    """Prove cessation of every unfinished earlier dispatch of this operation and journal it.

    Each prior dispatch must be verified ceased by the adapter (else ``GraphOperationUnknown``);
    its cessation is then committed in its own admitted short session.
    """
    prior_dispatches = (
        await session.scalars(
            select(GraphDispatch)
            .where(
                GraphDispatch.workspace_id == adm.scope.workspace_id,
                GraphDispatch.operation_id == operation.id,
                GraphDispatch.completed_at.is_(None),
                GraphDispatch.cessation_verified_at.is_(None),
            )
            .order_by(GraphDispatch.id)
        )
    ).all()
    for prior_dispatch in prior_dispatches:
        if mapping is None:
            raise RuntimeError("invariant violated: mapping must not be None")
        if operation is None:
            raise RuntimeError("invariant violated: operation must not be None")
        previous = DispatchOwnership(
            operation.id,
            str(mapping.partition_id),
            prior_dispatch.server_run_id,
            prior_dispatch.client_id,
        )
        if not await graph.verify_dispatch_cessation(previous, context):
            raise GraphOperationUnknown("graph_prior_dispatch_still_unknown")
        async with factory() as stopped:
            await _admit(stopped, adm)
            if operation is None:
                raise RuntimeError("invariant violated: operation must not be None")
            current = await _get(stopped, GraphOperation, operation.id, adm, lock=True)
            if current is None:
                raise RuntimeError("invariant violated: current must not be None")
            current.cessation_verified_at = datetime.now(UTC)
            current.cessation_reason = "owned_sync_client_killed"
            journal = await _get(stopped, GraphDispatch, prior_dispatch.id, adm, lock=True)
            if journal is None:
                raise RuntimeError("invariant violated: journal must not be None")
            journal.cessation_verified_at = datetime.now(UTC)
            journal.cessation_reason = "owned_sync_client_killed"
            await _commit(stopped, adm)


async def _project_current_episode(
    graph: TemporalGraph,
    factory: async_sessionmaker[AsyncSession],
    adm: _Admission,
    session: AsyncSession,
    ctx: dict[str, object],
    operation: GraphOperation,
    mapping: GraphMapping,
    token: UUID,
) -> None:
    """Publish a clean projection, or upsert the current episode and publish its revision.

    Terminal outcomes (clean projection, tombstoned mapping) finish early; otherwise the
    episode is written inside a dispatch session and published only after owner locks and
    a canonical-binding recheck, with every fence still held while ``_finish`` commits.
    """
    if not await _publish_clean_projection(factory, adm, operation, token):
        if operation is None:
            raise RuntimeError("invariant violated: operation must not be None")
        await _finish(factory, adm, operation.id, token, "succeeded", None)
        return
    await session.refresh(mapping)
    if mapping is None:
        raise RuntimeError("invariant violated: mapping must not be None")
    if mapping.tombstoned:
        if operation is None:
            raise RuntimeError("invariant violated: operation must not be None")
        await _finish(factory, adm, operation.id, token, "succeeded", None, external_state="absent")
        return
    context, gateway, _rows = await _context(
        factory, adm, session, ctx, operation, mapping, token, cleanup=False
    )
    if mapping is None:
        raise RuntimeError("invariant violated: mapping must not be None")
    data = await documents.read_extraction_input(
        session,
        mapping.document_version_id,
        scope=adm.scope,
        multi_workspace_enabled=adm.multi,
    )
    if data is None:
        raise GraphOperationError("graph_document_no_longer_ready")
    content = "\n\n".join(chunk.content for chunk in data.chunks)
    if operation is None:
        raise RuntimeError("invariant violated: operation must not be None")
    request = EpisodeRequest(
        mapping.episode_id,
        str(mapping.partition_id),
        "Evidence-backed document",
        content,
        data.observed_at,
        context.evidence,
        tuple(binding.canonical_entity_id for binding in context.canonical_bindings),
        operation.desired_revision,
        context.canonical_bindings,
    )
    async with graph.dispatch_session(context):
        await graph.upsert_episode(request, context, gateway)
    # The awaited synchronous command transport completed; publication still
    # needs owner locks and revision checks after remote inference.
    await entities.lock_entity_ids(
        session,
        [binding.canonical_entity_id for binding in context.canonical_bindings],
        scope=adm.scope,
        multi_workspace_enabled=adm.multi,
    )
    event_refs = await timeline.temporal_event_refs(
        session, [mapping.document_version_id], scope=adm.scope, multi_workspace_enabled=adm.multi
    )
    await timeline.lock_event_ids(
        session,
        sorted({item["event_id"] for item in event_refs}, key=str),
        scope=adm.scope,
        multi_workspace_enabled=adm.multi,
    )
    current_bindings = tuple(
        replace(binding, mapping_revision=operation.desired_revision)
        for binding in await _bindings(
            session,
            mapping,
            [
                item
                for item in await session.scalars(
                    select(GraphSupport).where(
                        GraphSupport.workspace_id == adm.scope.workspace_id,
                        GraphSupport.mapping_id == mapping.id,
                        GraphSupport.removed.is_(False),
                    )
                )
            ],
            scope=adm.scope,
            multi_workspace_enabled=adm.multi,
        )
    )
    if current_bindings != context.canonical_bindings:
        raise GraphOperationUnknown("graph_canonical_revision_changed_before_publication")
    # Source/document/entity/event fences remain held while the
    # independent projection transaction publishes its revision.
    # Releasing them first lets a correction invalidate this result.
    await _finish(factory, adm, operation.id, token, "succeeded", None, external_state="present")


async def _run_operation(
    graph: TemporalGraph,
    factory: async_sessionmaker[AsyncSession],
    adm: _Admission,
    ctx: dict[str, object],
    operation_id: UUID,
    token: UUID,
) -> None:
    """Run one claimed, admitted, graph-READY operation: cleanup scope, recovery or the current projection.

    Opens the owner session, proves cessation of earlier dispatches, then either recovers an
    existing journal, recovers an ancestor operation, or projects the current episode.
    """
    async with factory() as session:
        await _admit(session, adm)
        operation = await _get(session, GraphOperation, operation_id, adm)
        if operation is None:
            raise RuntimeError("invariant violated: operation must not be None")
        mapping = await _get(session, GraphMapping, operation.mapping_id, adm)
        if mapping is None:
            raise RuntimeError("invariant violated: mapping must not be None")
        ancestor = await _find_ancestor_operation(session, adm, operation, mapping)
        # Establish cessation/absence before model identity checks. A
        # cleaned old identity must not prevent a new model rebuild;
        # inference receives a separate current-policy scope below.
        context, _gateway, rows = await _context(
            factory, adm, session, ctx, operation, mapping, token, cleanup=True
        )
        await _verify_prior_dispatches(graph, factory, adm, session, operation, mapping, context)
        if context.receipt_history:
            await _recover(
                graph, factory, adm, session, ctx, operation, mapping, token, context, rows
            )
            return
        if ancestor is not None:
            await _recover_ancestor(
                graph, factory, adm, session, ctx, ancestor, operation, mapping, token
            )
            return
        await _project_current_episode(graph, factory, adm, session, ctx, operation, mapping, token)


async def process_graph_operation(ctx: dict[str, object], operation_id_value: str) -> None:
    """Execute one claimed projection while capacity owns publication and local teardown.

    Source fences and exact receipts govern remote recovery. Cancellation records
    uncertainty and joins local cleanup before releasing the heavy transaction;
    a lost database connection can release capacity earlier and proves no remote
    cessation. Contention retries without a domain claim.
    """
    factory = worker_session_factory(ctx)
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
                    if current is None:
                        raise RuntimeError("invariant violated: current must not be None")
                    fingerprint = await _dependency_fingerprint(
                        dependency, ctx, state, adm, current.partition_id
                    )
                    current.dependency_fingerprint = fingerprint
                    await _commit(dependency, adm)
                if state != GraphState.READY:
                    await _finish(
                        factory, adm, operation_id, token, "blocked", "graph_" + str(state)
                    )
                    return
                await _run_operation(graph, factory, adm, ctx, operation_id, token)
            except (GraphOperationUnknown, asyncio.CancelledError, TimeoutError) as exc:
                if isinstance(exc, (asyncio.CancelledError, TimeoutError)):
                    interrupted = exc
                if token:
                    await _join_local_cleanup(
                        _finish(
                            factory,
                            adm,
                            operation_id,
                            token,
                            "reconcile_needed",
                            "graph_outcome_unknown",
                        )
                    )
            except (GraphOperationError, ValueError, LookupError, PrivacyPolicyDenied) as exc:
                if token:
                    await _join_local_cleanup(
                        _finish(
                            factory,
                            adm,
                            operation_id,
                            token,
                            "blocked",
                            str(exc)
                            if str(exc).startswith("graph_")
                            else "graph_owner_proof_unavailable",
                        )
                    )
            finally:
                await _join_local_cleanup(graph.close())
    except HeavyWorkBusy as exc:
        raise retry_with_backoff(
            ctx, base=HEAVY_RETRY_DEFER_SECONDS, cap=HEAVY_RETRY_CAP_SECONDS
        ) from exc
    except RemoteHeavyWorkBlocked as exc:
        raise retry_with_backoff(
            ctx, base=HEAVY_BLOCKED_BASE_SECONDS, cap=HEAVY_BLOCKED_CAP_SECONDS
        ) from exc
    except (HTTPException, DomainError):
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
            terminal = {
                str(row.episode_id)
                for row in rows
                if row.tombstoned
                and row.external_state == "absent"
                and row.applied_revision == row.desired_revision
                and row.applied_digest == row.desired_digest
                and row.error_code is None
            }
            remaining -= terminal
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


async def _complete_recovery(
    graph: TemporalGraph,
    factory: async_sessionmaker[AsyncSession],
    adm: _Admission,
    operation: GraphOperation,
    mapping: GraphMapping,
    token: UUID,
    context: OperationAuthorization,
    receipts: tuple[GraphWriteReceipt, ...],
    inspection: GraphReceiptInspection,
    *,
    cleanup_only: bool,
) -> None:
    """Remove the original episode (unless it never existed) and publish the terminal recovery state.

    A canonical-node-only journal with nothing physically present needs no episode delete.
    An unsuccessful delete leaves the operation ``reconcile_needed``; success records cleanup
    completion and, outside ``cleanup_only``, either finishes a tombstone or queues the current
    desired state as a new attempt.
    """
    canonical_only = all(
        receipt.phase == "canonical_node_write_intent"
        or (receipt.phase == "cleanup_write_intent" and not receipt.prior_episode_state_fingerprint)
        for receipt in receipts
    )
    if (
        canonical_only
        and not inspection.episode_present
        and not inspection.fact_ids_present
        and not inspection.mention_ids_present
    ):
        succeeded, removed = True, True
    else:
        async with graph.dispatch_session(context):
            outcome = await graph.delete_episode(mapping.episode_id, context, True, True)
        succeeded, removed = outcome.outcome == "succeeded", outcome.removed
    if not succeeded:
        await _finish(
            factory,
            adm,
            operation.id,
            token,
            "reconcile_needed",
            "graph_current_recovery_pending",
            external_state="absent" if removed else "unknown",
        )
        return
    async with factory() as journal:
        await _admit(journal, adm)
        op_row = await _get(journal, GraphOperation, operation.id, adm, lock=True)
        if op_row is None:
            raise RuntimeError("invariant violated: op_row must not be None")
        op_row.cleanup_completed_at = datetime.now(UTC)
        await _commit(journal, adm)
    if cleanup_only:
        return
    if mapping.tombstoned:
        await _finish(factory, adm, operation.id, token, "succeeded", None, external_state="absent")
    else:
        # Cleanup converged; retain stable episode UUID and queue the current desired
        # canonical state under a new attempt, keeping every prior receipt immutable.
        await _finish(
            factory,
            adm,
            operation.id,
            token,
            "succeeded",
            None,
            external_state="absent",
            retry_upsert=True,
        )


async def _recover(
    graph: TemporalGraph,
    factory: async_sessionmaker[AsyncSession],
    adm: _Admission,
    session: AsyncSession,
    ctx: dict[str, object],
    operation: GraphOperation,
    mapping: GraphMapping,
    token: UUID,
    context: OperationAuthorization,
    rows: list[GraphMapping],
    *,
    cleanup_only: bool = False,
) -> None:
    """Inspect an original journal and recover typed effects under current owner fences.

    A fully matching current upsert may publish its exact readback. Ancestor
    cleanup always removes that original episode and records cleanup completion,
    even when its old fields match the current desired snapshot. Shared effects
    use fresh canonical replacements or durable dependent rebuild obligations;
    every adapter outcome must converge before terminal delete/retry publication.
    """
    receipts = context.receipt_history
    actions = _initial_node_actions(context, operation)
    async with graph.dispatch_session(context):
        inspection = await graph.inspect_write_receipt(receipts, context, actions)
    # A fully matching interrupted upsert has already converged. Publish its
    # exact readback rather than erase/re-extract an unchanged desired snapshot.
    bulk = next(
        (receipt for receipt in reversed(receipts) if receipt.phase == "bulk_write_intent"), None
    )
    if (
        not cleanup_only
        and bulk is not None
        and not mapping.tombstoned
        and not any(receipt.phase == "cleanup_write_intent" for receipt in receipts)
        and _readback_matches_desired(mapping, operation, context, receipts, inspection, bulk)
    ):
        await context.validate_partition("upsert", str(mapping.episode_id))
        await _finish(
            factory, adm, operation.id, token, "succeeded", None, external_state="present"
        )
        return
    async with factory() as refresh:
        await _admit(refresh, adm)
        receipts, aggregate = await _receipt_inventory(
            refresh, operation.id, adm=adm, inspection=inspection
        )
    context = replace(context, receipt_history=receipts, receipt_aggregate=aggregate)
    actual_actions = _bind_node_actions(actions, inspection)
    actual_actions = await _prepare_node_actions(
        factory, adm, operation, mapping, token, rows, actual_actions
    )
    # A target-only orphan still has its own episode links. Delete the episode
    # first; its persisted cleanup lets the next bounded pass remove that node.
    repaired_actions = tuple(
        action
        for action in actual_actions
        if action.action != "delete_orphan" or not action.expected_incident_links
    )
    if repaired_actions:
        async with graph.dispatch_session(context):
            node_outcome = await graph.reconcile_canonical_nodes(
                receipts, repaired_actions, context
            )
        if not node_outcome.converged or node_outcome.unresolved_ids:
            await _finish(
                factory, adm, operation.id, token, "reconcile_needed", "graph_node_recovery_pending"
            )
            return
    async with factory() as refresh:
        await _admit(refresh, adm)
        receipts, aggregate = await _receipt_inventory(refresh, operation.id, adm=adm)
    context = replace(
        context,
        receipt_history=receipts,
        receipt_aggregate=aggregate,
        node_recovery_actions=actual_actions,
    )
    # Node repair may remove an incident fact or change its fingerprint. Build
    # every subsequent fact action from a new exact readback of the new journal.
    async with graph.dispatch_session(context):
        inspection = await graph.inspect_write_receipt(receipts, context, actual_actions)
    async with factory() as refresh:
        await _admit(refresh, adm)
        receipts, aggregate = await _receipt_inventory(
            refresh, operation.id, adm=adm, inspection=inspection
        )
    final_node_states = {
        identifier: fingerprint
        for receipt in receipts
        for identifier, fingerprint in receipt.intended_entity_state_fingerprints
    }
    inspected_nodes = dict(inspection.current_entity_state_fingerprints)
    if any(
        action.graph_entity_uuid not in final_node_states
        or inspected_nodes.get(action.graph_entity_uuid)
        != final_node_states[action.graph_entity_uuid]
        for action in repaired_actions
    ):
        await _finish(
            factory,
            adm,
            operation.id,
            token,
            "reconcile_needed",
            "graph_node_final_readback_changed",
        )
        return
    fact_actions = await _build_fact_actions(
        factory, adm, session, ctx, operation, mapping, token, context, rows, receipts, inspection
    )
    context = replace(
        context,
        receipt_history=receipts,
        receipt_aggregate=aggregate,
        node_recovery_actions=actual_actions,
        fact_recovery_actions=tuple(fact_actions),
    )
    applicable = receipts
    if fact_actions:
        async with graph.dispatch_session(context):
            fact_outcome = await graph.reconcile_fact_recovery(
                applicable, tuple(fact_actions), context
            )
        if not fact_outcome.converged or fact_outcome.unresolved_ids:
            await _finish(
                factory, adm, operation.id, token, "reconcile_needed", "graph_fact_recovery_pending"
            )
            return
        async with factory() as refresh:
            await _admit(refresh, adm)
            history, aggregate = await _receipt_inventory(refresh, operation.id, adm=adm)
            context = replace(context, receipt_history=history, receipt_aggregate=aggregate)
    await _complete_recovery(
        graph,
        factory,
        adm,
        operation,
        mapping,
        token,
        context,
        receipts,
        inspection,
        cleanup_only=cleanup_only,
    )


async def _recover_ancestor(
    graph: TemporalGraph,
    factory: async_sessionmaker[AsyncSession],
    adm: _Admission,
    session: AsyncSession,
    ctx: dict[str, object],
    ancestor_id: UUID,
    intent: GraphOperation,
    mapping: GraphMapping,
    token: UUID,
) -> None:
    """Clean one original operation journal per job without copying its receipt identity into a newer intent.

    Current partition ownership fences old tasks; the original receipt token,
    revision and immutable IDs remain unchanged. A durable completion marker
    advances lifetime history across retries instead of loading it in memory.
    """
    async with factory() as claim:
        await _admit(claim, adm)
        partition = await _get(claim, GraphPartition, mapping.partition_id, adm, lock=True)
        ancestor = await _get(claim, GraphOperation, ancestor_id, adm, lock=True)
        if ancestor is None:
            raise RuntimeError("invariant violated: ancestor must not be None")
        if partition is None:
            raise RuntimeError("invariant violated: partition must not be None")
        if partition.lease_token != token or ancestor.cleanup_completed_at is not None:
            return
        ancestor.lease_owner, ancestor.lease_expires_at = token, partition.lease_expires_at
        await _commit(claim, adm)
    mapping_id, intent_id = mapping.id, intent.id
    session.expire_all()
    await _admit(session, adm)
    fresh_ancestor = await _get(session, GraphOperation, ancestor_id, adm)
    fresh_mapping = await _get(session, GraphMapping, mapping_id, adm)
    if fresh_ancestor is None or fresh_mapping is None:
        raise RuntimeError("invariant violated: fresh_ancestor and fresh_mapping must not be None")
    ancestor, mapping = fresh_ancestor, fresh_mapping
    context, _gateway, rows = await _context(
        factory, adm, session, ctx, ancestor, mapping, token, cleanup=True
    )
    prior = (
        await session.scalars(
            select(GraphDispatch).where(
                GraphDispatch.workspace_id == adm.scope.workspace_id,
                GraphDispatch.operation_id == ancestor_id,
                GraphDispatch.completed_at.is_(None),
                GraphDispatch.cessation_verified_at.is_(None),
            )
        )
    ).all()
    for dispatch in prior:
        if ancestor is None:
            raise RuntimeError("invariant violated: ancestor must not be None")
        ownership = DispatchOwnership(
            ancestor.id, str(mapping.partition_id), dispatch.server_run_id, dispatch.client_id
        )
        if not await graph.verify_dispatch_cessation(ownership, context):
            raise GraphOperationUnknown("graph_ancestor_dispatch_unknown")
        async with factory() as journal:
            await _admit(journal, adm)
            stopped = await _get(journal, GraphDispatch, dispatch.id, adm, lock=True)
            if stopped is None:
                raise RuntimeError("invariant violated: stopped must not be None")
            stopped.cessation_verified_at, stopped.cessation_reason = (
                datetime.now(UTC),
                "owned_sync_client_killed",
            )
            await _commit(journal, adm)
    if context.receipt_history:
        await _recover(
            graph,
            factory,
            adm,
            session,
            ctx,
            ancestor,
            mapping,
            token,
            context,
            rows,
            cleanup_only=True,
        )
    else:
        # Dispatch setup may create indexes, but every logical node/episode/fact
        # write awaits its durable prewrite receipt. No receipt plus exact prior
        # cessation proves this original actor owns no logical cleanup effects.
        async with factory() as journal:
            await _admit(journal, adm)
            if ancestor is None:
                raise RuntimeError("invariant violated: ancestor must not be None")
            cleaned = await _get(journal, GraphOperation, ancestor.id, adm, lock=True)
            if cleaned is None:
                raise RuntimeError("invariant violated: cleaned must not be None")
            cleaned.cleanup_completed_at = datetime.now(UTC)
            await _commit(journal, adm)
    async with factory() as progress:
        await _admit(progress, adm)
        completed = await _get(progress, GraphOperation, ancestor_id, adm)
        if completed is None:
            raise RuntimeError("invariant violated: completed must not be None")
        succeeded = completed.cleanup_completed_at is not None
    if not succeeded:
        raise GraphOperationUnknown("graph_ancestor_cleanup_pending")
    await _finish(
        factory, adm, intent_id, token, "reconcile_needed", "graph_ancestor_cleanup_continuation"
    )


async def _recovery_workspaces(
    redis: ArqRedis, factory: async_sessionmaker[AsyncSession]
) -> list[UUID]:
    """Page workspaces that currently have due work, fairly, by a keyset cursor over workspace IDs.

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
        and_(
            GraphOperation.status.in_(["pending", "reconcile_needed", "running"]),
            GraphOperation.next_attempt_at <= now,
            or_(GraphOperation.lease_expires_at.is_(None), GraphOperation.lease_expires_at <= now),
        ),
    )
    candidates = (
        select(GraphOperation.workspace_id.label("workspace_id"))
        .where(due)
        .union(
            select(GraphReconcileRun.workspace_id.label("workspace_id")).where(
                GraphReconcileRun.status.in_(["pending", "running"])
            )
        )
        .subquery()
    )
    page: list[UUID] = []
    for lower in (after, None) if after is not None else (None,):
        async with factory() as session:
            query = (
                select(candidates.c.workspace_id)
                .order_by(candidates.c.workspace_id)
                .limit(RECOVERY_WORKSPACE_PAGE)
            )
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
    """Recover due intents/runs one admitted workspace per transaction; Redis loss never deletes work.

    Each workspace is admitted through the access fence before any lock, then handled in its own
    short transactions (25 due rows). Denied, disabled or revoked workspaces are skipped without
    mutation. The graph is probed only after a workspace with blocked work is admitted.
    """
    factory = worker_session_factory(ctx)
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
                blocked = (
                    await session.scalars(
                        select(GraphOperation.id)
                        .where(
                            GraphOperation.workspace_id == workspace_id,
                            GraphOperation.status == "blocked",
                            GraphOperation.next_attempt_at <= now,
                        )
                        .order_by(GraphOperation.next_attempt_at, GraphOperation.id)
                        .limit(25)
                    )
                ).all()
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
                    if locator is None or dependency_state is None:
                        raise RuntimeError(
                            "invariant violated: locator and dependency_state must not be None"
                        )
                    fingerprint = await _dependency_fingerprint(
                        session, ctx, dependency_state, adm, locator.partition_id
                    )
                    if locator.dependency_fingerprint == fingerprint:
                        continue
                    partition = await _get(
                        session, GraphPartition, locator.partition_id, adm, lock=True
                    )
                    operation = await _get(session, GraphOperation, blocked_id, adm, lock=True)
                    if operation is None:
                        raise RuntimeError("invariant violated: operation must not be None")
                    if partition is None:
                        raise RuntimeError("invariant violated: partition must not be None")
                    if operation.status == "blocked" and (
                        partition.lease_expires_at is None or partition.lease_expires_at <= now
                    ):
                        operation.status, operation.attempts = "pending", 0
                        operation.dependency_fingerprint = fingerprint
                        mapping = await _get(
                            session, GraphMapping, operation.mapping_id, adm, lock=True
                        )
                        if mapping is None:
                            raise RuntimeError("invariant violated: mapping must not be None")
                        if (
                            mapping.desired_revision == operation.desired_revision
                            and mapping.desired_digest == operation.desired_digest
                        ):
                            mapping.status, mapping.error_code = "pending", None
                            await _commit(
                                session,
                                adm,
                                [
                                    await public.graph_status_change(
                                        session, mapping.id, scope=adm.scope
                                    )
                                ],
                            )
                operations = (
                    await session.scalars(
                        select(GraphOperation.id)
                        .where(
                            GraphOperation.workspace_id == workspace_id,
                            GraphOperation.status.in_(["pending", "reconcile_needed", "running"]),
                            GraphOperation.next_attempt_at <= now,
                            (GraphOperation.lease_expires_at.is_(None))
                            | (GraphOperation.lease_expires_at <= now),
                        )
                        .order_by(GraphOperation.next_attempt_at, GraphOperation.id)
                        .limit(25)
                    )
                ).all()
                runs = (
                    await session.scalars(
                        select(GraphReconcileRun.id)
                        .where(
                            GraphReconcileRun.workspace_id == workspace_id,
                            GraphReconcileRun.status.in_(["pending", "running"]),
                        )
                        .order_by(GraphReconcileRun.created_at)
                        .limit(25)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
                for run_id in runs:
                    await public.reconcile_slice(
                        session, run_id, scope=adm.scope, multi_workspace_enabled=adm.multi
                    )
                await _commit(session, adm)
        except (HTTPException, DomainError):
            continue  # authority changed mid-pass: skip this workspace, no rebase
        for operation_id in operations:
            await redis.enqueue_job(
                "process_graph_operation", str(operation_id), _job_id="graph:" + str(operation_id)
            )
        enqueued += len(operations)
    return enqueued
