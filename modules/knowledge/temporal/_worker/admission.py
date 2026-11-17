"""Admission fence, claim/lease and dependency fingerprint helpers for temporal graph operations.

Private implementation of ``modules.knowledge.temporal.worker``; import from that facade.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.errors import ConflictError, DomainError
from core.ids import uuid7
from core.job_denial import admit_retry_stale, denial_code, terminalize
from core.realtime import commit_with_replay
from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.knowledge.temporal import public
from modules.knowledge.temporal.adapter import (
    GraphOperationError,
    GraphState,
)
from modules.knowledge.temporal.models import (
    GraphMapping,
    GraphOperation,
    GraphPartition,
    GraphRebuildDependency,
)
from modules.settings import public as settings_public
from modules.sources import public as sources

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
