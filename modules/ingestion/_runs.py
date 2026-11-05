"""Run reads, history, retry and observability projections (internal; import through ``public``)."""

import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import String, and_, case, cast, func, not_, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationFailedError
from core.events import DomainEvent
from core.ids import uuid7
from core.pagination import b64url_decode, b64url_encode, decode_cursor, encode_cursor
from core.realtime import (
    commit_with_replay,
    make_ingestion_change,
)
from core.telemetry import RunMeta as _RunMeta
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope
from modules.ingestion._event_publisher import (
    publish_event,
)
from modules.ingestion._scope import (
    _actor_id,
    _admit_ingestion_scope,
    _digest,
    _event_scope,
    _materialization_scope,
    _run_scope,
    _source_in_scope,
)
from modules.ingestion.models import (
    COLLECTION_LEASE,
    CollectionReceipt,
    EventOutbox,
    IngestionBatch,
    IngestionRun,
    IngestionStage,
    ObservationNormalization,
    SourceIngestionState,
    SourceObservation,
)
from modules.ingestion.schemas import (
    RunRead,
    SourceIngestionRead,
    StageRead,
)
from modules.sources import public as sources
from modules.sources.public import SourceFence

_RUN_RETRY_ORDER = {"receive": 0, "collect_web": 1, "normalize": 2, "parse_file": 3}


async def _read_stages(session: AsyncSession, stages: list[IngestionStage], *, scope: Scope, multi_workspace_enabled: bool,
) -> list[StageRead]:
    """Authorize every persisted stage through its scoped run before owner count projection.

    Foreign stages fail the entire set before aggregation; materialization counts also
    compare their exact run/stage/batch/source-generation lineage. No commit occurs.
    """
    await _admit_ingestion_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not stages:
        return []
    stage_ids = {stage.id for stage in stages}
    authorized_ids = set((await session.scalars(
        select(IngestionStage.id).join(IngestionRun, IngestionRun.id == IngestionStage.run_id)
        .where(IngestionStage.id.in_(stage_ids), *_run_scope(scope))
    )).all())
    if authorized_ids != stage_ids:
        raise NotFoundError(detail="Ingestion stage not found")
    counts = await session.execute(
        select(
            ObservationNormalization.stage_id,
            func.sum(case((ObservationNormalization.disposition == "normalized", 1), else_=0)),
            func.sum(case((ObservationNormalization.disposition == "duplicate", 1), else_=0)),
            func.sum(case((ObservationNormalization.selected_current.is_(True), 1), else_=0)),
            func.sum(case((ObservationNormalization.disposition == "skipped", 1), else_=0)),
            func.sum(case((ObservationNormalization.disposition == "failed", 1), else_=0)),
            func.sum(case((ObservationNormalization.disposition == "pending", 1), else_=0)),
        )
        .where(ObservationNormalization.stage_id.in_([stage.id for stage in stages]), *_materialization_scope(scope))
        .group_by(ObservationNormalization.stage_id)
    )
    by_stage = {
        row[0]: tuple(int(value or 0) for value in row[1:])
        for row in counts
    }
    return [
        StageRead(
            stage_key=stage.stage_key, status=stage.status, attempts=stage.attempts,
            error_code=stage.error_code, result_count=stage.result_count, updated_at=stage.updated_at,
            normalized_count=by_stage.get(stage.id, (0, 0, 0, 0, 0, 0))[0],
            duplicate_count=by_stage.get(stage.id, (0, 0, 0, 0, 0, 0))[1],
            selected_current_count=by_stage.get(stage.id, (0, 0, 0, 0, 0, 0))[2],
            skipped_count=by_stage.get(stage.id, (0, 0, 0, 0, 0, 0))[3],
            failed_count=by_stage.get(stage.id, (0, 0, 0, 0, 0, 0))[4],
            pending_count=by_stage.get(stage.id, (0, 0, 0, 0, 0, 0))[5],
        )
        for stage in stages
    ]


async def get_run(session: AsyncSession, run_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> tuple[IngestionRun, list[StageRead]] | None:
    """Return a run and its stage projection, or None when the run is absent."""
    await _admit_ingestion_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    run = await session.scalar(select(IngestionRun).where(IngestionRun.id == run_id, *_run_scope(scope)))
    if run is None:
        return None
    stages = list(
        (
            await session.scalars(
                select(IngestionStage).where(IngestionStage.run_id == run_id).order_by(IngestionStage.stage_key)
            )
        ).all()
    )
    return run, await _read_stages(session, stages, scope=scope, multi_workspace_enabled=multi_workspace_enabled)


def _cursor_binding(scope: Scope, access_fence: AccessFence, kind: str, limit: int) -> str:
    """Bind a cursor to principal/configuration epoch, page kind/bounds and source restriction."""
    if (access_fence.workspace_id != scope.workspace_id or access_fence.user_id != _actor_id(scope)
            or access_fence.membership_revision != scope.membership_revision):
        raise ValueError("Cursor fence does not match its principal")
    return _digest({"workspace_id": str(scope.workspace_id), "actor_user_id": _actor_id(scope),
                    "membership_revision": scope.membership_revision,
                    "configuration_revision": access_fence.configuration_revision,
                    "source_id": str(scope.source_id) if isinstance(scope, InternalJobScope) else None,
                    "source_generation": scope.source_generation if isinstance(scope, InternalJobScope) else None,
                    "kind": kind, "limit": limit, "order": "timestamp,id"})


def _encode_history_cursor(position: tuple[datetime, UUID], binding: str) -> str:
    """Encode a scoped v2 cursor around the existing timestamp/UUID pagination primitive."""
    raw = json.dumps({"v": 2, "binding": binding, "position": encode_cursor(*position)},
                     separators=(",", ":")).encode("utf-8")
    return b64url_encode(raw)


def _decode_history_cursor(cursor: str, binding: str) -> tuple[datetime, UUID]:
    """Reject unbound/foreign/stale/noncanonical cursors before applying any query offset."""
    try:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096 or "=" in cursor:
            raise ValueError("Invalid cursor encoding")
        raw = b64url_decode(cursor)
        if b64url_encode(raw) != cursor:
            raise ValueError("Noncanonical cursor")
        envelope = json.loads(raw)
        if (not isinstance(envelope, dict) or set(envelope) != {"v", "binding", "position"}
                or type(envelope["v"]) is not int or envelope["v"] != 2
                or envelope["binding"] != binding or not isinstance(envelope["position"], str)):
            raise ValueError("Cursor binding does not match")
        return decode_cursor(envelope["position"])
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValidationFailedError(detail="Invalid or stale ingestion cursor") from exc


async def list_source_runs(
    session: AsyncSession,
    source_id: UUID,
    *,
    limit: int = 20,
    cursor: str | None = None, status: Literal["queued", "running", "succeeded", "needs_ocr", "failed"] | None = None,
    scope: Scope, multi_workspace_enabled: bool,
) -> SourceIngestionRead | None:
    """Return detached source history, optionally status-filtered, with a scoped cursor."""
    access_fence = await _admit_ingestion_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    from modules.ingestion.schemas import SourceIngestionRead

    source = await _source_in_scope(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source is None:
        return None
    if not 1 <= limit <= 100:
        raise ValueError("Ingestion history page size must be between 1 and 100")
    kind = f"history:{source.id}:{source.generation}"
    if status is not None:
        kind = f"{kind}:status:{status}"
    binding = _cursor_binding(scope, access_fence, kind, limit)
    statement = select(IngestionRun).where(IngestionRun.source_id == source_id, *_run_scope(scope))
    if status is not None:
        statement = statement.where(IngestionRun.status == status)
    if cursor:
        created_at, identifier = _decode_history_cursor(cursor, binding)
        statement = statement.where(
            tuple_(IngestionRun.created_at, IngestionRun.id) < (created_at, identifier)
        )
    rows = list((await session.scalars(
        statement.order_by(IngestionRun.created_at.desc(), IngestionRun.id.desc()).limit(limit + 1)
    )).all())
    page_rows = rows[:limit]
    next_cursor = (
        _encode_history_cursor((page_rows[-1].created_at, page_rows[-1].id), binding)
        if len(rows) > limit and page_rows
        else None
    )
    current = await session.scalar(
        select(IngestionRun)
        .where(*_run_scope(scope), IngestionRun.source_id == source_id, IngestionRun.status.in_(("queued", "running")))
        .order_by(IngestionRun.created_at.desc(), IngestionRun.id.desc())
        .limit(1)
    )
    run_rows = list({run.id: run for run in [*page_rows, *([current] if current else [])]}.values())
    stage_rows = list((await session.scalars(
        select(IngestionStage)
        .where(IngestionStage.run_id.in_([run.id for run in run_rows]))
        .order_by(IngestionStage.stage_key)
    )).all()) if run_rows else []
    stages_by_run: dict[UUID, list[IngestionStage]] = {run.id: [] for run in run_rows}
    for stage in stage_rows:
        stages_by_run[stage.run_id].append(stage)
    stage_reads = {
        run_id: await _read_stages(session, stages, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        for run_id, stages in stages_by_run.items()
    }
    # Receipt telemetry for these runs only (one bounded query); runs without a receipt report None.
    stats_by_run: dict[UUID, dict[str, Any]] = {}
    if run_rows:
        for stats_run_id, stats in (await session.execute(
            select(CollectionReceipt.run_id, CollectionReceipt.collection_stats).where(
                CollectionReceipt.run_id.in_([run.id for run in run_rows]),
                CollectionReceipt.source_id == source_id,
                CollectionReceipt.collection_stats.is_not(None)))).tuples().all():
            if stats_run_id is not None and stats is not None:
                stats_by_run[stats_run_id] = stats

    def detach(run: IngestionRun) -> RunRead:
        """Project an ORM run and its stages into a detached response."""
        return RunRead(
            workspace_id=run.workspace_id,
            run_id=run.id,
            source_id=run.source_id,
            status=run.status,
            stages=stage_reads[run.id],
            error_code=run.error_code,
            created_at=run.created_at,
            updated_at=run.updated_at,
            collection_stats=stats_by_run.get(run.id),
        )

    return SourceIngestionRead(
        workspace_id=scope.workspace_id,
        current_run=detach(current) if current is not None else None,
        items=[detach(run) for run in page_rows],
        next_cursor=next_cursor,
    )


def _choose_retry_stage(stages: list[IngestionStage], requested_stage_key: str | None) -> IngestionStage | None:
    """Pick the failed stage to retry, or ``None`` when the run needs no retry (the caller returns the run).

    Active stages block (409 only when a specific stage was requested); an all-succeeded run or one without a
    failed stage is a no-op. Failed stages are ordered by the pipeline order; an unrequested retry takes the
    earliest, and a requested key that is not failed is a 409.
    """
    if any(stage.status in {"pending", "queued", "running", "retrying"} for stage in stages):
        if requested_stage_key is not None:
            raise ConflictError(detail="Ingestion run still has an active stage")
        return None
    if all(stage.status == "succeeded" for stage in stages):
        return None
    failed = sorted(
        (stage for stage in stages if stage.status == "failed"),
        key=lambda stage: (_RUN_RETRY_ORDER.get(stage.stage_key, 100), stage.stage_key),
    )
    if not failed:
        return None
    stage = next((item for item in failed if item.stage_key == requested_stage_key), None) if requested_stage_key else failed[0]
    if stage is None:
        raise ConflictError(detail="Requested stage is not retryable")
    return stage


async def _load_retry_event(
    session: AsyncSession, run: IngestionRun, stage: IngestionStage, source: SourceFence, *, scope: Scope,
) -> EventOutbox:
    """Lock the stage's newest durable event and prove the retry is still valid; 409 otherwise.

    The failed stage must have a prior event captured at the current Source generation, and a failed
    normalization stage with invalid observations requires correction first.
    """
    prior_event = await session.scalar(
        select(EventOutbox)
        .where(EventOutbox.payload["stage_id"].astext == str(stage.id), EventOutbox.payload["run_id"].astext == str(run.id), *_event_scope(scope))
        .order_by(EventOutbox.created_at.desc())
        .limit(1)
        .with_for_update()
    )
    if prior_event is None:
        raise ConflictError(detail="Failed stage has no durable retry event")
    captured_generation = prior_event.payload.get("source_generation")
    if not isinstance(captured_generation, int) or captured_generation != source.generation:
        raise ConflictError(detail="Failed stage belongs to an obsolete source generation")
    if stage.stage_key == "normalize":
        failed_progress = await session.scalar(
            select(ObservationNormalization.id).where(
                *_materialization_scope(scope), ObservationNormalization.stage_id == stage.id,
                ObservationNormalization.disposition == "failed",
            ).limit(1)
        )
        if failed_progress is not None:
            raise ConflictError(detail="Invalid observations require correction before normalization retry")
    return prior_event


def _reacquire_collection_lease(state: SourceIngestionState, run: IngestionRun) -> None:
    """Hand the Source collection lease to ``run`` for a retried collection stage; 409 if another run owns it.

    Rejects an ownerless-expiry state and any live lease held by a different run, then sets a fresh lease.
    """
    now = datetime.now(UTC)
    if state.lease_run_id is not None and state.lease_expires_at is None:
        raise ConflictError(detail="Source collection ownership state is invalid")
    if state.lease_run_id not in (None, run.id) and state.lease_expires_at and state.lease_expires_at > now:
        raise ConflictError(detail="Source already has an active collection run")
    if state.lease_expires_at is not None and state.lease_expires_at > now and state.lease_run_id != run.id:
        raise ConflictError(detail="Source already has an active collection run")
    state.lease_run_id = run.id
    state.collection_lease_token = None
    state.lease_expires_at = now + COLLECTION_LEASE


async def retry_run(
    session: AsyncSession, run_id: UUID, requested_stage_key: str | None = None, *, scope: Scope, multi_workspace_enabled: bool,
) -> IngestionRun | None:
    """Requeue an eligible failed stage after validating source generation.

    Locks source, collection state, run, then stage and requires a durable prior event. Returns
    None when the run is absent; no-op paths return the existing run for active,
    successful, or otherwise non-retryable work. Accepted retries reset attempts/errors, copy the prior
    event payload into a fresh outbox event, refresh collection leases when
    needed, and commit. Failed normalization progress requiring correction and
    stale source generations are rejected with HTTP 409.
    """
    await _admit_ingestion_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    run_hint = await session.scalar(select(IngestionRun).where(IngestionRun.id == run_id, *_run_scope(scope)))
    if run_hint is None:
        return None
    # Source lock serializes retry against reservation acquisition and source purge.
    locked = await sources.lock_source_set(session, (run_hint.source_id,), scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    source = locked.fences[0]
    access_fence = locked.access_fence
    if source is None or source.status != "active":
        raise ConflictError(detail="Source is not active")
    state = await session.get(SourceIngestionState, run_hint.source_id, with_for_update=True)
    now = datetime.now(UTC)
    if state is not None and state.collection_lease_token is not None:
        if state.lease_expires_at is None or state.lease_expires_at > now:
            raise ConflictError(detail="Source collection is already in progress")
        state.collection_lease_token = None
        state.lease_expires_at = None
    run = await session.scalar(
        select(IngestionRun).where(IngestionRun.id == run_id, IngestionRun.source_id == source.id, *_run_scope(scope)).with_for_update()
    )
    if run is None:
        return None
    stages = list((await session.scalars(
        select(IngestionStage).where(IngestionStage.run_id == run_id)
        .order_by(IngestionStage.stage_key).with_for_update()
    )).all())
    if not stages:
        raise RuntimeError("Ingestion run has no stage")
    stage = _choose_retry_stage(stages, requested_stage_key)
    if stage is None:
        return run
    prior_event = await _load_retry_event(session, run, stage, source, scope=scope)
    stage.status = "pending"
    stage.attempts = 0
    stage.error_code = None
    stage.next_attempt_at = datetime.now(UTC)
    stage.lease_expires_at = None
    run.status = "queued"
    run.error_code = None
    event = DomainEvent(
        id=uuid7(),
        type=prior_event.type,
        version=1,
        occurred_at=datetime.now(UTC),
        producer="modules.ingestion",
        payload={
            **prior_event.payload,
        },
    )
    await publish_event(session, event, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if stage.stage_key in {"receive", "collect_web"} and state is not None:
        _reacquire_collection_lease(state, run)
    run.status = "queued"
    run.error_code = None
    await commit_with_replay(
        session,
        [make_ingestion_change(source.id, run.id, run.status, stage.stage_key, stage.status, scope=scope)], scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
    await session.refresh(run)
    return run


async def list_terminal_runs_after(
    session: AsyncSession, position: tuple[datetime, UUID] | None, limit: int = 100,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> list[tuple[datetime, UUID, str, dict[str, str | int]]]:
    """Read-only keyset page of ingestion runs in a terminal state for connector sync results.

    Ordered by ``(updated_at, id)`` after the caller's ``(updated_at, id)`` tuple ``position``;
    the key combines run id and status so a later status change is a new event. The workspace
    predicate (``_run_scope``) precedes ordering and LIMIT, owner admission precedes any query, and
    the tuple position is the sweep's own workspace-keyed cursor, never an opaque token.
    Payload carries the declared connector_sync_result condition fields only (``TRIGGER_FIELDS``:
    source_id, status, new_items); undeclared keys make trigger validation drop the event. Workspace
    scope comes from the sweep's own admission, not the payload.
    """
    await _admit_ingestion_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not 1 <= limit <= 100:
        raise ValueError("Terminal-run page size must be between 1 and 100")
    stmt = select(IngestionRun).join(IngestionBatch, and_(
        IngestionBatch.id == IngestionRun.batch_id, IngestionBatch.source_id == IngestionRun.source_id,
    )).where(IngestionRun.status.in_(("succeeded", "failed", "needs_ocr")),
             # Pre-0007 batches with NULL generation are excluded on purpose.
             IngestionBatch.source_generation >= 1, *_run_scope(scope))
    if position is not None:
        stmt = stmt.where(tuple_(IngestionRun.updated_at, IngestionRun.id) > tuple_(*position))
    rows = (await session.scalars(stmt.order_by(IngestionRun.updated_at, IngestionRun.id).limit(limit))).all()
    # new_items = observations collected in the run's batch (one grouped count for the page).
    counts = dict((await session.execute(
        select(SourceObservation.batch_id, func.count()).join(IngestionRun, and_(
            IngestionRun.batch_id == SourceObservation.batch_id,
            IngestionRun.source_id == SourceObservation.source_id,
        )).where(SourceObservation.batch_id.in_([r.batch_id for r in rows]), *_run_scope(scope))
        .group_by(SourceObservation.batch_id)
    )).all()) if rows else {}
    return [(r.updated_at, r.id, f"{r.id}:{r.status}",
             {"source_id": str(r.source_id), "status": r.status,
              "new_items": int(counts.get(r.batch_id, 0))})
            for r in rows]


async def list_run_meta(session: AsyncSession, limit: int, *, instance_operator: bool) -> list[_RunMeta]:
    """Return <=100 newest run metadata under real internal instance-operator admission.

    True comes from bootstrap/operator authorization, never client input or workspace
    owner role. This deliberate instance aggregate carries no content.
    """
    if instance_operator is not True:
        raise ForbiddenError(detail="Instance operator required")
    if limit < 1:
        raise ValueError("Run metadata page size must be positive")
    # Column projection: run payload/progress columns are never loaded for this metadata page.
    rows = await session.execute(select(
        IngestionRun.id, IngestionRun.status, IngestionRun.error_code, IngestionRun.created_at,
        IngestionRun.updated_at,
    ).order_by(IngestionRun.created_at.desc()).limit(min(limit, 100)))
    return [_RunMeta(kind="ingestion", id=str(r.id), status=r.status, error_code=r.error_code,
                     created_at=r.created_at, updated_at=r.updated_at,
                     finished_at=r.updated_at if r.status in {"succeeded", "failed", "needs_ocr"} else None)
            for r in rows]


async def observability_quality_summary(session: AsyncSession, *, instance_operator: bool) -> dict[str, int | float]:
    """Return content-free instance aggregates under real operator admission, never workspace-owner authority."""
    if instance_operator is not True:
        raise ForbiddenError(detail="Instance operator required")
    normalized, duplicates = (await session.execute(select(
        func.count().filter(ObservationNormalization.disposition.in_(("normalized", "duplicate"))),
        func.count().filter(ObservationNormalization.disposition == "duplicate"),
    ))).one()
    failed_runs = int(await session.scalar(select(func.count()).select_from(IngestionRun).where(
        IngestionRun.status == "failed"
    )) or 0)
    return {"duplicate_rate": float(duplicates or 0) / int(normalized or 1), "failed_ingestion": failed_runs}


async def observability_queue_summary(session: AsyncSession, *, instance_operator: bool, now: datetime | None = None) -> dict[str, object]:
    """Return instance queue aggregates under real operator admission and current lifecycle metadata.

    Source supplies a narrow operator-only lifecycle projection. Instance True comes
    from operator admission; no credentials/config/content cross this boundary.
    """
    if instance_operator is not True:
        raise ForbiddenError(detail="Instance operator required")
    now = now or datetime.now(UTC)
    source_lifecycle = sources.ingestion_instance_lifecycle_projection(instance_operator=instance_operator).subquery("source_lifecycle")
    stage_counts = dict((await session.execute(
        select(IngestionStage.status, func.count()).group_by(IngestionStage.status)
    )).all())
    active_stage = aliased(IngestionStage)
    active_stage_exists = select(1).select_from(active_stage).where(
        active_stage.run_id == IngestionStage.run_id,
        active_stage.status.in_(("pending", "queued", "running", "retrying")),
    ).exists()
    failed_progress_exists = select(1).where(
        ObservationNormalization.stage_id == IngestionStage.id,
        ObservationNormalization.disposition == "failed",
    ).exists()
    latest_event = aliased(EventOutbox)
    latest_event_id = (select(latest_event.id).where(
        latest_event.payload["stage_id"].astext == cast(IngestionStage.id, String),
    ).order_by(latest_event.created_at.desc()).limit(1).correlate(IngestionStage).scalar_subquery())
    latest_event_matches = select(1).select_from(EventOutbox).where(
        EventOutbox.id == latest_event_id,
        EventOutbox.payload["source_generation"].astext == cast(source_lifecycle.c.generation, String),
    ).exists()
    active_collection_exists = select(1).select_from(SourceIngestionState).where(
        SourceIngestionState.source_id == source_lifecycle.c.id,
        SourceIngestionState.collection_lease_token.is_not(None),
        or_(SourceIngestionState.lease_expires_at.is_(None), SourceIngestionState.lease_expires_at > now),
    ).exists()
    other_run_lease_exists = select(1).select_from(SourceIngestionState).where(
        SourceIngestionState.source_id == source_lifecycle.c.id,
        SourceIngestionState.lease_run_id.is_not(None),
        SourceIngestionState.lease_run_id != IngestionRun.id,
        or_(SourceIngestionState.lease_expires_at.is_(None), SourceIngestionState.lease_expires_at > now),
    ).exists()
    failed_candidate = aliased(IngestionStage)
    retry_order = case(*((IngestionStage.stage_key == key, rank)
                         for key, rank in _RUN_RETRY_ORDER.items()), else_=100)
    candidate_order = case(*((failed_candidate.stage_key == key, rank)
                             for key, rank in _RUN_RETRY_ORDER.items()), else_=100)
    earlier_failed_exists = select(1).select_from(failed_candidate).where(
        failed_candidate.run_id == IngestionStage.run_id,
        failed_candidate.status == "failed",
        or_(candidate_order < retry_order,
            and_(candidate_order == retry_order, failed_candidate.stage_key < IngestionStage.stage_key)),
    ).exists()
    retryable = int(await session.scalar(
        select(func.count()).select_from(IngestionStage)
        .join(IngestionRun, IngestionRun.id == IngestionStage.run_id)
        .join(source_lifecycle, source_lifecycle.c.id == IngestionRun.source_id)
        .where(
            IngestionStage.status == "failed", source_lifecycle.c.status == "active",
            not_(active_stage_exists), not_(earlier_failed_exists), latest_event_matches,
            or_(IngestionStage.stage_key != "normalize", not_(failed_progress_exists)),
            not_(active_collection_exists),
            or_(IngestionStage.stage_key.not_in(("receive", "collect_web")), not_(other_run_lease_exists)),
        )
    ) or 0)
    event_delivery = dict((await session.execute(
        select(EventOutbox.status, func.count()).group_by(EventOutbox.status)
    )).all())
    return {"ingestion_stages": stage_counts, "retryable_ingestion_stages": retryable,
            "event_delivery": event_delivery}


async def get_run_meta_by_id(session: AsyncSession, run_id: UUID, *, instance_operator: bool) -> _RunMeta | None:
    """Return one instance run metadata under explicit real operator admission; no content or scope borrowing."""
    if instance_operator is not True:
        raise ForbiddenError(detail="Instance operator required")
    row = await session.get(IngestionRun, run_id)
    if row is None:
        return None
    return _RunMeta(kind="ingestion", id=str(row.id), status=row.status, error_code=row.error_code,
                    created_at=row.created_at, updated_at=row.updated_at,
                    finished_at=row.updated_at if row.status in {"succeeded", "failed", "needs_ocr"} else None)
