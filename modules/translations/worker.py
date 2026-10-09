"""Durable translation runtime: fair claim, global fenced slot, per-send re-authorization, fenced publish.

Statuses stay within the T1 CHECK set, so "running" means ``pending`` plus a live ``lease_token``.
The gateway runs under the workspace owner's scope (OD-B); authorization stays the requesting
actor's. The worker never INSERTs, so a purged row is never resurrected. No text or marker map
is logged; only counts and latency are recorded.
"""

import asyncio
import contextlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from core.config import Settings
from core.model_gateway.client import ModelGateway, ModelGatewayError
from core.model_gateway.schemas import AIExecutionConfig, RequestPolicy
from core.telemetry import count, observe_ms
from core.workspaces.public import (
    lock_access_fence,
    resolve_workspace_context,
    resolve_workspace_owner_context,
)
from core.workspaces.schemas import InternalJobScope, WorkspaceContext
from modules.settings import public as settings_public
from modules.settings.models import TranslationSettingsRecord
from modules.translations import public
from modules.translations.inputs import load_translation_input
from modules.translations.lifecycle import PAGE, expire_page, orphan_page
from modules.translations.models import ContentTranslation, TranslationAdmissionSlot
from modules.translations.schemas import (
    TranslationInput,
    TranslationItemRequest,
    TranslationPayload,
)
from modules.translations.service import ALIAS, TranslationBlocked, content_hash, translate_input

LEASE_SECONDS = 120
SLOT_TTL_SECONDS = 120
HEARTBEAT_SECONDS = 20
ATTEMPT_TIMEOUT_SECONDS = 20
DEADLINE_SECONDS = 90
SLOT_WAIT_SECONDS = 30
MAX_ATTEMPTS = 2
RECOVER_LIMIT = 50
RECOVER_AGE = timedelta(seconds=30)
RETRY_DELAY = timedelta(seconds=30)
CURSOR_KEY = "translation_orphans:cursor"
# Codes that end in `blocked` (a policy/authorization/capability outcome); other terminals are `failed`.
BLOCKED_CODES = frozenset({
    "privacy_blocked", "model_capability_missing", "translation_disabled", "stale_request",
    "resource_unavailable", "input_too_large",
})
RETRYABLE_CODES = frozenset({"deadline_exceeded"})
_CLEAR: dict[str, Any] = {"lease_token": None, "lease_expires_at": None}


@dataclass(frozen=True, slots=True)
class Claim:
    """One leased row; carries only identity and fingerprint parts, never text."""

    id: UUID
    workspace_id: UUID
    actor_user_id: int
    lease_token: UUID
    attempt: int
    resource_type: str
    resource_id: UUID
    resource_revision: str
    content_hash: str
    visibility_hash: str
    target_language: str
    config_hash: str


def _factory(ctx: dict[str, object]) -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], ctx["session_factory"])


def _ready(now: datetime) -> Any:
    """Pending, not live-leased, past its retry time."""
    c = ContentTranslation
    return and_(
        c.status == "pending",
        or_(c.lease_token.is_(None), c.lease_expires_at <= now),
        or_(c.next_attempt_at.is_(None), c.next_attempt_at <= now),
    )


async def claim_next(session: AsyncSession, now: datetime | None = None) -> Claim | None:
    """Lease the next row fairly: one head per workspace (oldest first), least recently served workspace first.

    Caller commits. ponytail: the served-order subquery scans a workspace's rows; index it if
    a workspace ever holds far more than the 30-day retained cache.
    """
    now = now or datetime.now(UTC)
    c, other = ContentTranslation, aliased(ContentTranslation)
    heads = (select(c.id).ext(distinct_on(c.workspace_id)).where(_ready(now), c.attempt_count < MAX_ATTEMPTS)
             .order_by(c.workspace_id, c.created_at))
    served = select(func.max(other.completed_at)).where(other.workspace_id == c.workspace_id).correlate(c).scalar_subquery()
    row = await session.scalar(
        select(c).where(c.id.in_(heads)).order_by(served.asc().nulls_first(), c.created_at)
        .limit(1).with_for_update(skip_locked=True).execution_options(populate_existing=True))
    if row is None:
        return None
    token = uuid4()
    row.lease_token, row.lease_expires_at = token, now + timedelta(seconds=LEASE_SECONDS)
    row.attempt_count += 1
    await session.flush()
    return Claim(
        row.id, row.workspace_id, row.actor_user_id, token, row.attempt_count, row.resource_type,
        row.resource_id, row.resource_revision, row.content_hash, row.visibility_hash,
        row.target_language, row.config_hash)


# --- global admission slot -------------------------------------------------------------------

async def acquire_slot(session: AsyncSession, translation_id: UUID, now: datetime | None = None) -> int | None:
    """Take the single slot if free or expired; returns the new fencing token, else None. Caller commits."""
    now = now or datetime.now(UTC)
    s = TranslationAdmissionSlot
    return await session.scalar(update(s).where(
        s.id == 1, or_(s.expires_at.is_(None), s.expires_at < now),
    ).values(translation_id=translation_id, fencing_token=s.fencing_token + 1,
             expires_at=now + timedelta(seconds=SLOT_TTL_SECONDS)).returning(s.fencing_token))


async def renew_slot(session: AsyncSession, translation_id: UUID, token: int, now: datetime | None = None) -> bool:
    """Extend the slot only for the current fencing token; a taken-over (stale) holder gets False."""
    now = now or datetime.now(UTC)
    s = TranslationAdmissionSlot
    result = await session.execute(update(s).where(
        s.id == 1, s.translation_id == translation_id, s.fencing_token == token,
    ).values(expires_at=now + timedelta(seconds=SLOT_TTL_SECONDS)).returning(s.id))
    return result.first() is not None


async def release_slot(session: AsyncSession, translation_id: UUID, token: int) -> None:
    """Free the slot if (and only if) this holder's token still owns it."""
    s = TranslationAdmissionSlot
    await session.execute(update(s).where(
        s.id == 1, s.translation_id == translation_id, s.fencing_token == token,
    ).values(translation_id=None, expires_at=None))


# --- row state -------------------------------------------------------------------------------

def _mine(claim: Claim) -> Any:
    c = ContentTranslation
    return and_(c.id == claim.id, c.lease_token == claim.lease_token, c.status == "pending")


async def _finish(factory: async_sessionmaker[AsyncSession], claim: Claim, **values: Any) -> bool:
    """Fenced terminal/retry update; 0 rows (purged or lease lost) is silently fine."""
    async with factory() as session:
        result = await session.execute(
            update(ContentTranslation).where(_mine(claim)).values(**values).returning(ContentTranslation.id))
        done = result.first() is not None
        await session.commit()
        return done


async def _block(factory: async_sessionmaker[AsyncSession], claim: Claim, code: str) -> None:
    status = "blocked" if code in BLOCKED_CODES else "failed"
    await _finish(factory, claim, status=status, error_code=code, completed_at=datetime.now(UTC), **_CLEAR)
    count("translation_jobs_total", outcome=status)


async def _retry_or_fail(factory: async_sessionmaker[AsyncSession], claim: Claim, code: str) -> None:
    if claim.attempt >= MAX_ATTEMPTS:
        await _finish(factory, claim, status="failed", error_code=code, completed_at=datetime.now(UTC), **_CLEAR)
        count("translation_jobs_total", outcome="failed")
        return
    await _finish(factory, claim, next_attempt_at=datetime.now(UTC) + RETRY_DELAY, **_CLEAR)
    count("translation_jobs_total", outcome="retry")


# --- run -------------------------------------------------------------------------------------

@dataclass(slots=True)
class _Run:
    factory: async_sessionmaker[AsyncSession]
    settings: Settings
    redis: Redis
    claim: Claim
    member: WorkspaceContext
    gateway_scope: InternalJobScope
    config: AIExecutionConfig | None = None


async def _verify(run: _Run, session: AsyncSession) -> TranslationInput:
    """Re-authorize in the caller's short transaction (caller rolls back before any network I/O).

    Order: access fence, then the row/lease, settings, owner input, the whole fingerprint
    (resource revision, content, visibility, settings revision, AI policy) and the AI config.
    """
    claim, mw = run.claim, run.settings.multi_workspace_enabled
    try:
        await lock_access_fence(session, scope=run.member, multi_workspace_enabled=mw)
        if await session.scalar(select(ContentTranslation.id).where(_mine(claim))) is None:
            raise TranslationBlocked("stale_request")
        row = await session.scalar(select(TranslationSettingsRecord).where(
            TranslationSettingsRecord.workspace_id == claim.workspace_id).execution_options(populate_existing=True))
        if row is None or not row.enabled:
            raise TranslationBlocked("translation_disabled")
        source = await load_translation_input(session, run.member, TranslationItemRequest(
            resource_type=cast(Any, claim.resource_type), resource_id=claim.resource_id,
            resource_revision=claim.resource_revision), multi_workspace_enabled=mw)
        if source is None:
            raise TranslationBlocked("resource_unavailable")
        policy_fp = await public.policy_fingerprint(session, run.settings, run.redis, claim.workspace_id)
        current = public.config_hash_for(
            workspace_id=claim.workspace_id, actor_user_id=claim.actor_user_id,
            resource_revision=source.resource_revision, content_hash=content_hash(source),
            visibility_hash=source.visibility_hash, target_language=row.target_language,
            settings_revision=row.configuration_revision, policy_fingerprint=policy_fp)
        if current != claim.config_hash or source.resource_revision != claim.resource_revision:
            raise TranslationBlocked("stale_request")
        if run.config is not None:
            await settings_public.check_ai_execution_config(
                session, run.settings, run.redis, scope=run.gateway_scope, expected=run.config)
    except HTTPException as exc:
        raise TranslationBlocked("stale_request") from exc
    return source


async def _checked(run: _Run) -> TranslationInput:
    """One fresh short transaction, always rolled back before the caller does network I/O."""
    async with run.factory() as session:
        try:
            return await _verify(run, session)
        finally:
            await session.rollback()


async def _publish(run: _Run, source: TranslationInput, output: dict[str, str]) -> None:
    """Fenced publish: same checks under the locks, then an update guarded by the lease (never an INSERT)."""
    claim = run.claim
    async with run.factory() as session:
        try:
            await _verify(run, session)
        except TranslationBlocked as exc:
            await session.rollback()
            await _block(run.factory, claim, exc.code)
            return
        unchanged = output == source.fields
        payload = None if unchanged else TranslationPayload(**output).model_dump(mode="json", exclude_none=True)
        result = await session.execute(update(ContentTranslation).where(_mine(claim)).values(
            status="unchanged" if unchanged else "ready", result=payload, error_code=None,
            completed_at=datetime.now(UTC), **_CLEAR).returning(ContentTranslation.id))
        applied = result.first() is not None
        await session.commit()
    count("translation_jobs_total", outcome=("unchanged" if unchanged else "ready") if applied else "purged")
    count("translation_results_total", resource_type=claim.resource_type, target_language=claim.target_language,
          outcome=("unchanged" if unchanged else "ready") if applied else "purged")


async def _heartbeat(run: _Run, token: int) -> None:
    """Renew slot and lease every 20 s; returns (ending the job) when the fencing token went stale."""
    claim = run.claim
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        async with run.factory() as session:
            if not await renew_slot(session, claim.id, token):
                return
            await session.execute(update(ContentTranslation).where(_mine(claim)).values(
                lease_expires_at=datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS)))
            await session.commit()


async def _acquire(run: _Run) -> int | None:
    """Wait up to SLOT_WAIT_SECONDS for the single slot (no DB connection held while sleeping)."""
    end = time.monotonic() + SLOT_WAIT_SECONDS
    while True:
        async with run.factory() as session:
            token = await acquire_slot(session, run.claim.id)
            await session.commit()
        if token is not None or time.monotonic() >= end:
            return token
        await asyncio.sleep(1)


async def _translate(run: _Run, source: TranslationInput) -> dict[str, str]:
    """Build the gateway under the owner's config and translate; every send re-verifies first."""
    config = run.config
    if config is None:
        raise RuntimeError("AI config must be resolved before translating")
    mapping, destination = config.aliases.get(ALIAS), config.endpoint_destination_id
    if mapping is None or not destination or config.endpoint_policy_denied:
        raise TranslationBlocked("model_capability_missing")
    policy = RequestPolicy(
        workspace_id=config.workspace_id, actor_user_id=config.actor_user_id,
        membership_revision=config.membership_revision, gateway_identity=config.gateway_identity,
        reasoning_allowed=config.privacy.allow_remote_reasoning, local_only=source.local_only,
        permitted_destinations=frozenset({destination}),
        reasoning_destinations=frozenset(config.privacy.reasoning_destinations),
        configuration_revision=config.configuration_revision)

    async def before_send() -> None:
        """Runs before every gateway attempt and every Brief segment; raises to abort the send."""
        await _checked(run)

    gateway = ModelGateway(
        redis=run.redis, base_url=config.omniroute_base_url, api_key=config.omniroute_api_key,
        destination_id=destination, timeout_seconds=min(config.request_timeout_seconds, ATTEMPT_TIMEOUT_SECONDS),
        scope=run.gateway_scope, gateway_identity=config.gateway_identity,
        configuration_revision=config.configuration_revision, before_send=before_send,
        approved_endpoint_cidrs=config.endpoint_allowed_cidrs)
    return await translate_input(
        gateway, config, policy, source, cast(Any, run.claim.target_language), before_send,
        deadline=time.monotonic() + DEADLINE_SECONDS)


async def _execute(run: _Run) -> None:
    """Resolve the owner config, verify, translate, publish (all inside the held slot)."""
    async with run.factory() as session:
        try:
            run.config = await settings_public.get_ai_execution_config(
                session, run.settings, run.redis, scope=run.gateway_scope)
        finally:
            await session.rollback()
    source = await _checked(run)
    output = await _translate(run, source)
    await _publish(run, source, output)


async def translate_content(ctx: dict[str, object], translation_id: str) -> None:
    """Claim the next fair row (the id is only a wake-up hint), run it under the slot, publish fenced."""
    del translation_id
    factory, settings = _factory(ctx), cast(Settings, ctx["settings"])
    redis, mw = cast(Redis, ctx["redis"]), settings.multi_workspace_enabled
    started = time.perf_counter()
    async with factory() as session:
        claim = await claim_next(session)
        await session.commit()
    if claim is None:
        return
    try:
        async with factory() as session:
            member = await resolve_workspace_context(session, claim.actor_user_id, claim.workspace_id)
            owner = await resolve_workspace_owner_context(session, claim.workspace_id, multi_workspace_enabled=mw)
            await session.rollback()
        if member is None or owner is None:
            await _block(factory, claim, "resource_unavailable" if member is None else "translation_disabled")
            return
        run = _Run(factory, settings, redis, claim, member, InternalJobScope(
            workspace_id=claim.workspace_id, actor_user_id=owner.user_id, membership_revision=owner.membership_revision))
        token = await _acquire(run)
        if token is None:  # slot busy: hand the attempt back; recovery re-enqueues
            await _finish(factory, claim, attempt_count=ContentTranslation.attempt_count - 1,
                          next_attempt_at=datetime.now(UTC) + timedelta(seconds=5), **_CLEAR)
            return
        work = asyncio.ensure_future(_execute(run))
        beat = asyncio.ensure_future(_heartbeat(run, token))
        try:
            await asyncio.wait({work, beat}, return_when=asyncio.FIRST_COMPLETED)
            if not work.done():  # fencing token went stale: abandon without publishing
                work.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await work
                return
            work.result()
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await beat
            async with factory() as session:
                await release_slot(session, claim.id, token)
                await session.commit()
    except TranslationBlocked as exc:
        if exc.code in RETRYABLE_CODES:
            await _retry_or_fail(factory, claim, exc.code)
        else:
            await _block(factory, claim, exc.code)
    except (ModelGatewayError, OSError, TimeoutError):
        await _retry_or_fail(factory, claim, "transport_error")
    except HTTPException:
        await _block(factory, claim, "stale_request")
    finally:
        observe_ms("translation_job_ms", started)


# --- crons -----------------------------------------------------------------------------------

async def recover_translation_jobs(ctx: dict[str, object]) -> int:
    """Re-enqueue up to 50 stuck rows (unleased >30 s, or lease expired); fail exhausted attempt budgets."""
    factory, now, c = _factory(ctx), datetime.now(UTC), ContentTranslation
    dead = or_(c.lease_token.is_(None), c.lease_expires_at <= now)
    async with factory() as session:
        await session.execute(update(c).where(c.status == "pending", c.attempt_count >= MAX_ATTEMPTS, dead).values(
            status="failed", error_code="attempts_exhausted", completed_at=now, **_CLEAR))
        ids = list((await session.scalars(select(c.id).where(
            _ready(now), c.attempt_count < MAX_ATTEMPTS,
            or_(c.lease_expires_at <= now, and_(c.lease_token.is_(None), c.updated_at <= now - RECOVER_AGE)),
        ).order_by(c.created_at).limit(RECOVER_LIMIT))).all())
        await session.commit()
    redis = cast(Any, ctx["redis"])
    for translation_id in ids:
        with contextlib.suppress(Exception):
            await redis.enqueue_job("translate_content", str(translation_id), _job_id=f"translation:{translation_id}")
    return len(ids)


async def sweep_translation_orphans(ctx: dict[str, object]) -> int:
    """Delete cache rows whose resource is gone or no longer visible to the row's actor (one page of 100)."""
    factory, settings = _factory(ctx), cast(Settings, ctx["settings"])
    cursor = cast(dict[str, str], ctx.setdefault("w2_cursor_state", {}))
    after = UUID(cursor[CURSOR_KEY]) if CURSOR_KEY in cursor else None
    doomed: list[UUID] = []
    async with factory() as session:
        rows = await orphan_page(session, after)
        for row in rows:
            member = await resolve_workspace_context(session, row.actor_user_id, row.workspace_id)
            try:
                source = None if member is None else await load_translation_input(
                    session, member, TranslationItemRequest(
                        resource_type=cast(Any, row.resource_type), resource_id=row.resource_id,
                        resource_revision=row.resource_revision),
                    multi_workspace_enabled=settings.multi_workspace_enabled)
            except HTTPException:
                continue  # transient/fence error: judge it next sweep
            if source is None:
                doomed.append(row.id)
        if doomed:
            await session.execute(delete(ContentTranslation).where(
                ContentTranslation.id.in_(doomed), or_(
                    ContentTranslation.lease_token.is_(None), ContentTranslation.lease_expires_at <= datetime.now(UTC))))
        await session.commit()
    if len(rows) < PAGE:
        cursor.pop(CURSOR_KEY, None)
    else:
        cursor[CURSOR_KEY] = str(rows[-1].id)
    count("translation_orphans_total", len(doomed))
    return len(doomed)


async def expire_translations(ctx: dict[str, object]) -> int:
    """Nightly: delete expired rows (never a live-leased one) and batches in pages of 100."""
    factory, total = _factory(ctx), 0
    for _ in range(100):  # bounded: at most 10_000 rows per night per table
        async with factory() as session:
            removed = await expire_page(session)
            await session.commit()
        total += removed
        if removed == 0:
            break
    return total
