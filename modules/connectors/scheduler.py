"""Durable collection scheduler, global admission slots and request settlement.

PostgreSQL rows are the only authority: Redis carries a request UUID and may be lost at any
time. Lock order for every operation is Source -> ConnectorProvisioning -> request -> slot ->
schedule; no transaction is held across provider I/O. The existing SourceIngestionState lease
stays the single source lock: admission only checks it is free, and the executor acquires it
through the ingestion owner right after admission (ingestion settlement hooks arrive with C3).
A slot fences one admitted attempt; it guarantees at most two concurrent admitted jobs and one
per workspace, not cancellation of remote HTTP already sent by an expired attempt.
"""

from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.workspaces.models import WorkspaceMembership
from core.workspaces.schemas import InternalJobScope, Scope
from modules.connectors import provider_terms, provisioning
from modules.connectors import public as connectors
from modules.connectors.collection_schemas import (
    CollectionAdmissionRead,
    CollectionAdmissionRequest,
    CollectionRequestRead,
    CollectionTrigger,
)
from modules.connectors.models import (
    ConnectorAdmissionSlot,
    ConnectorCollectionRequest,
    ConnectorProvisioning,
    ConnectorSchedule,
    ConnectorWorkspaceDispatch,
)

MAX_ATTEMPTS = 5
SLOT_TTL = timedelta(seconds=120)  # renewal every 20 s is the executor's duty
BUSY_DEFER = timedelta(seconds=30)
ENQUEUE_CLAIM = timedelta(seconds=60)
REENQUEUE_AFTER = timedelta(minutes=5)  # lost Redis job: the durable row is enqueued again
TICK_WORKSPACES = 50
# Failures that need owner action: never auto-retried, they gate the source's schedule.
# The value is the blocked dimension; clear_collection_block re-opens it once that dimension moves.
ACTION_REQUIRED = {"invalid_credential": "credential", "schema_changed": "config", "terms_not_accepted": "terms"}
_FREE_SLOT = {
    "occupied_request_id": None, "workspace_id": None, "admission_token": None,
    "lease_kind": None, "source_owner_id": None, "expires_at": None,
}
Outcome = Literal["succeeded", "no_changes", "failed", "cancelled", "deferred"]


def retry_at(now: datetime, attempt: int, provider_deadline: datetime | None) -> datetime:
    """Never retry earlier than provider instructions; cap only local backoff."""
    delay = min(30 * (2 ** min(max(attempt - 1, 0), 5)), 900)
    candidate = now + timedelta(seconds=delay)
    return max(candidate, provider_deadline) if provider_deadline else candidate


def next_due_after(now: datetime, due: datetime, interval_minutes: int) -> datetime:
    """Return the first regular boundary after now, so a late tick yields one catch-up only."""
    step = timedelta(minutes=interval_minutes)
    return due + step * ((now - due) // step + 1) if due <= now else due + step


def _read(request: ConnectorCollectionRequest) -> CollectionRequestRead:
    """Project a request row to its public DTO."""
    return CollectionRequestRead(
        request_id=request.id, source_id=request.source_id, status=request.status,  # type: ignore[arg-type]
        ingestion_run_id=request.ingestion_run_id, error_code=request.error_code,
    )


def _request_scope(request: ConnectorCollectionRequest) -> InternalJobScope:
    """Rebuild the worker subject from the durable request, never from queued arguments."""
    return InternalJobScope(
        workspace_id=request.workspace_id, actor_user_id=request.actor_user_id,
        membership_revision=request.membership_revision,
        source_id=request.source_id, source_generation=request.source_generation,
    )


async def upsert_schedule(
    session: AsyncSession, *, workspace_id: UUID, source_id: UUID, interval_minutes: int, enabled: bool,
) -> None:
    """Create or update a source schedule; activation owners call it, the caller commits.

    Enabling an overdue schedule keeps next_due_at, so it yields a single catch-up request.
    """
    now = datetime.now(UTC)
    row = await session.get(ConnectorSchedule, source_id, with_for_update=True)
    if row is None:
        session.add(ConnectorSchedule(
            source_id=source_id, workspace_id=workspace_id, enabled=enabled,
            interval_minutes=interval_minutes, next_due_at=now,
        ))
    else:
        row.enabled = enabled
        row.interval_minutes = interval_minutes
    await session.flush()


async def _open_request(
    session: AsyncSession, scope: Scope, source_id: UUID, trigger: CollectionTrigger,
    expected_revision: int | None, *, multi_workspace_enabled: bool, existing_first: bool = False,
) -> ConnectorCollectionRequest:
    """Validate fences under Source/provisioning locks and return the coalesced active request.

    A queued request whose captured fences are stale is cancelled and replaced, so a click after
    a reconfigure is never absorbed. A scheduled trigger also requires the schedule to be due and
    advances it exactly once; with existing_first (managed n8n admission) a current active
    request is returned before the due/enabled checks. The caller commits; HTTPException reports
    missing/stale/blocked state.
    """
    from modules.sources import public as sources

    await connectors._connector_access(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    source_fence, row, _ = await provisioning.lock_connector(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    source = await sources.get_connector_source(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source_fence is None or source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if (
        source.status != "active" or row is None or row.state != "active"
        or row.source_generation != source.generation or row.applied_revision != row.desired_revision
    ):
        raise HTTPException(status_code=409, detail="Enable this source from connector settings before collecting")
    if expected_revision is not None and expected_revision != row.desired_revision:
        raise HTTPException(status_code=409, detail="Connector revision changed")
    if not await connectors.require_collection_fence(
        session, source, connectors.CollectionFence(
            source_generation=source.generation, connector_revision=row.desired_revision),
        lock=True, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    ):
        raise HTTPException(status_code=409, detail="Connector collection fence is stale")

    now = datetime.now(UTC)
    active = await session.scalar(
        select(ConnectorCollectionRequest).where(
            ConnectorCollectionRequest.source_id == source_id,
            ConnectorCollectionRequest.status.in_(("queued", "running")),
        ).with_for_update().execution_options(populate_existing=True))
    schedule = await session.scalar(
        select(ConnectorSchedule).where(ConnectorSchedule.source_id == source_id)
        .with_for_update().execution_options(populate_existing=True))
    if schedule is not None and schedule.blocked_error_code is not None:
        raise HTTPException(status_code=409, detail=f"Action required: {schedule.blocked_error_code}")
    # Terms/eligibility gate for manual, scheduled and retry alike, before any queueing or network.
    terms_revision = await provider_terms.require_terms_eligible(session, source)
    if active is not None and active.status == "queued" and (
        active.terms_revision != terms_revision
        or active.source_generation != source.generation or active.connector_revision != row.desired_revision
        or active.backend_revision != row.backend_revision or active.captured_backend != row.execution_backend
    ):
        active.status, active.error_code = "cancelled", "revision_changed"
        await session.flush()  # free the partial-unique slot before the replacement insert
        active = None
    if active is not None and existing_first:
        return active
    if trigger == "scheduled":
        if schedule is None or not schedule.enabled or schedule.next_due_at > now or (
            schedule.next_eligible_at is not None and schedule.next_eligible_at > now
        ):
            raise HTTPException(status_code=409, detail="Collection is not due")
        schedule.next_due_at = next_due_after(now, schedule.next_due_at, schedule.interval_minutes)
        schedule.last_dispatch_at = now
    if active is not None:
        return active
    if schedule is not None and schedule.next_eligible_at is not None and schedule.next_eligible_at > now:
        raise HTTPException(status_code=429, detail="Provider asked to retry later")
    request = ConnectorCollectionRequest(
        id=uuid4(), workspace_id=scope.workspace_id,
        source_id=source_id, actor_user_id=connectors._connector_actor(scope),
        membership_revision=scope.membership_revision, trigger=trigger,
        source_generation=source.generation, connector_revision=row.desired_revision,
        backend_revision=row.backend_revision, captured_backend=row.execution_backend,
        status="queued", attempt=0, available_at=now, enqueue_next_at=now, terms_revision=terms_revision,
    )
    session.add(request)
    await session.flush()
    return request


async def request_collection(
    session: AsyncSession, scope: Scope, source_id: UUID, trigger: CollectionTrigger,
    expected_revision: int, *, multi_workspace_enabled: bool,
) -> CollectionRequestRead:
    """Persist (or coalesce into) one durable request for an owner; the worker picks it up later."""
    try:
        request = await _open_request(
            session, scope, source_id, trigger, expected_revision,
            multi_workspace_enabled=multi_workspace_enabled)
        result = _read(request)
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    return result


async def get_collection_request(
    session: AsyncSession, scope: Scope, source_id: UUID, request_id: UUID, *, multi_workspace_enabled: bool,
) -> CollectionRequestRead:
    """Read a request after proving owner access to its source; no row locks."""
    source = await connectors._read_scoped_source(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    request = await session.scalar(
        select(ConnectorCollectionRequest).where(
            ConnectorCollectionRequest.id == request_id,
            ConnectorCollectionRequest.source_id == source_id,
            ConnectorCollectionRequest.workspace_id == scope.workspace_id,
        ).execution_options(populate_existing=True))
    if source is None or request is None:
        raise HTTPException(status_code=404, detail="Collection request not found")
    return _read(request)


async def _defer(session: AsyncSession, request_id: UUID, delay: timedelta) -> None:
    """Push a queued request back without burning an attempt, in its own short transaction."""
    when = datetime.now(UTC) + delay
    await session.execute(
        update(ConnectorCollectionRequest)
        .where(ConnectorCollectionRequest.id == request_id, ConnectorCollectionRequest.status == "queued")
        .values(available_at=when, enqueue_next_at=when))
    await session.commit()


def _penalize(schedule: ConnectorSchedule | None, until: datetime | None) -> None:
    """Record a terminal failure on the schedule; provider deadlines are never shortened."""
    if schedule is not None:
        schedule.failure_count += 1
        schedule.next_eligible_at = until


async def _stale_reason(
    session: AsyncSession, request: ConnectorCollectionRequest, scope: InternalJobScope,
    source_fence: object, source: object, row: ConnectorProvisioning | None, *, multi_workspace_enabled: bool,
) -> str | None:
    """Return why a request may no longer send (source_inactive/revision_changed), or None if current."""
    if (
        source_fence is None or source is None or getattr(source, "status", None) != "active"
        or row is None or row.state != "active"
    ):
        return "source_inactive"
    if not (
        source.generation == request.source_generation
        and row.source_generation == request.source_generation
        and row.applied_revision == row.desired_revision == request.connector_revision
        and row.execution_backend == request.captured_backend
        and row.backend_revision == request.backend_revision
        and await connectors.require_collection_fence(
            session, source, connectors.CollectionFence(  # type: ignore[arg-type]
                source_generation=request.source_generation, connector_revision=request.connector_revision),
            lock=True, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    ):
        return "revision_changed"
    return None


async def admit_collection_request(
    session: AsyncSession, request_id: UUID, *, multi_workspace_enabled: bool,
) -> CollectionAdmissionRead | None:
    """Claim a global slot and move one queued request to running, or return None.

    Busy source, workspace or capacity defers without burning an attempt. Revision changes
    cancel the request; the sixth admission fails it. Commits before any provider I/O.
    """
    from modules.ingestion.models import SourceIngestionState
    from modules.settings.public import module_is_enabled
    from modules.sources import public as sources

    peek = await session.get(ConnectorCollectionRequest, request_id)
    if peek is None or peek.status != "queued":
        await session.rollback()
        return None
    scope = _request_scope(peek)
    source_id = peek.source_id
    await session.rollback()
    if not await module_is_enabled(session, "connectors", scope=scope, multi_workspace_enabled=multi_workspace_enabled):
        await session.rollback()
        return None  # durable request untouched until the module is re-enabled
    try:
        await connectors._connector_access(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        source_fence, row, _ = await provisioning.lock_connector(
            session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        source = await sources.get_connector_source(
            session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        request = await session.scalar(
            select(ConnectorCollectionRequest).where(ConnectorCollectionRequest.id == request_id)
            .with_for_update().execution_options(populate_existing=True))
    except HTTPException:
        # Lost access or source: no admission is possible; terminalize without exposing data.
        await session.rollback()
        await session.execute(
            update(ConnectorCollectionRequest)
            .where(ConnectorCollectionRequest.id == request_id, ConnectorCollectionRequest.status == "queued")
            .values(status="cancelled", error_code="access_lost"))
        await session.commit()
        return None
    now = datetime.now(UTC)
    if request is None or request.status != "queued" or request.available_at > now:
        await session.rollback()
        return None
    stale = await _stale_reason(
        session, request, scope, source_fence, source, row, multi_workspace_enabled=multi_workspace_enabled)
    if stale is not None or request.attempt >= MAX_ATTEMPTS:
        request.status = "cancelled" if stale else "failed"
        request.error_code = stale or "attempts_exhausted"
        if stale is None:
            _penalize(await session.get(ConnectorSchedule, source_id, with_for_update=True), request.provider_deadline)
        await session.commit()
        return None
    state = await session.scalar(
        select(SourceIngestionState).where(SourceIngestionState.source_id == source_id)
        .execution_options(populate_existing=True))
    workspace_busy = await session.scalar(
        select(ConnectorAdmissionSlot.slot_id).where(ConnectorAdmissionSlot.workspace_id == request.workspace_id))
    slot = None if (
        workspace_busy is not None
        or (state is not None and state.lease_expires_at is not None and state.lease_expires_at > now)
    ) else await session.scalar(
        select(ConnectorAdmissionSlot).where(ConnectorAdmissionSlot.occupied_request_id.is_(None))
        .order_by(ConnectorAdmissionSlot.slot_id).limit(1).with_for_update(skip_locked=True))
    if slot is None:
        await session.rollback()
        await _defer(session, request_id, BUSY_DEFER)
        return None
    token = uuid4()
    slot.occupied_request_id = request.id
    slot.workspace_id = request.workspace_id
    slot.admission_token = token
    slot.lease_kind = "collection"
    slot.expires_at = now + SLOT_TTL
    request.status = "running"
    request.attempt += 1
    request.active_admission_token = token
    request.error_code = None
    try:
        await session.flush()
    except IntegrityError:
        # A concurrent same-workspace admission won the partial unique index.
        await session.rollback()
        await _defer(session, request_id, BUSY_DEFER)
        return None
    result = CollectionAdmissionRead(
        request_id=request.id, source_id=source_id, admission_token=token, attempt=request.attempt)
    await session.commit()
    return result


async def lock_running_request_in_uow(
    session: AsyncSession, request_id: UUID, admission_token: UUID, *, source_id: UUID,
    source_generation: int, connector_revision: int, workspace_id: UUID,
) -> bool:
    """Lock the request row and prove this exact attempt may still publish; no commit.

    True only for a running request owning an unexpired slot under the same admission token,
    source, workspace and captured generation/revision. An expired or superseded attempt gets
    False, so an old worker can never publish results or advance a cursor.
    """
    request = await session.scalar(
        select(ConnectorCollectionRequest).where(ConnectorCollectionRequest.id == request_id)
        .with_for_update().execution_options(populate_existing=True))
    if (
        request is None or request.status != "running" or request.active_admission_token != admission_token
        or request.source_id != source_id or request.workspace_id != workspace_id
        or request.source_generation != source_generation or request.connector_revision != connector_revision
    ):
        return False
    slot = await session.scalar(
        select(ConnectorAdmissionSlot.slot_id).where(
            ConnectorAdmissionSlot.occupied_request_id == request_id,
            ConnectorAdmissionSlot.admission_token == admission_token,
            ConnectorAdmissionSlot.expires_at > datetime.now(UTC)))
    return slot is not None


async def renew_admission(
    session: AsyncSession, request_id: UUID, admission_token: UUID, *, multi_workspace_enabled: bool,
) -> bool:
    """Extend a still-valid, still-current slot to now+120 s; never resurrect expired occupancy.

    Revalidates Source status and generation/connector/backend revisions under the Source ->
    provisioning lock. A paused, disabled, deleted or reconfigured source cancels the request and
    frees the slot, so the executor sees False and sends nothing more. (Extending the ingestion
    source lease belongs to the C3 owner API.)
    """
    from modules.sources import public as sources

    peek = await session.get(ConnectorCollectionRequest, request_id)
    if peek is None or peek.status != "running" or peek.active_admission_token != admission_token:
        await session.rollback()
        return False
    scope = _request_scope(peek)
    source_id = peek.source_id
    await session.rollback()
    reason: str | None = None
    try:
        await connectors._connector_access(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        source_fence, row, _ = await provisioning.lock_connector(
            session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        source = await sources.get_connector_source(
            session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        request = await session.scalar(
            select(ConnectorCollectionRequest).where(ConnectorCollectionRequest.id == request_id)
            .with_for_update().execution_options(populate_existing=True))
        if request is None or request.status != "running" or request.active_admission_token != admission_token:
            await session.rollback()
            return False
        reason = await _stale_reason(
            session, request, scope, source_fence, source, row, multi_workspace_enabled=multi_workspace_enabled)
    except HTTPException:
        reason = "access_lost"
    if reason is not None:
        await session.rollback()
        await settle_admission(session, request_id, admission_token, outcome="cancelled", error_code=reason)
        return False
    now = datetime.now(UTC)
    result = await session.execute(
        update(ConnectorAdmissionSlot)
        .where(
            ConnectorAdmissionSlot.occupied_request_id == request_id,
            ConnectorAdmissionSlot.admission_token == admission_token,
            ConnectorAdmissionSlot.expires_at > now)
        .values(expires_at=now + SLOT_TTL))
    await session.commit()
    return bool(getattr(result, "rowcount", 0))


async def settle_admission_in_uow(
    session: AsyncSession, request_id: UUID, admission_token: UUID, *, outcome: Outcome,
    error_code: str | None = None, ingestion_run_id: UUID | None = None,
    retryable: bool = False, provider_deadline: datetime | None = None,
) -> bool:
    """Flush a request outcome and free its slot if the token is still current; no commit.

    Returns False for a stale token, so an old worker can never settle a successor. Retryable
    failures requeue with retry_at until five attempts are spent; action-required codes gate the
    schedule instead of retrying; deferral refunds the attempt.
    """
    request = await session.scalar(
        select(ConnectorCollectionRequest).where(ConnectorCollectionRequest.id == request_id)
        .with_for_update().execution_options(populate_existing=True))
    if request is None or request.status != "running" or request.active_admission_token != admission_token:
        return False
    await session.execute(
        update(ConnectorAdmissionSlot)
        .where(ConnectorAdmissionSlot.occupied_request_id == request_id,
               ConnectorAdmissionSlot.admission_token == admission_token)
        .values(**_FREE_SLOT))
    schedule = await session.scalar(
        select(ConnectorSchedule).where(ConnectorSchedule.source_id == request.source_id)
        .with_for_update().execution_options(populate_existing=True))
    now = datetime.now(UTC)
    request.active_admission_token = None
    if provider_deadline is not None:
        request.provider_deadline = provider_deadline
    if outcome in ("succeeded", "no_changes"):
        request.status, request.error_code, request.ingestion_run_id = outcome, None, ingestion_run_id
        if schedule is not None:
            schedule.failure_count, schedule.next_eligible_at = 0, None
    elif outcome == "cancelled":
        request.status, request.error_code = "cancelled", error_code
    elif outcome == "deferred":
        request.status, request.attempt = "queued", max(request.attempt - 1, 0)
        request.available_at = request.enqueue_next_at = now + BUSY_DEFER
    elif error_code in ACTION_REQUIRED:
        request.status, request.error_code = "failed", error_code
        if schedule is not None:
            schedule.blocked_error_code = error_code
            schedule.blocked_dimensions = [ACTION_REQUIRED[error_code]]
            schedule.blocked_connector_revision = request.connector_revision
            schedule.blocked_credential_revision = request.credential_revision
            schedule.blocked_terms_revision = request.terms_revision
    elif retryable and request.attempt < MAX_ATTEMPTS:
        request.status, request.error_code = "queued", error_code
        request.available_at = request.enqueue_next_at = retry_at(now, request.attempt, request.provider_deadline)
    else:
        request.status, request.error_code = "failed", error_code or "failed"
        _penalize(schedule, request.provider_deadline)
    await session.flush()
    return True


async def settle_admission(session: AsyncSession, request_id: UUID, admission_token: UUID, **kwargs: object) -> bool:
    """Settle and commit; used where no ingestion transaction composes the outcome."""
    try:
        settled = await settle_admission_in_uow(session, request_id, admission_token, **kwargs)  # type: ignore[arg-type]
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    return settled


async def clear_collection_block(
    session: AsyncSession, source_id: UUID, *, connector_revision: int | None = None,
    credential_revision: int | None = None, terms_revision: int | None = None,
) -> bool:
    """Lift an action-required gate; flush only, the caller commits (C4/P1 call this).

    Rule: every blocked dimension (config, credential, terms) must now be at a revision strictly
    newer than the one it failed at, and the caller must have just passed fresh validation or
    eligibility. An unrelated revision bump or a replayed old operation leaves the gate in place.
    The failed request stays failed; corrected work creates a new request.
    """
    schedule = await session.get(ConnectorSchedule, source_id, with_for_update=True)
    if schedule is None or schedule.blocked_error_code is None:
        return False
    now_revision = {"config": connector_revision, "credential": credential_revision, "terms": terms_revision}
    failed_at = {
        "config": schedule.blocked_connector_revision, "credential": schedule.blocked_credential_revision,
        "terms": schedule.blocked_terms_revision,
    }
    for dimension in schedule.blocked_dimensions or ():
        current = now_revision[dimension]
        if current is None or (failed_at[dimension] is not None and current <= failed_at[dimension]):
            return False
    schedule.blocked_error_code = None
    schedule.blocked_dimensions = None
    schedule.blocked_connector_revision = schedule.blocked_credential_revision = schedule.blocked_terms_revision = None
    await session.flush()
    return True


async def admit_managed_collection(
    session: AsyncSession, scope: Scope, source_id: UUID, fence: CollectionAdmissionRequest,
    *, trigger: Literal["manual", "scheduled"] = "scheduled", multi_workspace_enabled: bool,
) -> CollectionAdmissionRead:
    """Admit a managed n8n run: an existing current request first, else a new one, else 409.

    A queued request (manual, busy-deferred or retry) is admissible without waiting for a tick.
    Only a brand-new "scheduled" request needs the schedule enabled and due; "manual" does not.
    The caller (service-token route) supplies the original scope and trigger. A busy
    source/workspace/slot or an already running request raises 409 so n8n skips provider I/O.
    """
    try:
        request = await _open_request(
            session, scope, source_id, trigger, None, multi_workspace_enabled=multi_workspace_enabled,
            existing_first=True)
        if (
            request.captured_backend != "n8n" or request.source_generation != fence.source_generation
            or request.connector_revision != fence.connector_revision
            or request.backend_revision != fence.backend_revision
        ):
            raise HTTPException(status_code=409, detail="Managed collection fence is stale")
        request_id = request.id
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    admission = await admit_collection_request(
        session, request_id, multi_workspace_enabled=multi_workspace_enabled)
    if admission is None:
        raise HTTPException(status_code=409, detail="Collection is busy")
    return admission


async def _recover_expired_slots(factory: async_sessionmaker[AsyncSession], now: datetime) -> int:
    """Fence and clear expired occupants one per transaction; requests requeue through settlement.

    An expired slot does not free the source lease: the next admission still waits for it.
    """
    async with factory() as session:
        expired = (await session.execute(
            select(ConnectorAdmissionSlot.slot_id, ConnectorAdmissionSlot.occupied_request_id,
                   ConnectorAdmissionSlot.admission_token)
            .where(ConnectorAdmissionSlot.expires_at <= now).order_by(ConnectorAdmissionSlot.slot_id)
        )).all()
    for slot_id, request_id, token in expired:
        async with factory() as session:
            if not await settle_admission_in_uow(
                session, request_id, token, outcome="failed", error_code="admission_expired", retryable=True,
            ):
                await session.execute(
                    update(ConnectorAdmissionSlot)
                    .where(ConnectorAdmissionSlot.slot_id == slot_id, ConnectorAdmissionSlot.admission_token == token)
                    .values(**_FREE_SLOT))
            await session.commit()
    return len(expired)


async def _create_due_requests(factory: async_sessionmaker[AsyncSession], now: datetime, multi: bool) -> int:
    """Create at most one scheduled request per workspace for up to 50 workspaces.

    Fair turns: workspaces and sources are ordered by persisted last_considered_at, and
    consideration is committed before scope work so a busy workspace cannot starve others.
    """
    from modules.settings.public import module_is_enabled

    async with factory() as session:
        picked = list((await session.execute(text("""
            SELECT t.source_id, t.workspace_id FROM (
              SELECT s.source_id, s.workspace_id, row_number() OVER (
                PARTITION BY s.workspace_id
                ORDER BY coalesce(s.last_considered_at, '-infinity'::timestamptz), s.next_due_at, s.source_id) AS rn
              FROM connector_schedules s JOIN connector_provisioning p ON p.source_id = s.source_id
              WHERE s.enabled AND s.next_due_at <= :now AND s.blocked_error_code IS NULL
                AND (s.next_eligible_at IS NULL OR s.next_eligible_at <= :now)
                AND p.execution_backend = 'native' AND p.state = 'active' AND p.desired_enabled
            ) t LEFT JOIN connector_workspace_dispatch d ON d.workspace_id = t.workspace_id
            WHERE t.rn = 1 ORDER BY d.last_considered_at NULLS FIRST, t.workspace_id LIMIT :limit
        """), {"now": now, "limit": TICK_WORKSPACES})).all())
        if not picked:
            return 0
        await session.execute(
            update(ConnectorSchedule).where(ConnectorSchedule.source_id.in_([row[0] for row in picked]))
            .values(last_considered_at=now))
        await session.execute(
            pg_insert(ConnectorWorkspaceDispatch).values([{"workspace_id": row[1], "last_considered_at": now} for row in picked])
            .on_conflict_do_update(
                index_elements=["workspace_id"], set_={"last_considered_at": now}))
        await session.commit()
    created = 0
    for source_id, workspace_id in picked:
        async with factory() as session:
            owner = await session.scalar(
                select(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == workspace_id, WorkspaceMembership.role == "owner"))
            row = await session.get(ConnectorProvisioning, source_id)
            if owner is None or row is None:
                continue
            scope = InternalJobScope(
                workspace_id=workspace_id, actor_user_id=owner.user_id, membership_revision=owner.revision,
                source_id=source_id, source_generation=row.source_generation)
            await session.rollback()
            if not await module_is_enabled(session, "connectors", scope=scope, multi_workspace_enabled=multi):
                await session.rollback()
                continue
            try:
                request = await _open_request(
                    session, scope, source_id, "scheduled", None, multi_workspace_enabled=multi)
                created += request.status == "queued"
                await session.execute(
                    pg_insert(ConnectorWorkspaceDispatch).values(workspace_id=workspace_id, last_dispatched_at=now)
                    .on_conflict_do_update(index_elements=["workspace_id"], set_={"last_dispatched_at": now}))
                await session.commit()
            except HTTPException:
                await session.rollback()
    return created


async def _enqueue_queued(ctx: dict[str, object], factory: async_sessionmaker[AsyncSession], now: datetime) -> int:
    """Claim due native requests with an expiring token, then enqueue only their UUIDs.

    A failed or lost enqueue leaves the durable queued row; its claim expires and enqueue_next_at
    brings it back. n8n requests are excluded: the managed template owns their delivery.
    """
    claim = uuid4()
    async with factory() as session:
        due = (
            select(ConnectorCollectionRequest.id)
            .where(
                ConnectorCollectionRequest.status == "queued",
                ConnectorCollectionRequest.captured_backend == "native",
                ConnectorCollectionRequest.available_at <= now,
                ConnectorCollectionRequest.enqueue_next_at <= now,
                or_(ConnectorCollectionRequest.enqueue_claim_expires_at.is_(None),
                    ConnectorCollectionRequest.enqueue_claim_expires_at <= now))
            .order_by(ConnectorCollectionRequest.enqueue_next_at).limit(TICK_WORKSPACES)
            .with_for_update(skip_locked=True))
        ids = list((await session.scalars(
            update(ConnectorCollectionRequest).where(ConnectorCollectionRequest.id.in_(due))
            .values(enqueue_claim_token=claim, enqueue_claim_expires_at=now + ENQUEUE_CLAIM,
                    enqueue_next_at=now + REENQUEUE_AFTER)
            .returning(ConnectorCollectionRequest.id))).all())
        await session.commit()
    enqueued = 0
    redis = ctx["redis"]
    for request_id in ids:
        try:
            job = await redis.enqueue_job(  # type: ignore[attr-defined]
                "process_collection_request", str(request_id), _job_id=f"collection:{claim}:{request_id}")
        except Exception:
            continue
        enqueued += job is not None
    return enqueued


async def dispatch_due_collections(ctx: dict[str, object]) -> int:
    """ARQ cron (every 15 s): recover expired slots, create due native requests, enqueue ids.

    Returns the number of requests enqueued. Safe to run concurrently and after Redis loss.
    """
    settings = cast(Settings, ctx["settings"])
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    now = datetime.now(UTC)
    await _recover_expired_slots(factory, now)
    await _create_due_requests(factory, now, settings.multi_workspace_enabled)
    return await _enqueue_queued(ctx, factory, datetime.now(UTC))

