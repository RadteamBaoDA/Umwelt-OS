"""GitHub webhook outbox fan-out, hint recovery, ``dispatch_github_webhooks`` and collection request processing."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.errors import DomainError
from core.worker_context import worker_session_factory
from core.workspaces.schemas import InternalJobScope
from modules.connectors._worker.base import (
    _DENIED,
    _admit_source_job,
    _cursor_ctx,
)
from modules.connectors._worker.grants import (
    expire_github_webhook_details,
    reclaim_github_webhook_digests,
    revoke_deleted_source_github_grants,
)
from modules.connectors.models import (
    GithubSourceHint,
    GithubWebhookCapacity,
    GithubWebhookDelivery,
    GithubWebhookOutbox,
)
from modules.sources import public as sources

logger = logging.getLogger("modules.connectors.worker")  # same logger object as the facade


def _webhook_retry_delay(attempt: int) -> int:
    """Bound durable webhook dispatch retry spacing between 30 seconds and one hour."""
    return int(min(30 * (2 ** max(0, attempt - 1)), 3600))


_OUTBOX_OPEN_STATES = frozenset({"pending", "dispatched", "needs_attention"})


async def _snapshot_outbox(
    factory: async_sessionmaker[AsyncSession],
    outbox_id: UUID,
) -> tuple[Any, Any, dict[str, Any]] | None:
    """Read an outbox and its delivery without holding locks and return a detached snapshot.

    Returns ``(binding_cursor, fanout_page, delivery_fields)`` or ``None`` when either row is
    gone. The read session is always rolled back before returning, so no row stays locked while
    binding discovery and per-binding admission run in their own transactions.
    """
    async with factory() as session:
        outbox = await session.get(GithubWebhookOutbox, outbox_id)
        delivery = await session.get(GithubWebhookDelivery, outbox.delivery_id) if outbox else None
        if outbox is None or delivery is None:
            await session.rollback()
            return None
        snapshot: dict[str, Any] = {
            "id": delivery.id,
            "app_id": delivery.app_id,
            "installation_id": delivery.installation_id,
            "repository_id": delivery.repository_id,
            "targets": delivery.targets,
        }
        cursor, stored_page = outbox.binding_cursor, outbox.fanout_page
        await session.rollback()
    return cursor, stored_page, snapshot


async def _complete_outbox_without_installation(
    factory: async_sessionmaker[AsyncSession],
    outbox_id: UUID,
    cursor: Any,
    stored_page: Any,
) -> bool:
    """Complete an outbox whose delivery has no installation and release its capacity slot.

    The outbox is locked and compared to the snapshot (state, cursor, page); a changed row is
    left alone. Returns True when the outbox was completed and committed, False when skipped.
    """
    async with factory() as session:
        current = await session.scalar(
            select(GithubWebhookOutbox)
            .where(
                GithubWebhookOutbox.id == outbox_id,
            )
            .with_for_update()
        )
        if (
            current is None
            or current.state not in _OUTBOX_OPEN_STATES
            or current.binding_cursor != cursor
            or current.fanout_page != stored_page
        ):
            await session.rollback()
            return False
        capacity = await session.scalar(
            select(GithubWebhookCapacity)
            .where(
                GithubWebhookCapacity.id == 1,
            )
            .with_for_update()
        )
        current.state = "complete"
        current.binding_cursor = None
        current.fanout_page = None
        if current.capacity_reserved and capacity is not None and capacity.pending_count > 0:
            capacity.pending_count -= 1
        if current.capacity_reserved:
            current.capacity_reserved = False
        await session.commit()
    return True


async def _resolve_fanout_page(
    factory: async_sessionmaker[AsyncSession],
    outbox_id: UUID,
    cursor: Any,
    stored_page: Any,
    delivery: dict[str, Any],
    *,
    flag: bool,
) -> tuple[Any, bool] | None:
    """Return the frozen fanout page for an outbox (reusing or freezing one) and a failure flag.

    A stored page is revalidated against the outbox cursor. Otherwise binding snapshots are
    fetched with explicit instance-operator admission and no row locks, converted to the strict
    private DTO, then frozen onto the outbox under its lock with an exact cursor re-check. Any
    discovery or validation error sets the failure flag (the caller schedules a retry). Returns
    ``None`` when the outbox changed or vanished while freezing, meaning skip it this pass.
    """
    from modules.connectors import public as connectors
    from modules.connectors.public import (
        _GitHubFanoutBinding,
        _GitHubFanoutPage,
        _validated_github_fanout_page,
    )

    fanout_page: _GitHubFanoutPage | None = None
    failed = False
    if stored_page is not None:
        try:
            candidate = _validated_github_fanout_page(stored_page)
            if candidate.cursor_before != cursor:
                raise ValueError("github_fanout_page_cursor_stale")
            fanout_page = candidate
        except ValueError:
            failed = True
        return fanout_page, failed
    detached_bindings = None
    try:
        async with factory() as session:
            detached_bindings = await connectors.list_github_event_bindings(
                session,
                app_id=str(delivery["app_id"]),
                installation_id=str(delivery["installation_id"]),
                repository_id=str(delivery["repository_id"]) if delivery["repository_id"] else None,
                limit=50,
                cursor=cursor,
                multi_workspace_enabled=flag,
                instance_operator=True,
            )
            await session.rollback()
    except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
        failed = True
    if detached_bindings is None:
        return fanout_page, failed
    try:
        # Convert the public DTO to a mapping explicitly; strict private DTO validation
        # does not coerce a distinct Pydantic model instance by attribute name.
        frozen_bindings = tuple(
            _GitHubFanoutBinding.model_validate(item.model_dump(mode="python"))
            for item in detached_bindings.items
        )
        candidate = _GitHubFanoutPage(
            cursor_before=cursor,
            cursor_after=detached_bindings.next_cursor,
            bindings=frozen_bindings,
            admissions=(),
        )
    except (TypeError, ValueError):
        return fanout_page, True
    async with factory() as session:
        current = await session.scalar(
            select(GithubWebhookOutbox)
            .where(
                GithubWebhookOutbox.id == outbox_id,
            )
            .with_for_update()
        )
        if (
            current is None
            or current.state not in _OUTBOX_OPEN_STATES
            or current.binding_cursor != cursor
        ):
            await session.rollback()
            return None
        if current.fanout_page is None:
            current.fanout_page = candidate.model_dump(mode="json")
            await session.commit()
            fanout_page = candidate
        else:
            try:
                persisted = _validated_github_fanout_page(current.fanout_page)
                if persisted.cursor_before != cursor:
                    raise ValueError("github_fanout_page_cursor_stale")
                fanout_page = persisted
                await session.rollback()
            except ValueError:
                await session.rollback()
                failed = True
    return fanout_page, failed


def _target_applies_to_binding(binding: Any, target: Any, delivery_repository_id: Any) -> bool:
    """Return whether a delivery target concerns this binding's installation and repository.

    Installation/repository-scoped targets must name the binding's own identifiers; any other
    target is restricted to the delivery's repository when the delivery names one.
    """
    if target.locator_kind == "installation" and target.locator != binding.installation_id:
        return False
    if target.locator_kind == "repository" and target.locator != binding.repository_id:
        return False
    return not (
        delivery_repository_id
        and target.locator_kind not in {"installation"}
        and target.locator_kind != "repository"
        and binding.repository_id != delivery_repository_id
    )


async def _admit_binding_target(
    factory: async_sessionmaker[AsyncSession],
    fanout_page: Any,
    binding: Any,
    target: Any,
    delivery_id: Any,
    *,
    flag: bool,
) -> bool:
    """Admit one target for one frozen binding under that binding's original identity.

    Returns True when the whole delivery attempt must be marked failed (capacity exhausted,
    stale progress or an unexpected error) and False otherwise. A disabled module or a denied
    owner (401/403/404/409) only skips this binding; other bindings still proceed.
    """
    from modules.connectors import public as connectors
    from modules.settings.public import module_is_enabled

    try:
        binding_scope = InternalJobScope(
            workspace_id=binding.workspace_id,
            actor_user_id=binding.actor_user_id,
            membership_revision=binding.membership_revision,
            source_id=binding.source_id,
            source_generation=binding.source_generation,
        )
        async with factory() as session:
            if not await module_is_enabled(
                session,
                "connectors",
                scope=binding_scope,
                multi_workspace_enabled=flag,
            ):
                await session.rollback()
                return False
            result = await connectors.enqueue_github_hint(
                session,
                binding=binding,
                delivery_receipt_id=delivery_id,
                target=target,
                expected_binding_cursor=fanout_page.cursor_before,
                scope=binding_scope,
                multi_workspace_enabled=flag,
            )
        return bool(result.disposition in {"capacity_exhausted", "stale_progress"})
    except (DomainError, HTTPException) as exc:
        return exc.status_code not in _DENIED  # a denied binding owner is skipped, not a failure
    except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
        return True


async def _fan_out_page(
    factory: async_sessionmaker[AsyncSession],
    fanout_page: Any,
    delivery: dict[str, Any],
    *,
    flag: bool,
) -> bool:
    """Admit every applicable (binding, target) pair of a frozen page; return True on any failure.

    Bindings of another app/installation/repository are skipped. The first malformed target or
    failed admission stops the page so the outbox attempt is retried as a whole (admissions
    already stored on the page are replayed idempotently by ``enqueue_github_hint``).
    """
    from modules.connectors.github.webhooks import GitHubTargetHint

    for binding in fanout_page.bindings:
        if (
            binding.app_id != delivery["app_id"]
            or binding.installation_id != delivery["installation_id"]
        ):
            continue
        if delivery["repository_id"] and binding.repository_id != delivery["repository_id"]:
            continue
        for raw_target in delivery["targets"]:
            try:
                target = GitHubTargetHint.model_validate(raw_target)
            except (TypeError, ValueError):
                return True
            if not _target_applies_to_binding(binding, target, delivery["repository_id"]):
                continue
            if await _admit_binding_target(
                factory, fanout_page, binding, target, delivery["id"], flag=flag
            ):
                return True
    return False


async def _settle_outbox(
    factory: async_sessionmaker[AsyncSession],
    outbox_id: UUID,
    cursor: Any,
    stored_page: Any,
    fanout_page: Any,
    failed: bool,
    now: datetime,
) -> bool:
    """Persist the fanout outcome on the outbox if the same cursor/page still owns it.

    Failure bumps attempts (``needs_attention`` and a one-hour delay from the fifth), a page with a
    next cursor advances the cursor and clears the ledger, and a final page completes the outbox
    and releases its capacity slot. Returns False (rolled back) when the outbox changed.
    """
    from modules.connectors.public import _validated_github_fanout_page

    async with factory() as session:
        current = await session.scalar(
            select(GithubWebhookOutbox)
            .where(
                GithubWebhookOutbox.id == outbox_id,
            )
            .with_for_update()
        )
        if current is None:
            await session.rollback()
            return False
        if fanout_page is not None:
            try:
                current_page = _validated_github_fanout_page(current.fanout_page)
            except ValueError:
                current_page = None
            if (
                current.state not in _OUTBOX_OPEN_STATES
                or current.binding_cursor != fanout_page.cursor_before
                or current_page is None
                or current_page.cursor_before != fanout_page.cursor_before
                or current_page.cursor_after != fanout_page.cursor_after
                or current_page.bindings != fanout_page.bindings
            ):
                await session.rollback()
                return False
        elif (
            current.state not in _OUTBOX_OPEN_STATES
            or current.binding_cursor != cursor
            or current.fanout_page != stored_page
        ):
            await session.rollback()
            return False
        capacity = await session.scalar(
            select(GithubWebhookCapacity)
            .where(
                GithubWebhookCapacity.id == 1,
            )
            .with_for_update()
        )
        if failed:
            current.attempts = min(5, current.attempts + 1)
            current.state = "needs_attention" if current.attempts >= 5 else "pending"
            current.next_attempt_at = now + timedelta(
                seconds=3600
                if current.state == "needs_attention"
                else _webhook_retry_delay(current.attempts)
            )
        elif fanout_page is not None and fanout_page.cursor_after is not None:
            current.binding_cursor = fanout_page.cursor_after
            current.fanout_page = None
            current.state = "pending"
            current.next_attempt_at = now
        else:
            current.state = "complete"
            current.binding_cursor = None
            current.fanout_page = None
            if current.capacity_reserved and capacity is not None and capacity.pending_count > 0:
                capacity.pending_count -= 1
            if current.capacity_reserved:
                current.capacity_reserved = False
        await session.commit()
    return True


async def _recover_deferred_hints(session: AsyncSession, now: datetime, *, flag: bool) -> None:
    """Retry capacity-deferred hints of the oldest due Source, then push that Source's hints later.

    Runs inside the caller's session and commits once when a deferred Source exists. The Source
    is admitted as a worker subject, locked and reconciled only while active with the connectors
    module enabled; a denied admission (401/403/404/409) is rolled back and swallowed, any other
    HTTP-shaped error propagates. Rescheduling every remaining deferred hint rotates a
    denied/disabled Source out of the queue head.
    """
    from modules.connectors import public as connectors
    from modules.settings.public import module_is_enabled

    deferred_source_id = await session.scalar(
        select(GithubSourceHint.source_id)
        .where(
            GithubSourceHint.state == "capacity_deferred",
            GithubSourceHint.capacity_reserved.is_(False),
            GithubSourceHint.next_attempt_at <= now,
        )
        .order_by(GithubSourceHint.next_attempt_at, GithubSourceHint.updated_at)
        .limit(1)
    )
    if deferred_source_id is None:
        return
    admitted = await _admit_source_job(session, deferred_source_id, multi_workspace_enabled=flag)
    if admitted is not None:
        deferred_scope, deferred_fence = admitted
        try:
            locked = await sources.lock_source(
                session,
                deferred_source_id,
                scope=deferred_scope,
                multi_workspace_enabled=flag,
                expected_access_fence=deferred_fence,
            )
            source = (
                await sources.get_connector_source(
                    session,
                    deferred_source_id,
                    scope=deferred_scope,
                    multi_workspace_enabled=flag,
                )
                if locked is not None
                else None
            )
            if (
                source is not None
                and source.status == "active"
                and await module_is_enabled(
                    session,
                    "connectors",
                    scope=deferred_scope,
                    multi_workspace_enabled=flag,
                )
            ):
                await connectors.reconcile_github_source_hints_lifecycle(
                    session,
                    source_id=deferred_source_id,
                    source_generation=source.generation,
                    active=True,
                    scope=deferred_scope,
                    multi_workspace_enabled=flag,
                )
        except (DomainError, HTTPException) as exc:
            await session.rollback()
            if exc.status_code not in _DENIED:
                raise
    # Pushing the Source's deferred hints later also rotates a denied/disabled Source out of the head.
    retry_base = datetime.now(UTC)
    retry_at = retry_base + timedelta(seconds=60)
    remaining = list(
        (
            await session.scalars(
                select(GithubSourceHint)
                .where(
                    GithubSourceHint.source_id == deferred_source_id,
                    GithubSourceHint.state == "capacity_deferred",
                    GithubSourceHint.capacity_reserved.is_(False),
                    GithubSourceHint.next_attempt_at <= retry_base,
                )
                .order_by(GithubSourceHint.next_attempt_at, GithubSourceHint.id)
                .limit(100)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    for deferred_hint in remaining:
        deferred_hint.next_attempt_at = max(deferred_hint.next_attempt_at, retry_at)
    await session.commit()


async def _claim_due_hint(session: AsyncSession, now: datetime) -> tuple[Any, ...] | None:
    """Claim the oldest due reserved hint for one wake attempt and commit the claim.

    Locks one hint (``skip_locked``), bumps its attempts and marks it ``dispatched`` (or
    ``needs_attention`` with a one-hour delay from the fifth attempt) before any network call.
    Returns ``(id, source_id, source_generation, connector_revision, dirty_revision)``, or
    ``None`` after a rollback when nothing is due.
    """
    hint = await session.scalar(
        select(GithubSourceHint)
        .where(
            GithubSourceHint.state.in_(("pending", "dispatched", "needs_attention")),
            GithubSourceHint.capacity_reserved.is_(True),
            GithubSourceHint.reconcile_page <= 100,
            GithubSourceHint.next_attempt_at <= now,
        )
        .order_by(GithubSourceHint.next_attempt_at, GithubSourceHint.updated_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if hint is None:
        await session.rollback()
        return None
    snapshot = (
        hint.id,
        hint.source_id,
        hint.source_generation,
        hint.connector_revision,
        hint.dirty_revision,
    )
    needs_attention = hint.state == "needs_attention" or hint.attempts >= 4
    hint.attempts = min(5, hint.attempts + 1)
    attempt = hint.attempts
    needs_attention = needs_attention or attempt >= 5
    hint.state = "needs_attention" if needs_attention else "dispatched"
    hint.next_attempt_at = now + timedelta(
        seconds=3600 if needs_attention else _webhook_retry_delay(attempt)
    )
    await session.commit()
    return snapshot


async def _wake_hint_source(
    factory: async_sessionmaker[AsyncSession],
    snapshot: tuple[Any, ...],
    settings: Settings,
    *,
    flag: bool,
) -> Any:
    """Wake the packaged collection for a claimed hint's Source under a fresh worker admission.

    Returns the wake result, or ``None`` when no subject is admissible or the connectors module is
    disabled. A denied admission (401/403/404/409) is rolled back and treated as no wake; other
    HTTP-shaped errors propagate.
    """
    from modules.connectors import public as connectors
    from modules.settings.public import module_is_enabled

    async with factory() as session:
        wake: Any = None
        admitted = await _admit_source_job(session, snapshot[1], multi_workspace_enabled=flag)
        if admitted is not None:
            wake_scope, _wake_fence = admitted
            try:
                if await module_is_enabled(
                    session, "connectors", scope=wake_scope, multi_workspace_enabled=flag
                ):
                    wake = await connectors.wake_packaged_collection(
                        session,
                        source_id=snapshot[1],
                        source_generation=snapshot[2],
                        connector_revision=snapshot[3],
                        settings=settings,
                        timeout_seconds=15,
                        scope=wake_scope,
                        multi_workspace_enabled=flag,
                    )
            except (DomainError, HTTPException) as exc:
                await session.rollback()
                if exc.status_code not in _DENIED:
                    raise
    return wake


async def _settle_hint_after_wake(
    factory: async_sessionmaker[AsyncSession],
    snapshot: tuple[Any, ...],
    wake: Any,
    now: datetime,
) -> None:
    """Record the wake outcome on the claimed hint unless its dirty revision or state moved.

    No wake or a failed wake retries with backoff, an acknowledged wake stays ``dispatched``
    until ingestion acknowledges it, and a deferred wake waits for the provider's eligibility
    time. From the fifth attempt the hint becomes ``needs_attention`` with a one-hour delay.
    A hint changed since the claim is left untouched (rolled back).
    """
    async with factory() as session:
        hint = await session.scalar(
            select(GithubSourceHint)
            .where(
                GithubSourceHint.id == snapshot[0],
            )
            .with_for_update()
        )
        if (
            hint is not None
            and hint.dirty_revision == snapshot[4]
            and hint.state in {"dispatched", "needs_attention"}
        ):
            if wake is None:
                # No admissible subject (or module disabled): retry later, bounded by the attempts cap.
                hint.state = "needs_attention" if hint.attempts >= 5 else "pending"
                hint.next_attempt_at = now + timedelta(
                    seconds=3600
                    if hint.state == "needs_attention"
                    else _webhook_retry_delay(max(1, hint.attempts))
                )
            elif wake.outcome == "acknowledged":
                hint.state = "needs_attention" if hint.attempts >= 5 else "dispatched"
                hint.next_attempt_at = now + timedelta(
                    seconds=3600
                    if hint.state == "needs_attention"
                    else _webhook_retry_delay(max(1, hint.attempts))
                )
            elif wake.outcome == "deferred" and wake.next_eligible_at is not None:
                hint.state = "needs_attention" if hint.attempts >= 5 else "pending"
                hint.next_attempt_at = max(
                    wake.next_eligible_at,
                    now + timedelta(seconds=3600 if hint.state == "needs_attention" else 0),
                )
            elif hint.attempts >= 5:
                hint.state = "needs_attention"
                hint.next_attempt_at = now + timedelta(seconds=3600)
            else:
                hint.state = "pending"
                hint.next_attempt_at = now + timedelta(seconds=_webhook_retry_delay(hint.attempts))
            await session.commit()
        else:
            await session.rollback()


async def dispatch_github_webhooks(ctx: dict[str, object]) -> int:
    """Fan out bounded frozen binding pages with durable target admissions and fair hint recovery.

    Binding snapshots are fetched without Source/outbox locks, then frozen under an exact outbox
    cursor check. Each public binding DTO is explicitly dumped and validated into the stricter
    private persisted DTO, preserving its field, identity, and bounds checks. Per-target admission
    and hint/capacity effects commit together; page advancement clears that ledger only after the
    same cursor/page still owns the result. Deferred hints that cannot revalidate current authority
    get a durable retry deadline and never reserve or wake.

    The receiver ledger (deliveries, outboxes, capacity) is a deliberate instance global, so binding
    discovery runs under explicit operator admission (``instance_operator=True``). Each binding is then
    a separate subject: its real workspace, actor, membership revision, Source and generation come
    from the frozen page entry, ``enqueue_github_hint`` admits that original identity itself, and a
    denied, disabled or vanished binding is skipped (webhook content is only a hint; polling and
    reauthorization remain authoritative) instead of failing the whole delivery for every workspace.
    Deferred-hint recovery and the wake use Recipe S per Source, with the connectors module checked
    per workspace after admission.

    Phases per pass: housekeeping, then per due outbox (snapshot, freeze page, fan out, settle),
    then at most one deferred-Source recovery and one hint wake. Returns the progress count.
    """
    settings = cast(Settings, ctx["settings"])
    factory = worker_session_factory(ctx)
    flag = settings.multi_workspace_enabled
    now = datetime.now(UTC)
    progressed = await expire_github_webhook_details(factory, now=now)
    progressed += await reclaim_github_webhook_digests(factory, now=now)
    progressed += await revoke_deleted_source_github_grants(
        factory, settings, cursor_ctx=_cursor_ctx(ctx)
    )
    async with factory() as session:
        outbox_ids = list(
            (
                await session.scalars(
                    select(GithubWebhookOutbox.id)
                    .where(
                        GithubWebhookOutbox.state.in_(("pending", "dispatched", "needs_attention")),
                        GithubWebhookOutbox.next_attempt_at <= now,
                    )
                    .order_by(GithubWebhookOutbox.next_attempt_at, GithubWebhookOutbox.id)
                    .limit(50)
                )
            ).all()
        )

    for outbox_id in outbox_ids:
        snapshot_rows = await _snapshot_outbox(factory, outbox_id)
        if snapshot_rows is None:
            continue
        cursor, stored_page, delivery = snapshot_rows
        if not delivery["installation_id"]:
            if await _complete_outbox_without_installation(factory, outbox_id, cursor, stored_page):
                progressed += 1
            continue
        resolved = await _resolve_fanout_page(
            factory, outbox_id, cursor, stored_page, delivery, flag=flag
        )
        if resolved is None:
            continue
        fanout_page, failed = resolved
        if fanout_page is not None and not failed:
            failed = await _fan_out_page(factory, fanout_page, delivery, flag=flag)
        if await _settle_outbox(factory, outbox_id, cursor, stored_page, fanout_page, failed, now):
            progressed += 1

    async with factory() as session:
        await _recover_deferred_hints(session, now, flag=flag)
        claimed = await _claim_due_hint(session, now)
    if claimed is None:
        return progressed
    wake = await _wake_hint_source(factory, claimed, settings, flag=flag)
    await _settle_hint_after_wake(factory, claimed, wake, now)
    return progressed + 1


async def process_collection_request(ctx: dict[str, object], request_id: str) -> str:
    """Admit one queued native request and hand it to the registered collection executor.

    The executor defaults to the shared collection service (``collection.execute_collection``);
    ``ctx["collection_executor"]`` overrides it (tests, alternative composition). An executor
    error is a retryable failure, and an executor that already settled the request makes the
    fallback settlement a stale no-op.
    """
    from modules.connectors import scheduler
    from modules.connectors.collection import execute_collection

    executor = ctx.get("collection_executor") or execute_collection
    settings = cast(Settings, ctx["settings"])
    factory = worker_session_factory(ctx)
    async with factory() as session:
        admission = await scheduler.admit_collection_request(
            session, UUID(request_id), multi_workspace_enabled=settings.multi_workspace_enabled
        )
    if admission is None:
        return "deferred"
    try:
        await cast(Any, executor)(ctx, admission)
    except Exception:  # any executor failure must settle the admission as failed
        logger.exception("Collection executor raised; request_id=%s", admission.request_id)
        async with factory() as session:
            await scheduler.settle_admission(
                session,
                admission.request_id,
                admission.admission_token,
                outcome="failed",
                error_code="executor_error",
                retryable=True,
            )
        return "failed"
    return "admitted"
