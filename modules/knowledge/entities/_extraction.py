"""Entity extraction work queue operations and extraction-driven entity/membership writes.

Private implementation of ``modules.knowledge.entities.public``; import from that facade.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import desc, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.schemas import (
    Scope,
)
from modules.knowledge.entities._common import _admit
from modules.knowledge.entities._reads import resolve_canonical_entity_id
from modules.knowledge.entities.models import (
    Entity,
    EntityAlias,
    EntityEvidenceMembership,
    EntityExtractionResult,
    EntityExtractionWork,
    EntityRedirect,
)
from modules.knowledge.entities.schemas import (
    canonicalize_name,
)

if TYPE_CHECKING:
    from modules.knowledge.documents.public import ExtractionEvidenceRef
    from modules.knowledge.entities.schemas import EntityExtractionStatus as _ExtractionStatus


async def schedule_extraction_work(
    session: AsyncSession, document_version_id: UUID, source_generation: int,
    extractor_version: str, prompt_version: str, *, scope: Scope, multi_workspace_enabled: bool,
) -> EntityExtractionWork:
    """Create or lock durable extraction work keyed by version and prompt identity."""
    if len(extractor_version) > 64 or len(prompt_version) > 64:
        raise ValueError("Extraction versions are too long")
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    await session.execute(pg_insert(EntityExtractionWork).values(
        workspace_id=scope.workspace_id,
        document_version_id=document_version_id,
        source_generation=source_generation,
        extractor_version=extractor_version,
        prompt_version=prompt_version,
    ).on_conflict_do_nothing(constraint="uq_entity_extraction_work_identity"))
    work = await session.scalar(select(EntityExtractionWork).where(
        EntityExtractionWork.workspace_id == scope.workspace_id,
        EntityExtractionWork.document_version_id == document_version_id,
        EntityExtractionWork.extractor_version == extractor_version,
        EntityExtractionWork.prompt_version == prompt_version,
    ).with_for_update())
    if work is None:
        raise RuntimeError("Extraction work could not be scheduled")
    # A durable work identity captures source generation once. Duplicate ready
    # events and recovery cannot rebind it to a later source generation.
    return work


async def claim_extraction_work(
    session: AsyncSession, work_id: UUID, lease_owner: str, now: datetime, *, scope: Scope,
    multi_workspace_enabled: bool, lease_seconds: int = 110,
) -> EntityExtractionWork | None:
    """Claim due work under a lease of ``lease_seconds`` (callers pass one that outlives the model call), enforcing expiry and the five-attempt ceiling."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    work = await session.scalar(select(EntityExtractionWork).where(
        EntityExtractionWork.id == work_id, EntityExtractionWork.workspace_id == scope.workspace_id,
    ).with_for_update())
    if work is None or work.status not in {"pending", "running"}:
        return None
    if work.status == "running" and work.lease_expires_at is not None and work.lease_expires_at > now:
        return None
    if work.attempt >= 5:
        work.status = "failed"
        work.error_code = "attempt_limit_exhausted"
        work.lease_owner = None
        work.lease_expires_at = None
        await session.flush()
        return None
    if work.next_attempt_at > now:
        return None
    work.attempt += 1
    work.status = "running"
    work.lease_owner = lease_owner
    work.lease_expires_at = now + timedelta(seconds=lease_seconds)
    work.error_code = None
    await session.flush()
    return work


async def list_recoverable_extraction_work(
    session: AsyncSession, limit: int = 25, *, scope: Scope, multi_workspace_enabled: bool,
) -> list[UUID]:
    """Lock and return bounded pending or expired extraction work IDs."""
    if not 1 <= limit <= 100:
        raise ValueError("Extraction recovery limit must be between 1 and 100")
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    now = datetime.now(UTC)
    return list((await session.scalars(
        select(EntityExtractionWork.id).where(
            EntityExtractionWork.workspace_id == scope.workspace_id,
            EntityExtractionWork.next_attempt_at <= now,
            or_(
                EntityExtractionWork.status == "pending",
                (EntityExtractionWork.status == "running") & (EntityExtractionWork.lease_expires_at <= now),
            ),
        ).order_by(EntityExtractionWork.next_attempt_at, EntityExtractionWork.created_at)
        .limit(limit).with_for_update(skip_locked=True)
    )).all())


async def terminalize_exhausted_extraction_work(
    session: AsyncSession, limit: int = 25, *, scope: Scope, multi_workspace_enabled: bool,
) -> int:
    """Fail expired running work at the attempt ceiling and clear its lease."""
    if not 1 <= limit <= 100:
        raise ValueError("Extraction terminalization limit must be between 1 and 100")
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    now = datetime.now(UTC)
    rows = list((await session.scalars(
        select(EntityExtractionWork).where(
            EntityExtractionWork.workspace_id == scope.workspace_id,
            EntityExtractionWork.status == "running",
            EntityExtractionWork.attempt >= 5,
            EntityExtractionWork.lease_expires_at <= now,
        ).order_by(EntityExtractionWork.lease_expires_at, EntityExtractionWork.id)
        .limit(limit).with_for_update(skip_locked=True)
    )).all())
    for work in rows:
        work.status = "failed"
        work.error_code = "attempt_limit_exhausted"
        work.lease_owner = None
        work.lease_expires_at = None
    return len(rows)


async def list_blocked_extraction_work(
    session: AsyncSession, limit: int = 25, *, scope: Scope, multi_workspace_enabled: bool,
) -> list[tuple[UUID, UUID, int, str | None, str | None]]:
    """List due policy/capability-blocked work with its dependency fingerprint."""
    if not 1 <= limit <= 100:
        raise ValueError("Blocked extraction page size must be between 1 and 100")
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    rows = (await session.execute(
        select(
            EntityExtractionWork.id, EntityExtractionWork.document_version_id,
            EntityExtractionWork.source_generation, EntityExtractionWork.error_code,
            EntityExtractionWork.dependency_fingerprint,
        ).where(
            EntityExtractionWork.workspace_id == scope.workspace_id,
            EntityExtractionWork.status == "blocked",
            EntityExtractionWork.error_code.in_(("ai_policy_denied", "structured_unsupported")),
            EntityExtractionWork.next_attempt_at <= datetime.now(UTC),
        ).order_by(EntityExtractionWork.updated_at, EntityExtractionWork.id).limit(limit)
    )).all()
    return [(row[0], row[1], row[2], row[3], row[4]) for row in rows]


async def requeue_blocked_extraction_work(
    session: AsyncSession, work_id: UUID, previous_fingerprint: str | None, current_fingerprint: str,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Requeue blocked work only after its dependency fingerprint has changed."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if previous_fingerprint is None or previous_fingerprint == current_fingerprint:
        return False
    work = await session.scalar(select(EntityExtractionWork).where(
        EntityExtractionWork.id == work_id,
        EntityExtractionWork.workspace_id == scope.workspace_id,
        EntityExtractionWork.status == "blocked",
        EntityExtractionWork.dependency_fingerprint == previous_fingerprint,
    ).with_for_update())
    if work is None:
        return False
    if work.lease_owner is not None and work.lease_expires_at is not None and work.lease_expires_at > datetime.now(UTC):
        return False
    work.status = "pending"
    work.attempt = 0
    work.next_attempt_at = datetime.now(UTC)
    work.error_code = None
    work.dependency_fingerprint = None
    work.lease_owner = None
    work.lease_expires_at = None
    return True


async def defer_blocked_extraction_recheck(
    session: AsyncSession, work_id: UUID, fingerprint: str, *, scope: Scope,
    multi_workspace_enabled: bool, minutes: int = 15,
) -> None:
    """Delay a matching blocked work item's next dependency recheck."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    work = await session.scalar(select(EntityExtractionWork).where(
        EntityExtractionWork.id == work_id,
        EntityExtractionWork.workspace_id == scope.workspace_id,
        EntityExtractionWork.status == "blocked",
        EntityExtractionWork.dependency_fingerprint == fingerprint,
    ).with_for_update())
    if work is not None:
        work.next_attempt_at = datetime.now(UTC) + timedelta(minutes=minutes)


async def get_extraction_status(
    session: AsyncSession, document_version_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> _ExtractionStatus | None:
    """Return the newest work status and stored facts for one document version."""
    from modules.knowledge.entities.schemas import EntityExtractionStatus

    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    row = (await session.execute(
        select(EntityExtractionWork, EntityExtractionResult)
        .outerjoin(EntityExtractionResult, EntityExtractionResult.work_id == EntityExtractionWork.id)
        .where(EntityExtractionWork.document_version_id == document_version_id,
               EntityExtractionWork.workspace_id == scope.workspace_id)
        .order_by(desc(EntityExtractionWork.created_at)).limit(1)
    )).one_or_none()
    if row is None:
        return None
    work, result = row
    return EntityExtractionStatus(
        document_version_id=work.document_version_id, status=work.status, attempt=work.attempt,
        error_code=work.error_code, model=result.model if result else None,
        facts=result.facts_json if result else [], review_candidates=result.review_json if result else [],
        completed_at=result.completed_at if result else None,
    )


async def finish_extraction_work(
    session: AsyncSession, work_id: UUID, lease_owner: str, *, facts: list[dict[str, object]],
    review: list[dict[str, object]], model: str | None, usage: dict[str, object] | None,
    scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Persist results and succeed only while the caller still owns a live lease."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    work = await session.scalar(select(EntityExtractionWork).where(
        EntityExtractionWork.id == work_id, EntityExtractionWork.status == "running",
        EntityExtractionWork.workspace_id == scope.workspace_id,
        EntityExtractionWork.lease_owner == lease_owner,
        EntityExtractionWork.lease_expires_at > datetime.now(UTC),
    ).with_for_update())
    if work is None:
        return False
    result = await session.scalar(select(EntityExtractionResult).where(EntityExtractionResult.work_id == work.id).with_for_update())
    if result is None:
        session.add(EntityExtractionResult(
            work_id=work.id, model=model, usage_json=usage, facts_json=facts, review_json=review,
        ))
    else:
        result.model, result.usage_json, result.facts_json, result.review_json = model, usage, facts, review
    work.status = "succeeded"
    work.lease_owner = None
    work.lease_expires_at = None
    await session.flush()
    return True


async def set_extraction_work_error(
    session: AsyncSession, work_id: UUID, lease_owner: str, error_code: str, *,
    scope: Scope, multi_workspace_enabled: bool,
    blocked: bool = False, dependency_fingerprint: str | None = None,
) -> None:
    """Mutate failure state only while the caller still owns a live lease.

    Changes the ORM row in the caller's transaction without explicitly flushing
    or committing; callers commit the session. Returns without mutation when
    the live lease is absent or owned by someone else. Retry delay is exponential
    in minutes up to 60; blocked work normally waits 15 minutes. Dependency
    recovery considers only ``ai_policy_denied`` and ``structured_unsupported``
    rows with a fingerprint. Local-only work has no fingerprint and is parked at
    ``datetime.max`` rather than requeued after a dependency change.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    work = await session.scalar(select(EntityExtractionWork).where(
        EntityExtractionWork.id == work_id, EntityExtractionWork.status == "running",
        EntityExtractionWork.workspace_id == scope.workspace_id,
        EntityExtractionWork.lease_owner == lease_owner,
        EntityExtractionWork.lease_expires_at > datetime.now(UTC),
    ).with_for_update())
    if work is None:
        return
    work.status = "blocked" if blocked else ("failed" if work.attempt >= 5 else "pending")
    work.error_code = error_code[:64]
    work.dependency_fingerprint = dependency_fingerprint if blocked else None
    work.next_attempt_at = (
        datetime.max.replace(tzinfo=UTC) if error_code == "local_only_source"
        else datetime.now(UTC) + timedelta(minutes=15 if blocked else min(2 ** work.attempt, 60))
    )
    work.lease_owner = None
    work.lease_expires_at = None


async def list_resolution_candidates(
    session: AsyncSession, entity_type: str, candidate_names: list[str], limit: int = 1000, *,
    scope: Scope, multi_workspace_enabled: bool,
) -> tuple[list[dict[str, object]], bool]:
    """Return bounded same-type entities and exact confirmed aliases for resolution."""
    if not 1 <= limit <= 1000 or not 1 <= len(candidate_names) <= 30:
        raise ValueError("Resolution context must be bounded")
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    normalized_names = sorted({canonicalize_name(name) for name in candidate_names})
    rows = list((await session.execute(
        select(Entity.id, Entity.type, Entity.name, Entity.revision)
        .where(
            Entity.workspace_id == scope.workspace_id,
            Entity.type == entity_type,
            ~Entity.id.in_(select(EntityRedirect.old_entity_id).where(
                EntityRedirect.workspace_id == scope.workspace_id,
            )),
        ).order_by(Entity.id).limit(limit + 1)
    )).all())
    overflow = len(rows) > limit
    rows = rows[:limit]
    aliases = (await session.execute(
        select(EntityAlias.entity_id, EntityAlias.normalized_alias).join(
            Entity, Entity.id == EntityAlias.entity_id,
        )
        .where(
            Entity.workspace_id == scope.workspace_id,
            EntityAlias.entity_id.in_([row.id for row in rows]),
            EntityAlias.confirmed.is_(True),
            EntityAlias.normalized_alias.in_(normalized_names),
        )
        .order_by(EntityAlias.entity_id, EntityAlias.normalized_alias).limit(limit + 1)
    )).all() if rows and not overflow else []
    overflow = overflow or len(aliases) > limit
    aliases = aliases[:limit]
    alias_map: dict[UUID, list[str]] = {}
    for entity_id, alias in aliases:
        alias_map.setdefault(entity_id, []).append(alias)
    return ([
        {"id": row.id, "type": row.type, "name": row.name,
         "revision": row.revision, "confirmed_aliases": alias_map.get(row.id, [])}
        for row in rows
    ], overflow)


async def create_extracted_entity(
    session: AsyncSession, entity_type: str, *, scope: Scope, multi_workspace_enabled: bool,
) -> UUID:
    """Create an unnamed derived entity inside an already admitted workspace."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    entity = Entity(
        workspace_id=scope.workspace_id, type=entity_type, name=None, canonical_name=None,
        name_origin=None, description_origin=None,
    )
    session.add(entity)
    await session.flush()
    return entity.id


async def find_extraction_entity(
    session: AsyncSession, *, extraction_identity: str, candidate_key: str,
    scope: Scope, multi_workspace_enabled: bool,
) -> UUID | None:
    """Return the canonical entity already bound to a deterministic extraction key, if any.

    Deterministic mappers (for example GitHub) use this with
    ``record_extraction_membership`` to stay idempotent: the first membership
    creates the entity, later calls find it. The earliest membership wins so
    the answer is stable; a merged entity is followed to its canonical ID. The
    caller holds the source lock that serializes find-or-create.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    entity_id = await session.scalar(
        select(EntityEvidenceMembership.entity_id).where(
            EntityEvidenceMembership.workspace_id == scope.workspace_id,
            EntityEvidenceMembership.extraction_identity == extraction_identity,
            EntityEvidenceMembership.candidate_key == candidate_key,
        ).order_by(EntityEvidenceMembership.extracted_at, EntityEvidenceMembership.id).limit(1)
    )
    if entity_id is None:
        return None
    try:
        return await resolve_canonical_entity_id(
            session, entity_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
    except LookupError:
        # The owner deleted the entity: report "absent" so the mapper recreates it
        # deterministically instead of failing every later record of the source.
        return None


async def record_extraction_membership(
    session: AsyncSession, *, entity_id: UUID, evidence_ref: ExtractionEvidenceRef,
    source_generation: int, extraction_identity: str, candidate_key: str,
    match_fingerprint: str, observed_at: datetime, confidence: float,
    scope: Scope, multi_workspace_enabled: bool,
) -> UUID:
    """Insert idempotent evidence membership after validating source generation and identity."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if (
        not extraction_identity or len(extraction_identity) > 256
        or not candidate_key or len(candidate_key) > 256
        or len(match_fingerprint) != 64
        or not math.isfinite(confidence) or not 0 <= confidence <= 1
        or evidence_ref.source_generation != source_generation
    ):
        raise ValueError("Extraction membership values are invalid")
    entity = await session.scalar(select(Entity).where(
        Entity.id == entity_id, Entity.workspace_id == scope.workspace_id,
    ).with_for_update())
    if entity is None:
        raise LookupError("Extraction entity is missing")
    inserted = (await session.execute(pg_insert(EntityEvidenceMembership).values(
        workspace_id=scope.workspace_id,
        entity_id=entity_id, document_id=evidence_ref.document_id, source_id=evidence_ref.source_id,
        document_version_id=evidence_ref.document_version_id, chunk_id=evidence_ref.chunk_id,
        extraction_identity=extraction_identity, candidate_key=candidate_key,
        match_fingerprint=match_fingerprint,
        observed_at=observed_at, confidence=confidence,
    ).on_conflict_do_nothing(constraint="uq_entity_evidence_retry").returning(
        EntityEvidenceMembership.id, EntityEvidenceMembership.entity_id,
    ))).first()
    if inserted is None:
        # Retry: the row already exists (nothing returned), so read back its id and owning entity.
        inserted = (await session.execute(select(
            EntityEvidenceMembership.id, EntityEvidenceMembership.entity_id,
        ).where(
            EntityEvidenceMembership.workspace_id == scope.workspace_id,
            EntityEvidenceMembership.extraction_identity == extraction_identity,
            EntityEvidenceMembership.candidate_key == candidate_key,
            EntityEvidenceMembership.chunk_id == evidence_ref.chunk_id,
        ))).first()
    if inserted is None:
        raise RuntimeError("Entity evidence membership could not be recorded")
    membership_id, member_entity_id = inserted
    if member_entity_id != entity_id:
        raise ValueError("Extraction retry identity resolved to a different entity")
    return membership_id
