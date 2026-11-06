"""Ingestion stage claim, failure, retry and completion plus the process_ingestion_event job."""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime, timedelta
from uuid import UUID

from arq import Retry
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.realtime import (
    make_source_change,
)
from core.telemetry import set_trace, timed
from core.worker_context import worker_multi_workspace_flag as _worker_flag
from core.worker_context import worker_session_factory
from modules.connectors import public as connectors
from modules.ingestion import public as ingestion_api
from modules.ingestion._worker_collect import (
    ConnectorRetryError,
    _collect_web_job,
    _commit_ingestion_change,
    _refresh_run_status,
    _update_native_run_lease,
    _wake_dispatcher_after_commit,
)
from modules.ingestion._worker_lock import _lock_worker_event
from modules.ingestion._worker_state import (
    MAX_STAGE_ATTEMPTS,
    STAGE_TIMEOUT_SECONDS,
    WorkerClaim,
    WorkerState,
    _capture_worker_claim,
    logger,
)
from modules.ingestion.models import (
    COLLECTION_LEASE,
    EventOutbox,
    SourceObservation,
)
from modules.sources import public as sources


async def _fail_ingestion_stage(
    factory: async_sessionmaker[AsyncSession],
    event_id: UUID,
    run_id: UUID,
    stage_id: UUID,
    error_code: str,
    *,
    claim: WorkerClaim,
    multi_workspace_enabled: bool,
) -> None:
    """Fail only the original admitted dispatch/stage attempt, with flush-only Source health.

    Lost access/Source or a successor claim produces no cleanup using another principal.
    Earlier locks are acquired once by the scoped loader before state/run/stage/outbox.
    """
    async with factory() as session:
        work = await _lock_worker_event(
            session,
            event_id,
            multi_workspace_enabled=multi_workspace_enabled,
            event_types=(claim.event_type,),
            expected=claim,
        )
        if work is None or work.run.id != run_id or work.stage.id != stage_id:
            return
        work.stage.status = "failed"
        work.stage.error_code = error_code
        work.stage.lease_expires_at = None
        work.run.status = "failed"
        work.run.error_code = error_code
        work.event.status = "failed"
        logger.warning(
            "Ingestion stage failed run_id=%s stage_id=%s error_code=%s",
            work.run.id,
            work.stage.id,
            error_code,
        )
        changed = await sources.record_collection_result_in_uow(
            session,
            work.source.id,
            work.source.generation,
            datetime.now(UTC),
            error_code,
            scope=work.scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=work.access_fence,
            source_fence=work.source,
        )
        _update_native_run_lease(work.state, work.run.id, terminal=True)
        extras = (
            (
                make_source_change(
                    work.source.id, work.source.generation, work.source.status, scope=work.scope
                ),
            )
            if changed
            else ()
        )
        await _commit_ingestion_change(
            session,
            work.run,
            work.stage,
            extras,
            scope=work.scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=work.access_fence,
        )


async def _ingestion_enabled(session: AsyncSession, work: WorkerState, enabled: bool) -> bool:
    """Per-workspace module gate after admission; disabled leaves the durable event untouched."""
    from modules.settings.public import module_is_enabled

    if await module_is_enabled(
        session, "ingestion", scope=work.scope, multi_workspace_enabled=enabled
    ):
        return True
    await session.rollback()
    return False


async def _claim_ingestion_stage(
    factory: async_sessionmaker[AsyncSession],
    identifier: UUID,
    enabled: bool,
    event_types: tuple[str, ...],
) -> tuple[WorkerClaim, EventOutbox, UUID, UUID] | None:
    """Commit the stage's running lease in one transaction and return the detached claim, or ``None``.

    ``None`` means there is nothing to run: no work, a succeeded stage (event delivered here), a failed or
    still-leased stage, an exhausted attempt budget or a lost source lease (both settled through
    ``_fail_ingestion_stage`` after rolling this transaction back). On success the stage is running with a
    fresh lease, the attempt counter is incremented, the native source lease is renewed and the change is
    committed with replay before any external work.
    """
    async with factory() as session:
        work = await _lock_worker_event(
            session, identifier, multi_workspace_enabled=enabled, event_types=event_types
        )
        if work is None or not await _ingestion_enabled(session, work, enabled):
            return None
        run, stage, event, state = work.run, work.stage, work.event, work.state
        now = datetime.now(UTC)
        if stage.status == "succeeded":
            await ingestion_api.mark_event_delivered(
                session, identifier, scope=work.scope, multi_workspace_enabled=enabled
            )
            await session.commit()
            return None
        if stage.status == "failed" or (
            stage.status == "running" and stage.lease_expires_at and stage.lease_expires_at > now
        ):
            return None
        if stage.attempts >= MAX_STAGE_ATTEMPTS:
            claim = _capture_worker_claim(work)
            await session.rollback()
            await _fail_ingestion_stage(
                factory,
                identifier,
                claim.run_id,
                claim.stage_id,
                "retry_exhausted",
                claim=claim,
                multi_workspace_enabled=enabled,
            )
            return None
        if (
            state is None
            or state.lease_run_id != run.id
            or state.lease_expires_at is None
            or state.lease_expires_at <= now
        ):
            claim = _capture_worker_claim(work)
            await session.rollback()
            await _fail_ingestion_stage(
                factory,
                identifier,
                claim.run_id,
                claim.stage_id,
                "lease_expired",
                claim=claim,
                multi_workspace_enabled=enabled,
            )
            return None
        stage.status = "running"
        stage.attempts += 1
        stage.lease_expires_at = now + timedelta(seconds=STAGE_TIMEOUT_SECONDS)
        stage.error_code = None
        state.lease_expires_at = now + COLLECTION_LEASE
        run.status = "running"
        claim = _capture_worker_claim(work)
        run_id, stage_id = run.id, stage.id
        set_trace(ingestion_run_id=str(run_id))
        logger.info(
            "Ingestion stage started run_id=%s stage_id=%s attempt=%s",
            run_id,
            stage_id,
            stage.attempts,
        )
        await _commit_ingestion_change(
            session,
            run,
            stage,
            scope=work.scope,
            multi_workspace_enabled=enabled,
            access_fence=work.access_fence,
        )
    return claim, event, run_id, stage_id


async def _schedule_transient_retry(
    factory: async_sessionmaker[AsyncSession],
    identifier: UUID,
    enabled: bool,
    event_types: tuple[str, ...],
    claim: WorkerClaim,
    exc: Exception,
) -> float | None:
    """Schedule a bounded retry after a transient failure under the original claim; return the defer delay.

    ``None`` means the original claim no longer matches or the attempt budget is spent (the latter settles
    the stage through ``_fail_ingestion_stage``); the caller then stops without re-queueing. Otherwise the
    stage is ``retrying`` with a jittered (or provider ``Retry-After``) delay, the run queued, the event
    pending and the native lease renewed, all committed with replay.
    """
    run_id, stage_id = claim.run_id, claim.stage_id
    async with factory() as session:
        work = await _lock_worker_event(
            session,
            identifier,
            multi_workspace_enabled=enabled,
            event_types=event_types,
            expected=claim,
        )
        if work is None:
            return None
        if work.stage.attempts >= MAX_STAGE_ATTEMPTS:
            await session.rollback()
            await _fail_ingestion_stage(
                factory,
                identifier,
                run_id,
                stage_id,
                "retry_exhausted",
                claim=claim,
                multi_workspace_enabled=enabled,
            )
            return None
        delay = (
            max(0.5, exc.retry_after)
            if isinstance(exc, ConnectorRetryError) and exc.retry_after is not None
            else random.uniform(0.5, min(60.0, 2.0**work.stage.attempts))  # noqa: S311  # retry backoff jitter, not a security value
        )
        work.stage.status = "retrying"
        work.stage.error_code = "transient_failure"
        work.stage.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
        work.stage.lease_expires_at = None
        work.run.status = "queued"
        work.event.status = "pending"
        work.event.next_attempt_at = work.stage.next_attempt_at
        _wake_dispatcher_after_commit(session, delay)
        _update_native_run_lease(work.state, work.run.id, terminal=False)
        logger.warning(
            "Ingestion stage retry scheduled run_id=%s stage_id=%s attempt=%s",
            work.run.id,
            work.stage.id,
            work.stage.attempts,
        )
        await _commit_ingestion_change(
            session,
            work.run,
            work.stage,
            scope=work.scope,
            multi_workspace_enabled=enabled,
            access_fence=work.access_fence,
        )
    return delay


async def _complete_ingestion_stage(
    factory: async_sessionmaker[AsyncSession],
    identifier: UUID,
    enabled: bool,
    event_types: tuple[str, ...],
    claim: WorkerClaim,
) -> None:
    """Settle a finished stage under the original claim: observations proven, run refreshed, event delivered.

    Returns silently when the claim or lease is gone. A batch without observations fails the stage as
    ``missing_observations``. Otherwise the stage succeeds, the run status is derived, the source collection
    result is recorded, the native lease is released when terminal and everything commits with replay.
    """
    run_id, stage_id = claim.run_id, claim.stage_id
    async with factory() as session:
        work = await _lock_worker_event(
            session,
            identifier,
            multi_workspace_enabled=enabled,
            event_types=event_types,
            expected=claim,
        )
        if (
            work is None
            or work.stage.status != "running"
            or work.stage.lease_expires_at is None
            or work.stage.lease_expires_at <= datetime.now(UTC)
        ):
            return
        observed = await session.scalar(
            select(func.count())
            .select_from(SourceObservation)
            .where(
                SourceObservation.batch_id == work.batch.id,
                SourceObservation.source_id == work.source.id,
            )
        )
        if not observed:
            await session.rollback()
            await _fail_ingestion_stage(
                factory,
                identifier,
                run_id,
                stage_id,
                "missing_observations",
                claim=claim,
                multi_workspace_enabled=enabled,
            )
            return
        work.stage.status = "succeeded"
        work.stage.error_code = None
        work.stage.lease_expires_at = None
        await _refresh_run_status(
            session,
            work.run,
            scope=work.scope,
            multi_workspace_enabled=enabled,
            access_fence=work.access_fence,
        )
        changed = await sources.record_collection_result_in_uow(
            session,
            work.source.id,
            work.source.generation,
            datetime.now(UTC),
            None,
            scope=work.scope,
            multi_workspace_enabled=enabled,
            access_fence=work.access_fence,
            source_fence=work.source,
        )
        native_source = connectors.is_native_provider(work.projection.provider)
        _update_native_run_lease(
            work.state,
            work.run.id,
            terminal=work.run.status in {"succeeded", "failed"} if native_source else True,
        )
        await ingestion_api.mark_event_delivered(
            session, identifier, scope=work.scope, multi_workspace_enabled=enabled
        )
        logger.info("Ingestion stage completed run_id=%s stage_id=%s", work.run.id, work.stage.id)
        extras = (
            (
                make_source_change(
                    work.source.id, work.source.generation, work.source.status, scope=work.scope
                ),
            )
            if changed
            else ()
        )
        await _commit_ingestion_change(
            session,
            work.run,
            work.stage,
            extras,
            scope=work.scope,
            multi_workspace_enabled=enabled,
            access_fence=work.access_fence,
        )


@timed("ingestion_stage_ms", stage="collect")
async def process_ingestion_event(ctx: dict[str, object], event_id: str) -> None:
    """Commit one original scoped stage lease, release SQL for work and CAS before settlement.

    Retain dispatch timestamp, full payload principal and stage attempt across all phases.
    Rollback handoffs use only detached claim IDs; expired ORM attributes are never read.
    At most five stage attempts preserve bounded browser recovery; no model-gateway retry
    layer is added. Exact durable managed request/slot/quota binding remains C2 ownership.

    Phases: claim the running lease (``_claim_ingestion_stage``) -> external work under a stage timeout ->
    on transient failure schedule a retry (``_schedule_transient_retry``) -> otherwise settle the stage
    (``_complete_ingestion_stage``). Any unexpected error settles the stage as ``stage_failed``.
    """
    factory = worker_session_factory(ctx)
    enabled = _worker_flag(ctx)
    identifier = UUID(event_id)
    event_types = ("ingestion.stage.requested", "connector.crawl.requested")
    claimed = await _claim_ingestion_stage(factory, identifier, enabled, event_types)
    if claimed is None:
        return
    claim, event, run_id, stage_id = claimed
    try:
        async with asyncio.timeout(STAGE_TIMEOUT_SECONDS):
            if claim.event_type == "connector.crawl.requested":
                await _collect_web_job(
                    ctx,
                    factory,
                    event,
                    run_id,
                    stage_id,
                    claim=claim,
                    multi_workspace_enabled=enabled,
                )
    except (TimeoutError, OSError, OperationalError) as exc:
        delay = await _schedule_transient_retry(
            factory, identifier, enabled, event_types, claim, exc
        )
        if delay is None:
            return
        raise Retry(defer=delay) from exc
    except Exception:  # noqa: BLE001  # last-resort handler: any unexpected error must settle the stage as failed
        await _fail_ingestion_stage(
            factory,
            identifier,
            run_id,
            stage_id,
            "stage_failed",
            claim=claim,
            multi_workspace_enabled=enabled,
        )
        return
    await _complete_ingestion_stage(factory, identifier, enabled, event_types, claim)
