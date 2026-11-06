"""Extraction work queue claim/requeue/finish operations (private to the timeline package)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces import public as workspaces  # noqa: F401  (patch target for fence tests)
from core.workspaces.schemas import (
    Scope,
)


async def schedule_extraction_work(
    session: AsyncSession,
    ready: Any,
    extractor_version: str,
    prompt_version: str,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> UUID:
    """Idempotently create timeline extraction work and flush without committing the caller transaction."""
    from sqlalchemy.dialects.postgresql import insert

    from modules.timeline.models import TimelineExtractionWork

    statement = (
        insert(TimelineExtractionWork)
        .values(
            workspace_id=scope.workspace_id,
            document_id=ready.document_id,
            document_version_id=ready.document_version_id,
            source_id=ready.source_id,
            source_generation=ready.source_generation,
            extractor_version=extractor_version,
            prompt_version=prompt_version,
        )
        .on_conflict_do_nothing(constraint="uq_timeline_extraction_work_identity")
        .returning(TimelineExtractionWork.id)
    )
    work_id = await session.scalar(statement)
    if work_id is None:
        work_id = await session.scalar(
            select(TimelineExtractionWork.id).where(
                TimelineExtractionWork.workspace_id == scope.workspace_id,
                TimelineExtractionWork.document_version_id == ready.document_version_id,
                TimelineExtractionWork.source_generation == ready.source_generation,
                TimelineExtractionWork.extractor_version == extractor_version,
                TimelineExtractionWork.prompt_version == prompt_version,
            )
        )
    # the conflicting identity row exists
    if work_id is None:
        raise RuntimeError("invariant violated: work_id must not be None")
    await session.flush()
    # GitHub events are produced deterministically by the connector mapper and are canonical;
    # model extraction would only add duplicates. Terminal-block the work (an error code the
    # recheck/requeue paths ignore) so every scheduler, including recovery, skips it.
    from modules.sources import public as sources

    detached = await sources.get_connector_source(
        session, ready.source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if detached is not None and detached.provider == "github":
        work = await session.scalar(
            select(TimelineExtractionWork)
            .where(
                TimelineExtractionWork.workspace_id == scope.workspace_id,
                TimelineExtractionWork.id == work_id,
            )
            .with_for_update()
        )
        if work is not None and work.status not in {"succeeded", "blocked"}:
            work.status, work.error_code = "blocked", "deterministic_provider"
            work.next_attempt_at = datetime.max.replace(tzinfo=UTC)
            work.dependency_fingerprint = None
            work.lease_owner = work.lease_expires_at = None
            await session.flush()
    return work_id


async def claim_extraction_work(
    session: AsyncSession,
    work_id: UUID,
    lease_owner: str,
    now: datetime,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    lease_seconds: int = 110,
) -> Any | None:
    """Claim due work for ``lease_seconds`` (must outlive the model call) or terminalize an exhausted expired lease; flush without committing."""
    from datetime import timedelta

    from modules.timeline.models import TimelineExtractionWork

    work = await session.scalar(
        select(TimelineExtractionWork)
        .where(
            TimelineExtractionWork.workspace_id == scope.workspace_id,
            TimelineExtractionWork.id == work_id,
            (TimelineExtractionWork.status == "pending")
            | (
                (TimelineExtractionWork.status == "running")
                & (TimelineExtractionWork.lease_expires_at <= now)
            ),
            TimelineExtractionWork.next_attempt_at <= now,
        )
        .with_for_update(skip_locked=True)
    )
    if work is None:
        return None
    if work.status == "running" and work.attempt >= 5:
        work.status, work.error_code = "failed", "lease_attempts_exhausted"
        work.lease_owner = work.lease_expires_at = None
        await session.flush()
        return None
    work.status, work.attempt = "running", work.attempt + 1
    work.lease_owner, work.lease_expires_at = lease_owner, now + timedelta(seconds=lease_seconds)
    work.error_code = None
    await session.flush()
    return work


async def list_recoverable_extraction_work(
    session: AsyncSession,
    limit: int = 25,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[UUID]:
    """Return bounded due pending work and expired leases for claim or exhaustion handling."""
    from modules.timeline.models import TimelineExtractionWork

    if not 1 <= limit <= 100:
        raise ValueError("Timeline recovery page must be between 1 and 100")
    now = datetime.now(UTC)
    return list(
        (
            await session.scalars(
                select(TimelineExtractionWork.id)
                .where(
                    TimelineExtractionWork.workspace_id == scope.workspace_id,
                    (
                        (TimelineExtractionWork.status == "pending")
                        & (TimelineExtractionWork.next_attempt_at <= now)
                    )
                    | (
                        (TimelineExtractionWork.status == "running")
                        & (TimelineExtractionWork.lease_expires_at <= now)
                    ),
                )
                .order_by(TimelineExtractionWork.next_attempt_at, TimelineExtractionWork.id)
                .limit(limit)
            )
        ).all()
    )


async def list_blocked_extraction_work(
    session: AsyncSession,
    limit: int = 25,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[tuple[UUID, UUID, int, str, str]]:
    """List a bounded page of retryable policy-blocked work and its dependency fence."""
    from modules.timeline.models import TimelineExtractionWork

    if not 1 <= limit <= 100:
        raise ValueError("Blocked timeline recovery page must be between 1 and 100")
    rows = (
        await session.execute(
            select(
                TimelineExtractionWork.id,
                TimelineExtractionWork.document_version_id,
                TimelineExtractionWork.source_generation,
                TimelineExtractionWork.error_code,
                TimelineExtractionWork.dependency_fingerprint,
            )
            .where(
                TimelineExtractionWork.workspace_id == scope.workspace_id,
                TimelineExtractionWork.status == "blocked",
                TimelineExtractionWork.error_code.in_(
                    ("ai_policy_denied", "structured_unsupported")
                ),
                TimelineExtractionWork.dependency_fingerprint.is_not(None),
                TimelineExtractionWork.next_attempt_at <= datetime.now(UTC),
            )
            .order_by(TimelineExtractionWork.updated_at, TimelineExtractionWork.id)
            .limit(limit)
        )
    ).all()
    # error_code is an in_() match and dependency_fingerprint is filtered non-NULL above.
    return [(row[0], row[1], row[2], cast(str, row[3]), cast(str, row[4])) for row in rows]


async def requeue_blocked_extraction_work(
    session: AsyncSession,
    work_id: UUID,
    previous_fingerprint: str,
    current_fingerprint: str,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> bool:
    """Requeue policy-blocked work only if its stored dependency fence still matches."""
    from modules.timeline.models import TimelineExtractionWork

    if previous_fingerprint == current_fingerprint:
        return False
    work = await session.scalar(
        select(TimelineExtractionWork)
        .where(
            TimelineExtractionWork.workspace_id == scope.workspace_id,
            TimelineExtractionWork.id == work_id,
            TimelineExtractionWork.status == "blocked",
            TimelineExtractionWork.dependency_fingerprint == previous_fingerprint,
        )
        .with_for_update()
    )
    if work is None:
        return False
    work.status, work.attempt = "pending", 0
    work.next_attempt_at = datetime.now(UTC)
    work.error_code = work.dependency_fingerprint = None
    work.lease_owner = work.lease_expires_at = None
    return True


async def defer_blocked_extraction_recheck(
    session: AsyncSession,
    work_id: UUID,
    fingerprint: str,
    *,
    minutes: int = 15,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Delay the next recheck only while a policy block retains the observed dependency fence."""
    from modules.timeline.models import TimelineExtractionWork

    work = await session.scalar(
        select(TimelineExtractionWork)
        .where(
            TimelineExtractionWork.workspace_id == scope.workspace_id,
            TimelineExtractionWork.id == work_id,
            TimelineExtractionWork.status == "blocked",
            TimelineExtractionWork.dependency_fingerprint == fingerprint,
        )
        .with_for_update()
    )
    if work is not None:
        work.next_attempt_at = datetime.now(UTC) + timedelta(minutes=minutes)


async def set_extraction_work_error(
    session: AsyncSession,
    work_id: UUID,
    lease_owner: str,
    error_code: str,
    *,
    blocked: bool = False,
    dependency_fingerprint: str | None = None,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Persist lease-owned retry state; blocked retries fence against the attempted dependencies.

    The caller retains the request's original dependency fingerprint when it
    discards stale output so a newly permitted configuration can be retried.
    """
    from datetime import timedelta

    from modules.timeline.models import TimelineExtractionWork

    now = datetime.now(UTC)
    work = await session.scalar(
        select(TimelineExtractionWork)
        .where(
            TimelineExtractionWork.workspace_id == scope.workspace_id,
            TimelineExtractionWork.id == work_id,
            TimelineExtractionWork.status == "running",
            TimelineExtractionWork.lease_owner == lease_owner,
            TimelineExtractionWork.lease_expires_at > now,
        )
        .with_for_update()
    )
    if work is None:
        return
    work.status = "blocked" if blocked else ("failed" if work.attempt >= 5 else "pending")
    work.error_code = error_code[:64]
    work.dependency_fingerprint = dependency_fingerprint if blocked else None
    work.next_attempt_at = (
        now + timedelta(minutes=15)
        if blocked
        else now + timedelta(minutes=min(2**work.attempt, 60))
    )
    work.lease_owner = work.lease_expires_at = None


async def block_local_only_extraction_work(
    session: AsyncSession,
    work_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Mark extraction ineligible while the source remains local-only, without a recheck fence.

    Configuration changes cannot authorize provider egress from a local-only
    source, so this durable state has no scheduled policy recheck.
    """
    from modules.timeline.models import TimelineExtractionWork

    work = await session.scalar(
        select(TimelineExtractionWork)
        .where(
            TimelineExtractionWork.workspace_id == scope.workspace_id,
            TimelineExtractionWork.id == work_id,
        )
        .with_for_update()
    )
    if work is not None and work.status != "succeeded":
        work.status, work.error_code = "blocked", "local_only_source"
        work.next_attempt_at = datetime.max.replace(tzinfo=UTC)
        work.dependency_fingerprint = None
        work.lease_owner = work.lease_expires_at = None
        await session.flush()


async def finish_extraction_work(
    session: AsyncSession,
    work_id: UUID,
    lease_owner: str,
    proposals: list[dict[str, object]],
    model: str | None,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> bool:
    """Persist bounded structured proposals without raw source chunks under a live lease."""
    from modules.timeline.models import TimelineExtractionResult, TimelineExtractionWork

    now = datetime.now(UTC)
    work = await session.scalar(
        select(TimelineExtractionWork)
        .where(
            TimelineExtractionWork.workspace_id == scope.workspace_id,
            TimelineExtractionWork.id == work_id,
            TimelineExtractionWork.status == "running",
            TimelineExtractionWork.lease_owner == lease_owner,
            TimelineExtractionWork.lease_expires_at > now,
        )
        .with_for_update()
    )
    if work is None:
        return False
    result = await session.scalar(
        select(TimelineExtractionResult)
        .where(
            TimelineExtractionResult.work_id
            == work.id  # work was selected above under this workspace
        )
        .with_for_update()
    )
    if result is None:
        session.add(
            TimelineExtractionResult(work_id=work.id, proposals_json=proposals, model=model)
        )
    else:
        result.proposals_json, result.model = proposals, model
    work.status, work.lease_owner, work.lease_expires_at = "succeeded", None, None
    await session.flush()
    return True
