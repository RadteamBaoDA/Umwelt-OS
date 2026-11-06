"""Episode projection and the ``process_graph_operation`` arq job.

Private implementation of ``modules.knowledge.temporal.worker``; import from that facade.
"""

import asyncio
from dataclasses import replace
from typing import cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.errors import DomainError
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
from core.model_gateway.client import PrivacyPolicyDenied
from core.worker_context import worker_session_factory
from core.worker_retry import retry_with_backoff
from modules.knowledge.documents import public as documents
from modules.knowledge.entities import public as entities
from modules.knowledge.temporal._worker_admission import (
    OWNER_BUDGET_SECONDS,
    _Admission,
    _admit,
    _admit_job,
    _claim,
    _commit,
    _dependency_fingerprint,
    _get,
)
from modules.knowledge.temporal._worker_finish import (
    _find_ancestor_operation,
    _finish,
    _join_local_cleanup,
    _publish_clean_projection,
    _verify_prior_dispatches,
)
from modules.knowledge.temporal._worker_journal import (
    _bindings,
)
from modules.knowledge.temporal._worker_owner import (
    _context,
)
from modules.knowledge.temporal._worker_recover import (
    _recover,
    _recover_ancestor,
)
from modules.knowledge.temporal.adapter import (
    EpisodeRequest,
    GraphConfiguration,
    GraphOperationError,
    GraphOperationUnknown,
    GraphState,
    TemporalGraph,
)
from modules.knowledge.temporal.models import (
    GraphMapping,
    GraphOperation,
    GraphSupport,
)
from modules.timeline import public as timeline


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
