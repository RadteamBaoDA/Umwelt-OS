"""Operation finishing, clean projection publication and prior-dispatch verification for temporal graph operations.

Private implementation of ``modules.knowledge.temporal.worker``; import from that facade.
"""

import asyncio
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modules.knowledge.temporal import public
from modules.knowledge.temporal._worker_admission import (
    _Admission,
    _admit,
    _commit,
    _get,
)
from modules.knowledge.temporal.adapter import (
    DispatchOwnership,
    GraphOperationError,
    GraphOperationUnknown,
    OperationAuthorization,
    TemporalGraph,
)
from modules.knowledge.temporal.models import (
    GraphDispatch,
    GraphMapping,
    GraphOperation,
    GraphPartition,
    GraphReceipt,
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
