"""Graph recovery of unknown operations and the scheduled recovery job.

Private implementation of ``modules.knowledge.temporal.worker``; import from that facade.
"""

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from arq.connections import ArqRedis
from fastapi import HTTPException
from redis.exceptions import RedisError
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.errors import DomainError
from core.worker_context import worker_session_factory
from modules.knowledge.temporal import public
from modules.knowledge.temporal._worker_actions import (
    _bind_node_actions,
    _build_fact_actions,
    _initial_node_actions,
    _prepare_node_actions,
    _readback_matches_desired,
)
from modules.knowledge.temporal._worker_admission import (
    RECOVERY_CURSOR_KEY,
    RECOVERY_WORKSPACE_PAGE,
    _Admission,
    _admit,
    _commit,
    _dependency_fingerprint,
    _get,
    admit_workspace,
)
from modules.knowledge.temporal._worker_finish import (
    _finish,
)
from modules.knowledge.temporal._worker_journal import (
    _receipt_inventory,
)
from modules.knowledge.temporal._worker_owner import (
    _context,
)
from modules.knowledge.temporal.adapter import (
    DispatchOwnership,
    GraphConfiguration,
    GraphOperationUnknown,
    GraphReceiptInspection,
    GraphState,
    GraphWriteReceipt,
    OperationAuthorization,
    TemporalGraph,
)
from modules.knowledge.temporal.models import (
    GraphDispatch,
    GraphMapping,
    GraphOperation,
    GraphPartition,
    GraphReconcileRun,
)


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


async def _verify_ancestor_dispatches(
    graph: TemporalGraph,
    factory: async_sessionmaker[AsyncSession],
    adm: _Admission,
    context: Any,
    ancestor: GraphOperation,
    mapping: GraphMapping,
    prior: Sequence[GraphDispatch],
) -> None:
    """Prove each unfinished ancestor dispatch has ceased and journal the cessation.

    For every dispatch in ``prior`` (in order) asks the graph to verify exact cessation, raising
    ``GraphOperationUnknown`` if unproven, then records ``cessation_verified_at`` under a row lock
    in its own committed journal session.
    """
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
    await _verify_ancestor_dispatches(graph, factory, adm, context, ancestor, mapping, prior)
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


async def _requeue_blocked_operations(
    session: AsyncSession,
    ctx: dict[str, object],
    adm: _Admission,
    blocked: Sequence[UUID],
    dependency_state: GraphState | None,
    now: datetime,
) -> None:
    """Return blocked operations to pending once their dependency fingerprint changed.

    Per operation: reads the locator, computes the current dependency fingerprint, then (partition
    first, then operation, then mapping, matching execution lock order) requeues it only if it is
    still blocked and its partition lease has lapsed. A mapping still at the operation's desired
    revision is marked pending and committed with its status change. Raises ``RuntimeError`` on
    missing rows or an unprobed dependency state.
    """
    for blocked_id in blocked:
        # Retry admission uses the same row order as execution. Mapping is
        # deliberately untouched; any uncertainty retains its exact ledger.
        locator = await _get(session, GraphOperation, blocked_id, adm)
        if locator is None or dependency_state is None:
            raise RuntimeError("invariant violated: locator and dependency_state must not be None")
        fingerprint = await _dependency_fingerprint(
            session, ctx, dependency_state, adm, locator.partition_id
        )
        if locator.dependency_fingerprint == fingerprint:
            continue
        partition = await _get(session, GraphPartition, locator.partition_id, adm, lock=True)
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
            mapping = await _get(session, GraphMapping, operation.mapping_id, adm, lock=True)
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
                    [await public.graph_status_change(session, mapping.id, scope=adm.scope)],
                )


async def _due_operations_and_runs(
    session: AsyncSession, workspace_id: UUID, now: datetime
) -> tuple[Sequence[UUID], Sequence[UUID]]:
    """Select up to 25 due operation ids and 25 pending reconcile runs for one workspace.

    Operations are unleased (or lease-expired) pending/reconcile/running intents due now, oldest
    first; runs are locked with ``SKIP LOCKED`` so concurrent recovery passes never share one.
    """
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
    return operations, runs


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
                await _requeue_blocked_operations(session, ctx, adm, blocked, dependency_state, now)
                operations, runs = await _due_operations_and_runs(session, workspace_id, now)
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
