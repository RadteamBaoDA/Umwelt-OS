"""Entity identity reads: list/get, canonical-redirect resolution, references, membership and evidence reads.

Private implementation of ``modules.knowledge.entities.public``; import from that facade.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, desc, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ForbiddenError
from core.pagination import decode_cursor, encode_cursor
from core.workspaces.admission import actor_id as _actor
from core.workspaces.schemas import (
    Scope,
)
from modules.knowledge.entities._common import _admit
from modules.knowledge.entities.models import (
    Entity,
    EntityAlias,
    EntityEvidenceMembership,
    EntityExtractionWork,
    EntityOwnerAction,
    EntityRedirect,
)
from modules.knowledge.entities.schemas import (
    EntityAliasRead,
    EntityEvidencePage,
    EntityEvidenceRead,
    EntityMembershipReferenceRead,
    EntityPage,
    EntityRead,
    EntityReferenceRead,
    VersionMembershipReference,
    canonicalize_name,
)
from modules.sources import public as sources


async def observability_quality_summary(
    session: AsyncSession,
    *,
    instance_operator: bool,
) -> dict[str, int]:
    """Return global entity quality counts only for the explicit instance operator route."""
    if instance_operator is not True:
        raise ForbiddenError("Instance operator access required")
    unresolved = int(
        await session.scalar(
            select(func.count())
            .select_from(Entity)
            .where(and_(Entity.canonical_name.is_(None), Entity.name.is_(None)))
        )
        or 0
    )
    failed_extraction = int(
        await session.scalar(
            select(func.count())
            .select_from(EntityExtractionWork)
            .where(EntityExtractionWork.status == "failed")
        )
        or 0
    )
    return {"unresolved_entities": unresolved, "failed_extraction": failed_extraction}


class RedirectedEntityConflict(ValueError):
    """Signal that a write used an entity ID redirected by a merge."""


class TerminalEntityConflict(LookupError):
    """Signal that an entity identity was deleted and cannot be followed."""


def _entity_read(entity: Entity, aliases: list[EntityAlias] | None = None) -> EntityRead:
    """Project an entity and aliases while hiding fields without known provenance."""
    return EntityRead(
        id=entity.id,
        type=entity.type,
        name=entity.name if entity.name_origin is not None else None,
        canonical_name=entity.canonical_name if entity.name_origin is not None else None,
        description=entity.description if entity.description_origin is not None else None,
        metadata=entity.metadata_json,
        revision=entity.revision,
        name_origin=entity.name_origin,
        description_origin=entity.description_origin,
        first_seen_at=entity.first_seen_at,
        last_seen_at=entity.last_seen_at,
        created_at=entity.created_at,
        updated_at=entity.updated_at,
        aliases=[
            EntityAliasRead(
                id=item.id,
                entity_id=item.entity_id,
                alias=item.alias,
                source_id=item.source_id,
                confirmed=item.confirmed,
                origin=item.origin,
                confidence=item.confidence,
                created_at=item.created_at,
            )
            for item in aliases or []
        ],
    )


async def _aliases(
    session: AsyncSession,
    entity_ids: list[UUID],
    *,
    scope: Scope,
) -> dict[UUID, list[EntityAlias]]:
    """Load ordered display aliases only from the admitted workspace."""
    if not entity_ids:
        return {}
    result: dict[UUID, list[EntityAlias]] = {}
    aliases = (
        await session.scalars(
            select(EntityAlias)
            .join(Entity, Entity.id == EntityAlias.entity_id)
            .where(
                EntityAlias.entity_id.in_(entity_ids),
                Entity.workspace_id == scope.workspace_id,
                or_(EntityAlias.origin.is_not(None), EntityAlias.source_id.is_(None)),
            )
            .order_by(EntityAlias.alias)
        )
    ).all()
    for alias in aliases:
        result.setdefault(alias.entity_id, []).append(alias)
    return result


async def record_owner_action(
    session: AsyncSession,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    operation: str,
    reason: str,
    affected_ids: list[UUID],
    revisions: dict[str, int | None] | None = None,
) -> None:
    """Queue an actor-attributed workspace correction record without committing."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    clean_reason = " ".join(reason.split())
    if not clean_reason or len(clean_reason) > 300:
        raise ValueError("Owner action reason must contain 1 to 300 characters")
    session.add(
        EntityOwnerAction(
            workspace_id=scope.workspace_id,
            actor_id=_actor(scope),
            operation=operation,
            reason=clean_reason,
            affected_ids=[str(identifier) for identifier in affected_ids],
            revisions=revisions or {},
            created_at=datetime.now(UTC),
        )
    )


async def list_entities(
    session: AsyncSession,
    limit: int,
    cursor: str | None,
    entity_type: str | None,
    query: str | None,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> EntityPage:
    """Return a bounded cursor page of canonical entities in one admitted workspace."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not 1 <= limit <= 100:
        raise ValueError("Entity page limit must be between 1 and 100")
    statement = select(Entity).where(
        Entity.workspace_id == scope.workspace_id,
        ~Entity.id.in_(
            select(EntityRedirect.old_entity_id).where(
                EntityRedirect.workspace_id == scope.workspace_id,
            )
        ),
    )
    if entity_type:
        statement = statement.where(Entity.type == entity_type)
    if query:
        # Escape LIKE metacharacters so a literal % or _ in the query is not a wildcard.
        def _like_escape(value: str) -> str:
            """Escape backslash, ``%`` and ``_`` so user text matches literally under ``ILIKE ... ESCAPE '\\'``."""
            return value.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")

        statement = statement.where(
            or_(
                Entity.name.ilike(f"%{_like_escape(query)}%", escape="\\"),
                Entity.canonical_name.ilike(
                    f"%{_like_escape(canonicalize_name(query))}%", escape="\\"
                ),
            )
        )
    if cursor:
        created_at, identifier = decode_cursor(cursor)
        statement = statement.where(tuple_(Entity.created_at, Entity.id) < (created_at, identifier))
    rows = list(
        (
            await session.scalars(
                statement.order_by(desc(Entity.created_at), desc(Entity.id)).limit(limit + 1)
            )
        ).all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    aliases = await _aliases(session, [row.id for row in rows], scope=scope)
    next_cursor = encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None
    return EntityPage(
        items=[_entity_read(row, aliases.get(row.id)) for row in rows], next_cursor=next_cursor
    )


async def get_entity(
    session: AsyncSession,
    entity_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> EntityRead | None:
    """Read a canonical entity through redirects, returning None when unavailable."""
    try:
        canonical_id = await resolve_canonical_entity_id(
            session,
            entity_id,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    except LookupError:
        return None
    entity = await session.scalar(
        select(Entity).where(
            Entity.id == canonical_id,
            Entity.workspace_id == scope.workspace_id,
        )
    )
    if entity is None:
        return None
    aliases = await _aliases(session, [entity.id], scope=scope)
    return _entity_read(entity, aliases.get(entity.id))


_MAX_CANONICAL_HOPS = 32


async def _resolve_canonical_bulk(
    session: AsyncSession,
    ids: list[UUID],
    *,
    workspace_id: UUID,
) -> dict[UUID, UUID | Exception]:
    """Resolve many entity IDs through the owner redirect chain with one query round per hop (no admission).

    Mirrors ``resolve_canonical_entity_id`` exactly, per ID: a redirect-free ID must exist in the workspace
    (else ``LookupError("Entity reference is missing")``), a tombstoned redirect (NULL target) yields
    ``LookupError("Entity identity was deleted")``, a revisited target ``ValueError`` (cycle) and a chain
    longer than 32 hops ``ValueError`` (limit). Failures are returned as exception *values* so callers keep
    the first-failure-in-input-order semantics of the sequential loop. At most 33 rounds x 2 statements.
    """
    unique = list(dict.fromkeys(ids))
    state = {identifier: identifier for identifier in unique}
    seen = {identifier: {identifier} for identifier in unique}
    result: dict[UUID, UUID | Exception] = {}
    pending = set(unique)
    for _ in range(_MAX_CANONICAL_HOPS):
        if not pending:
            break
        currents = {state[identifier] for identifier in pending}
        redirects = {
            old: target
            for old, target in (
                await session.execute(
                    select(EntityRedirect.old_entity_id, EntityRedirect.target_entity_id).where(
                        EntityRedirect.workspace_id == workspace_id,
                        EntityRedirect.old_entity_id.in_(currents),
                    )
                )
            ).all()
        }
        unredirected = currents - redirects.keys()
        existing = (
            set(
                (
                    await session.scalars(
                        select(Entity.id).where(
                            Entity.id.in_(unredirected),
                            Entity.workspace_id == workspace_id,
                        )
                    )
                ).all()
            )
            if unredirected
            else set()
        )
        for identifier in list(pending):
            current = state[identifier]
            if current not in redirects:
                result[identifier] = (
                    current if current in existing else LookupError("Entity reference is missing")
                )
                pending.discard(identifier)
                continue
            target = redirects[current]
            if target is None:
                result[identifier] = LookupError("Entity identity was deleted")
                pending.discard(identifier)
            elif target in seen[identifier]:
                result[identifier] = ValueError("Entity redirect cycle detected")
                pending.discard(identifier)
            else:
                seen[identifier].add(target)
                state[identifier] = target
    for identifier in pending:
        result[identifier] = ValueError("Entity redirect chain exceeds its limit")
    return result


async def resolve_canonical_entity_ids(
    session: AsyncSession,
    ids: list[UUID],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[UUID | None]:
    """Bulk ``resolve_canonical_entity_id``: canonical IDs aligned with ``ids`` (duplicates allowed, <=100).

    Read-only, one admission, a constant number of queries per redirect hop. IDs that are missing or
    deleted map to ``None`` (the singular raises ``LookupError``); a redirect cycle or over-long chain
    raises ``ValueError`` exactly like the singular, for the first such ID in input order.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if len(ids) > 100:
        raise ValueError("Entity bulk query must contain at most 100 IDs")
    resolved = await _resolve_canonical_bulk(session, ids, workspace_id=scope.workspace_id)
    canonical: list[UUID | None] = []
    for identifier in ids:
        outcome = resolved[identifier]
        if isinstance(outcome, ValueError):
            raise outcome
        canonical.append(
            None if isinstance(outcome, Exception) else outcome
        )  # only LookupError remains here
    return canonical


async def get_entities(
    session: AsyncSession,
    ids: list[UUID],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[EntityRead | None]:
    """Bulk ``get_entity``: reads aligned with ``ids`` (duplicates allowed, at most 100), ``None`` if unavailable.

    Follows redirects, applies the same workspace scoping and builds each read with the same alias
    projection as ``get_entity``; costs a constant number of queries instead of ~4 per ID. Read-only.
    """
    canonical_ids = await resolve_canonical_entity_ids(
        session,
        ids,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    wanted = list(
        dict.fromkeys(identifier for identifier in canonical_ids if identifier is not None)
    )
    if not wanted:
        return [None] * len(ids)
    rows = {
        row.id: row
        for row in (
            await session.scalars(
                select(Entity).where(
                    Entity.id.in_(wanted),
                    Entity.workspace_id == scope.workspace_id,
                )
            )
        ).all()
    }
    aliases = await _aliases(session, list(rows), scope=scope)
    return [
        _entity_read(rows[identifier], aliases.get(identifier))
        if identifier is not None and identifier in rows
        else None
        for identifier in canonical_ids
    ]


async def get_entity_refs(
    session: AsyncSession,
    ids: list[UUID],
    *,
    for_write: bool = False,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[EntityReferenceRead]:
    """Resolve up to 100 unique entity IDs while preserving requested order.

    Read mode follows redirects. Write mode rejects redirected and terminally
    deleted IDs, then locks canonical rows in sorted order. Missing references
    raise LookupError; duplicate or oversized input raises ValueError.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if len(ids) > 100 or len(set(ids)) != len(ids):
        raise ValueError("Entity reference query must contain up to 100 unique IDs")
    if not ids:
        return []
    canonical_ids: dict[UUID, UUID] = {}
    if not for_write:
        # Read mode: one bulk redirect resolution (constant queries) instead of two queries per ID. The first
        # failing ID in request order raises the same exception the sequential loop below would raise.
        resolved = await _resolve_canonical_bulk(session, ids, workspace_id=scope.workspace_id)
        for identifier in ids:
            outcome = resolved[identifier]
            if isinstance(outcome, Exception):
                raise outcome
            canonical_ids[identifier] = outcome
    for identifier in ids if for_write else ():
        try:
            canonical_ids[identifier] = await resolve_canonical_entity_id(
                session,
                identifier,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        except LookupError as exc:
            if (
                for_write
                and await session.scalar(
                    select(EntityRedirect.old_entity_id).where(
                        EntityRedirect.workspace_id == scope.workspace_id,
                        EntityRedirect.old_entity_id == identifier,
                    )
                )
                is not None
            ):
                raise TerminalEntityConflict("Entity identity was deleted") from exc
            raise
    if for_write and any(canonical_ids[identifier] != identifier for identifier in ids):
        raise RedirectedEntityConflict("Entity ID was merged; use its canonical ID")
    query = select(Entity).where(
        Entity.workspace_id == scope.workspace_id,
        Entity.id.in_(set(canonical_ids.values())),
    )
    if for_write:
        query = query.order_by(Entity.id).with_for_update()
    rows = (await session.scalars(query)).all()
    rows_by_id = {row.id: row for row in rows}
    by_id = {
        requested_id: EntityReferenceRead(
            requested_id=requested_id,
            canonical_id=canonical_ids[requested_id],
            revision=rows_by_id[canonical_ids[requested_id]].revision,
            type=rows_by_id[canonical_ids[requested_id]].type,
            name=rows_by_id[canonical_ids[requested_id]].name
            if rows_by_id[canonical_ids[requested_id]].name_origin is not None
            else None,
        )
        for requested_id in ids
        if canonical_ids[requested_id] in rows_by_id
    }
    if set(by_id) != set(ids):
        raise LookupError("Entity reference is missing")
    return [by_id[identifier] for identifier in ids]


async def resolve_canonical_entity_id(
    session: AsyncSession,
    entity_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> UUID:
    """Follow the bounded owner redirect chain; malformed cycles fail closed."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    current = entity_id
    seen = {current}
    for _ in range(32):
        redirect = await session.scalar(
            select(EntityRedirect).where(
                EntityRedirect.old_entity_id == current,
                EntityRedirect.workspace_id == scope.workspace_id,
            )
        )
        if redirect is None:
            if (
                await session.scalar(
                    select(Entity.id).where(
                        Entity.id == current,
                        Entity.workspace_id == scope.workspace_id,
                    )
                )
                is None
            ):
                raise LookupError("Entity reference is missing")
            return current
        target = redirect.target_entity_id
        if target is None:
            raise LookupError("Entity identity was deleted")
        if target in seen:
            raise ValueError("Entity redirect cycle detected")
        seen.add(target)
        current = target
    raise ValueError("Entity redirect chain exceeds its limit")


async def get_membership_refs(
    session: AsyncSession,
    ids: list[UUID],
    *,
    for_write: bool = False,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[EntityMembershipReferenceRead]:
    """Resolve unique memberships, acquiring stable entity/ID locks for writes."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if len(ids) > 200 or len(set(ids)) != len(ids):
        raise ValueError("Entity membership query must contain up to 200 unique IDs")
    if not ids:
        return []
    query = select(EntityEvidenceMembership).where(
        EntityEvidenceMembership.id.in_(ids),
        EntityEvidenceMembership.workspace_id == scope.workspace_id,
    )
    if for_write:
        query = (
            query.order_by(EntityEvidenceMembership.entity_id, EntityEvidenceMembership.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    rows = (await session.scalars(query)).all()
    by_id = {
        row.id: EntityMembershipReferenceRead(
            id=row.id,
            entity_id=row.entity_id,
            document_version_id=row.document_version_id,
            chunk_id=row.chunk_id,
            observed_at=row.observed_at,
            extracted_at=row.extracted_at,
            confidence=row.confidence,
        )
        for row in rows
    }
    if set(by_id) != set(ids):
        raise LookupError("Entity evidence membership is missing")
    return [by_id[identifier] for identifier in ids]


async def list_version_membership_refs(
    session: AsyncSession,
    document_version_id: UUID,
    chunk_ids: list[UUID],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[VersionMembershipReference]:
    """Return bounded canonical memberships for chunks already authorized by documents extraction input.

    The caller must keep the documents source/document egress fence and must
    validate each returned chunk against that extraction input. The detached
    result exposes membership keys for model selection; the model never chooses
    a global entity ID.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if len(chunk_ids) > 100 or len(set(chunk_ids)) != len(chunk_ids):
        raise ValueError("Version membership chunks must be unique and bounded")
    if not chunk_ids:
        return []
    rows = (
        await session.execute(
            select(EntityEvidenceMembership, Entity)
            .join(Entity, Entity.id == EntityEvidenceMembership.entity_id)
            .where(
                EntityEvidenceMembership.document_version_id == document_version_id,
                EntityEvidenceMembership.workspace_id == scope.workspace_id,
                Entity.workspace_id == scope.workspace_id,
                EntityEvidenceMembership.chunk_id.in_(chunk_ids),
                ~EntityEvidenceMembership.entity_id.in_(
                    select(EntityRedirect.old_entity_id).where(
                        EntityRedirect.workspace_id == scope.workspace_id,
                    )
                ),
            )
            .order_by(EntityEvidenceMembership.id)
        )
    ).all()
    if len(rows) > 150:
        raise ValueError("Version membership context exceeds 150 exact supports")
    return [
        VersionMembershipReference(
            membership_id=membership.id,
            entity_id=entity.id,
            entity_type=entity.type,
            entity_revision=entity.revision,
            chunk_id=membership.chunk_id,
            observed_at=membership.observed_at,
            name=entity.name if entity.name_origin is not None else None,
        )
        for membership, entity in rows
    ]


async def list_retained_version_membership_refs(
    session: AsyncSession,
    document_version_id: UUID,
    chunk_ids: list[UUID],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[VersionMembershipReference]:
    """Return canonical membership keys for exact currently permitted retained chunks.

    Accepts an older retained version without making it current extraction input.
    The caller holds its source/document egress fences and checks destination
    policy; this read checks active source and current generation using document
    public provenance contracts. Up to100 unique chunks and100 memberships are
    authorized completely, never truncated. Missing/deleted/mismatched evidence
    raises LookupError; malformed or over-bound input raises ValueError. Names
    are identity hints, not model seed proof: get_temporal_node_seeds owns that
    source-local field-value proof. No locks, writes or commits occur here.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if len(chunk_ids) > 100 or len(set(chunk_ids)) != len(chunk_ids):
        raise ValueError("Retained membership chunks must be unique and bounded to100")
    if not chunk_ids:
        return []
    from modules.knowledge.documents import public as documents

    fences = await documents.review_version_fences(
        session,
        [document_version_id],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    fence = fences.get(document_version_id)
    if fence is None:
        raise LookupError("Retained membership version is unavailable")
    source = await sources.get_connector_source(
        session,
        fence.source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if (
        source is None
        or source.status != "active"
        or source.generation != fence.current_source_generation
    ):
        raise LookupError("Retained membership source policy or generation changed")
    try:
        evidence = await documents.read_evidence_refs(
            session,
            [(document_version_id, chunk_id) for chunk_id in chunk_ids],
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    except ValueError as exc:
        raise LookupError("Retained membership evidence is unavailable") from exc
    if len(evidence) != len(chunk_ids) or any(
        ref.document_id != fence.document_id or ref.source_id != fence.source_id for ref in evidence
    ):
        raise LookupError("Retained membership evidence identity changed")
    rows = (
        await session.execute(
            select(EntityEvidenceMembership, Entity)
            .join(Entity, Entity.id == EntityEvidenceMembership.entity_id)
            .where(
                EntityEvidenceMembership.document_version_id == document_version_id,
                EntityEvidenceMembership.workspace_id == scope.workspace_id,
                Entity.workspace_id == scope.workspace_id,
                EntityEvidenceMembership.chunk_id.in_(chunk_ids),
                ~EntityEvidenceMembership.entity_id.in_(
                    select(EntityRedirect.old_entity_id).where(
                        EntityRedirect.workspace_id == scope.workspace_id,
                    )
                ),
            )
            .order_by(EntityEvidenceMembership.id)
            .limit(101)
        )
    ).all()
    if len(rows) > 100:
        raise ValueError("Retained membership context exceeds100 exact supports")
    if any(
        membership.document_id != fence.document_id or membership.source_id != fence.source_id
        for membership, _ in rows
    ):
        raise LookupError("Retained membership owner provenance changed")
    return [
        VersionMembershipReference(
            membership_id=membership.id,
            entity_id=entity.id,
            entity_type=entity.type,
            entity_revision=entity.revision,
            chunk_id=membership.chunk_id,
            observed_at=membership.observed_at,
            name=entity.name if entity.name_origin is not None else None,
        )
        for membership, entity in rows
    ]


async def list_entity_evidence(
    session: AsyncSession,
    entity_id: UUID,
    limit: int = 50,
    cursor: str | None = None,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> EntityEvidencePage | None:
    """Return evidence for a canonical entity with document-version provenance."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not 1 <= limit <= 100:
        raise ValueError("Entity evidence page limit must be between 1 and 100")
    try:
        canonical_id = await resolve_canonical_entity_id(
            session,
            entity_id,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    except LookupError:
        return None
    statement = select(EntityEvidenceMembership).where(
        EntityEvidenceMembership.entity_id == canonical_id,
        EntityEvidenceMembership.workspace_id == scope.workspace_id,
    )
    if cursor:
        extracted_at, identifier = decode_cursor(cursor)
        statement = statement.where(
            tuple_(EntityEvidenceMembership.extracted_at, EntityEvidenceMembership.id)
            > (extracted_at, identifier)
        )
    rows = list(
        (
            await session.scalars(
                statement.order_by(
                    EntityEvidenceMembership.extracted_at, EntityEvidenceMembership.id
                ).limit(limit + 1)
            )
        ).all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    if not rows:
        return EntityEvidencePage(items=[], next_cursor=None)
    from modules.knowledge.documents import public as documents

    refs = await documents.read_evidence_refs(
        session,
        list(dict.fromkeys((row.document_version_id, row.chunk_id) for row in rows)),
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    by_pair = {(ref.document_version_id, ref.chunk_id): ref for ref in refs}
    items = [
        EntityEvidenceRead(
            id=row.id,
            entity_id=row.entity_id,
            document_id=ref.document_id,
            document_version_id=row.document_version_id,
            version_number=ref.version_number,
            chunk_id=row.chunk_id,
            observed_at=row.observed_at,
            extracted_at=row.extracted_at,
            confidence=row.confidence,
            source_id=ref.source_id,
            title=ref.title,
            canonical_url=ref.canonical_url,
            metadata_is_version_snapshot=ref.metadata_is_version_snapshot,
            excerpt=ref.excerpt,
        )
        for row in rows
        if (ref := by_pair.get((row.document_version_id, row.chunk_id))) is not None
    ]
    next_cursor = encode_cursor(rows[-1].extracted_at, rows[-1].id) if has_more and rows else None
    return EntityEvidencePage(items=items, next_cursor=next_cursor)


async def _first_evidence_memberships(
    session: AsyncSession, wanted: list[UUID], limit: int, *, scope: Scope
) -> dict[UUID, list[EntityEvidenceMembership]]:
    """Read the first ``limit + 1`` evidence memberships of every wanted entity in one query.

    Read-only window-function query ordered ``(extracted_at, id)`` per entity and scoped to the
    workspace; every wanted entity has an entry (possibly empty).
    """
    rows_by_entity: dict[UUID, list[EntityEvidenceMembership]] = {
        identifier: [] for identifier in wanted
    }
    if wanted:
        position = func.row_number().over(
            partition_by=EntityEvidenceMembership.entity_id,
            order_by=(EntityEvidenceMembership.extracted_at, EntityEvidenceMembership.id),
        )
        ranked = (
            select(EntityEvidenceMembership.id.label("membership_id"), position.label("position"))
            .where(
                EntityEvidenceMembership.entity_id.in_(wanted),
                EntityEvidenceMembership.workspace_id == scope.workspace_id,
            )
            .subquery()
        )
        memberships = (
            await session.scalars(
                select(EntityEvidenceMembership)
                .join(ranked, ranked.c.membership_id == EntityEvidenceMembership.id)
                .where(ranked.c.position <= limit + 1)
                .order_by(
                    EntityEvidenceMembership.entity_id,
                    EntityEvidenceMembership.extracted_at,
                    EntityEvidenceMembership.id,
                )
            )
        ).all()
        for membership in memberships:
            rows_by_entity[membership.entity_id].append(membership)
    return rows_by_entity


async def list_entity_evidence_bulk(
    session: AsyncSession,
    ids: list[UUID],
    limit: int = 50,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[EntityEvidencePage | None]:
    """Bulk first-page ``list_entity_evidence``: pages aligned with ``ids`` (duplicates allowed, at most 100).

    Per entity the result equals ``list_entity_evidence(id, limit)`` without a cursor: redirects are followed,
    rows are ordered ``(extracted_at, id)`` and cut at ``limit`` (``next_cursor`` set when more exist), items
    whose evidence ref is no longer readable are dropped, and a missing entity yields ``None``. One
    window-function query selects the first ``limit + 1`` memberships of every entity and evidence refs are
    read in batches of at most 100 unique pairs, instead of ~3 statements per entity. Read-only.
    """
    if not 1 <= limit <= 100:
        raise ValueError("Entity evidence page limit must be between 1 and 100")
    canonical_ids = await resolve_canonical_entity_ids(
        session,
        ids,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    wanted = list(
        dict.fromkeys(identifier for identifier in canonical_ids if identifier is not None)
    )
    rows_by_entity = await _first_evidence_memberships(session, wanted, limit, scope=scope)
    from modules.knowledge.documents import public as documents

    pairs = list(
        dict.fromkeys(
            (row.document_version_id, row.chunk_id)
            for identifier in wanted
            for row in rows_by_entity[identifier][:limit]
        )
    )
    by_pair: dict[tuple[UUID, UUID], Any] = {}
    for start in range(
        0, len(pairs), 100
    ):  # read_evidence_refs accepts at most 100 unique pairs per call
        for evidence_ref in await documents.read_evidence_refs(
            session,
            pairs[start : start + 100],
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        ):
            by_pair[(evidence_ref.document_version_id, evidence_ref.chunk_id)] = evidence_ref
    pages: dict[UUID, EntityEvidencePage] = {}
    for identifier in wanted:
        entity_rows = rows_by_entity[identifier]
        has_more = len(entity_rows) > limit
        entity_rows = entity_rows[:limit]
        items = [
            EntityEvidenceRead(
                id=row.id,
                entity_id=row.entity_id,
                document_id=ref.document_id,
                document_version_id=row.document_version_id,
                version_number=ref.version_number,
                chunk_id=row.chunk_id,
                observed_at=row.observed_at,
                extracted_at=row.extracted_at,
                confidence=row.confidence,
                source_id=ref.source_id,
                title=ref.title,
                canonical_url=ref.canonical_url,
                metadata_is_version_snapshot=ref.metadata_is_version_snapshot,
                excerpt=ref.excerpt,
            )
            for row in entity_rows
            if (ref := by_pair.get((row.document_version_id, row.chunk_id))) is not None
        ]
        next_cursor = (
            encode_cursor(entity_rows[-1].extracted_at, entity_rows[-1].id)
            if has_more and entity_rows
            else None
        )
        pages[identifier] = EntityEvidencePage(items=items, next_cursor=next_cursor)
    return [pages[identifier] if identifier is not None else None for identifier in canonical_ids]
