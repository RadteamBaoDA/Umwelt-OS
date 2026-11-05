"""Owner entity create/update/alias writes and derived-field publication.

Private implementation of ``modules.knowledge.entities.public``; import from that facade.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Literal
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.realtime import commit_with_replay, make_graph_change
from core.workspaces.schemas import (
    Scope,
)
from modules.knowledge.entities._common import _admit, _schedule_entity_change
from modules.knowledge.entities._reads import (
    RedirectedEntityConflict,
    TerminalEntityConflict,
    _aliases,
    _entity_read,
    record_owner_action,
    resolve_canonical_entity_id,
)
from modules.knowledge.entities.models import (
    Entity,
    EntityAlias,
    EntityEvidenceMembership,
    EntityFieldEvidence,
    EntityRedirect,
)
from modules.knowledge.entities.schemas import (
    AliasCreate,
    EntityCreate,
    EntityPatch,
    EntityRead,
    canonicalize_name,
)


async def publish_derived_field(
    session: AsyncSession,
    *,
    entity_id: UUID,
    membership_id: UUID,
    field_name: Literal["name", "description"],
    value: str,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> bool:
    """Publish a derived field and bind its exact value to one valid membership.

    The caller owns the source/document locks and the outer transaction. This
    command takes entity then membership locks and queues temporal desired state
    with exact current support in the same transaction; it never commits.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if field_name not in {"name", "description"}:
        raise ValueError("Unsupported derived entity field")
    entity = await session.scalar(
        select(Entity).where(Entity.id == entity_id, Entity.workspace_id == scope.workspace_id).with_for_update()
    )
    if entity is None:
        raise LookupError("Entity is missing")
    membership = await session.scalar(
        select(EntityEvidenceMembership)
        .where(
            EntityEvidenceMembership.id == membership_id,
            EntityEvidenceMembership.entity_id == entity_id,
            EntityEvidenceMembership.workspace_id == scope.workspace_id,
        )
        .with_for_update()
    )
    if membership is None:
        raise LookupError("Entity evidence membership is missing or belongs to another entity")
    if (field_name == "name" and entity.name_origin == "owner") or (
        field_name == "description" and entity.description_origin == "owner"
    ):
        return False
    previous_value = entity.name if field_name == "name" else entity.description
    previous_origin = entity.name_origin if field_name == "name" else entity.description_origin
    if field_name == "name":
        value = " ".join(value.split())
        if not value or len(value) > 300:
            raise ValueError("Derived entity name must contain 1 to 300 characters")
        entity.name = value
        entity.canonical_name = canonicalize_name(value)
        entity.name_origin = "derived"
    else:
        if not value or len(value) > 20_000:
            raise ValueError("Derived entity description must contain 1 to 20000 characters")
        entity.description = value
        entity.description_origin = "derived"
    value_hash = sha256(value.encode("utf-8")).hexdigest()
    await session.execute(delete(EntityFieldEvidence).where(
        EntityFieldEvidence.entity_id == entity_id,
        EntityFieldEvidence.field_name == field_name,
        EntityFieldEvidence.value_hash != value_hash,
    ))
    support_exists = await session.scalar(select(EntityFieldEvidence.id).where(
        EntityFieldEvidence.entity_id == entity_id,
        EntityFieldEvidence.field_name == field_name,
        EntityFieldEvidence.value_hash == value_hash,
        EntityFieldEvidence.membership_id == membership_id,
    ).limit(1))
    if support_exists is None:
        session.add(EntityFieldEvidence(
            entity_id=entity_id,
            field_name=field_name,
            value_hash=value_hash,
            membership_id=membership_id,
        ))
        entity.revision += 1
    elif previous_value != value or previous_origin != "derived":
        entity.revision += 1
    await session.flush()
    if support_exists is None or previous_value != value or previous_origin != "derived":
        await _schedule_entity_change(
            session, entity, [field_name, "support"], "derived", scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    return True


async def create_entity(
    session: AsyncSession, payload: EntityCreate, *, scope: Scope, multi_workspace_enabled: bool,
) -> EntityRead:
    """Create an owner-authored entity and aliases inside one admitted workspace transaction."""
    fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    entity = Entity(
        workspace_id=scope.workspace_id,
        type=payload.type,
        name=payload.name,
        canonical_name=canonicalize_name(payload.name),
        description=payload.description,
        name_origin="owner",
        description_origin="owner" if payload.description is not None else None,
        metadata_json=payload.metadata,
    )
    session.add(entity)
    try:
        await session.flush()
        aliases = [
            EntityAlias(
                entity_id=entity.id,
                alias=alias,
                normalized_alias=canonicalize_name(alias),
                confirmed=True,
                origin="owner",
            )
            for alias in payload.aliases
            if canonicalize_name(alias) != entity.canonical_name
        ]
        session.add_all(aliases)
        await session.flush()
        await session.refresh(entity)
        result = _entity_read(entity, aliases)
        await record_owner_action(
            session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            operation="entity_create", reason=payload.reason,
            affected_ids=[entity.id], revisions={str(entity.id): 1},
        )
        await _schedule_entity_change(
            session, entity, ["name", "description", "metadata", "aliases"], "owner",
            scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        await commit_with_replay(
            session, [make_graph_change(entity_id=entity.id, scope=scope)], scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=fence,
        )
    except IntegrityError:
        await session.rollback()
        raise
    return result


async def update_entity(
    session: AsyncSession, entity_id: UUID, payload: EntityPatch, *,
    scope: Scope, multi_workspace_enabled: bool,
) -> EntityRead | None:
    """Apply a canonical owner-field update with revision and redirect fences.

    Admission is captured before canonical/entity locks. Returns None if the row
    disappears, rejects merged/deleted IDs with typed conflicts and stale revisions,
    marks edited fields
    owner-authored, removes their derived field support, then commits audit and
    graph changes plus exact-support temporal desired state in one transaction.
    """
    fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    try:
        canonical_id = await resolve_canonical_entity_id(
            session, entity_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
    except LookupError as exc:
        if await session.scalar(select(EntityRedirect.old_entity_id).where(
            EntityRedirect.workspace_id == scope.workspace_id,
            EntityRedirect.old_entity_id == entity_id,
        )) is not None:
            raise TerminalEntityConflict("Entity identity was deleted") from exc
        raise
    if canonical_id != entity_id:
        raise RedirectedEntityConflict("Entity ID was merged; use its canonical ID")
    entity = await session.scalar(select(Entity).where(
        Entity.id == entity_id, Entity.workspace_id == scope.workspace_id,
    ).with_for_update())
    if entity is None:
        return None
    previous_revision = entity.revision
    if entity.revision != payload.expected_revision:
        raise ValueError("Entity revision is stale")
    if not payload.model_fields_set - {"expected_revision", "reason"}:
        raise ValueError("At least one entity field is required")
    if "name" in payload.model_fields_set and payload.name is not None:
        entity.name = payload.name
        entity.canonical_name = canonicalize_name(payload.name)
        entity.name_origin = "owner"
        await session.execute(delete(EntityFieldEvidence).where(
            EntityFieldEvidence.entity_id == entity_id,
            EntityFieldEvidence.field_name == "name",
            EntityFieldEvidence.membership_id.in_(select(EntityEvidenceMembership.id).where(
                EntityEvidenceMembership.workspace_id == scope.workspace_id,
            )),
        ))
    if "description" in payload.model_fields_set:
        entity.description = payload.description
        entity.description_origin = "owner"
        await session.execute(delete(EntityFieldEvidence).where(
            EntityFieldEvidence.entity_id == entity_id,
            EntityFieldEvidence.field_name == "description",
            EntityFieldEvidence.membership_id.in_(select(EntityEvidenceMembership.id).where(
                EntityEvidenceMembership.workspace_id == scope.workspace_id,
            )),
        ))
    if "metadata" in payload.model_fields_set and payload.metadata is not None:
        entity.metadata_json = payload.metadata
    entity.revision += 1
    await session.flush()
    await session.refresh(entity)
    aliases = await _aliases(session, [entity.id], scope=scope)
    result = _entity_read(entity, aliases.get(entity.id))
    await record_owner_action(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        operation="entity_update", reason=payload.reason,
        affected_ids=[entity.id], revisions={str(entity.id): previous_revision},
    )
    await _schedule_entity_change(
        session, entity, sorted(payload.model_fields_set - {"expected_revision", "reason"}), "owner",
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    await commit_with_replay(
        session, [make_graph_change(entity_id=entity.id, scope=scope)], scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=fence,
    )
    return result


async def add_alias(
    session: AsyncSession, entity_id: UUID, payload: AliasCreate, *,
    scope: Scope, multi_workspace_enabled: bool,
) -> EntityRead | None:
    """Add an owner-authored alias to a canonical entity and commit its audit.

    Admission and its access fence precede row locks. Redirected or terminal IDs
    raise typed conflicts; a missing canonical row returns None. A successful
    insert records the actor/reason and atomically schedules graph desired state.
    """
    fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    try:
        canonical_id = await resolve_canonical_entity_id(
            session, entity_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
    except LookupError as exc:
        if await session.scalar(select(EntityRedirect.old_entity_id).where(
            EntityRedirect.workspace_id == scope.workspace_id,
            EntityRedirect.old_entity_id == entity_id,
        )) is not None:
            raise TerminalEntityConflict("Entity identity was deleted") from exc
        raise
    if canonical_id != entity_id:
        raise RedirectedEntityConflict("Entity ID was merged; use its canonical ID")
    entity = await session.scalar(select(Entity).where(
        Entity.id == entity_id, Entity.workspace_id == scope.workspace_id,
    ).with_for_update())
    if entity is None:
        return None
    normalized = canonicalize_name(payload.alias)
    if normalized == entity.canonical_name:
        raise ValueError("Alias duplicates the canonical entity name")
    alias = EntityAlias(
        entity_id=entity.id,
        alias=payload.alias,
        normalized_alias=normalized,
        confirmed=payload.confirmed,
        origin="owner",
    )
    session.add(alias)
    await session.flush()
    aliases = await _aliases(session, [entity.id], scope=scope)
    result = _entity_read(entity, aliases.get(entity.id))
    await record_owner_action(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        operation="alias_create", reason=payload.reason,
        affected_ids=[entity.id, alias.id], revisions={str(entity.id): entity.revision},
    )
    await _schedule_entity_change(
        session, entity, ["aliases"], "owner", scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    await commit_with_replay(
        session, [make_graph_change(entity_id=entity.id, scope=scope)], scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=fence,
    )
    return result


async def delete_alias(
    session: AsyncSession, entity_id: UUID, alias_id: UUID, *, reason: str = "owner_alias_delete",
    scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Delete one alias from a canonical entity and commit its owner audit.

    The admission fence precedes canonical/entity/alias locks. Redirected or
    terminal IDs raise typed conflicts; a missing entity or alias returns False. Success
    records the actor/reason and atomically schedules graph desired state.
    """
    fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    try:
        canonical_id = await resolve_canonical_entity_id(
            session, entity_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
    except LookupError as exc:
        if await session.scalar(select(EntityRedirect.old_entity_id).where(
            EntityRedirect.workspace_id == scope.workspace_id,
            EntityRedirect.old_entity_id == entity_id,
        )) is not None:
            raise TerminalEntityConflict("Entity identity was deleted") from exc
        raise
    if canonical_id != entity_id:
        raise RedirectedEntityConflict("Entity ID was merged; use its canonical ID")
    entity = await session.scalar(select(Entity).where(
        Entity.id == entity_id, Entity.workspace_id == scope.workspace_id,
    ).with_for_update())
    if entity is None:
        return False
    alias = await session.scalar(
        select(EntityAlias).where(
            EntityAlias.id == alias_id, EntityAlias.entity_id == entity_id,
        ).with_for_update()
    )
    if alias is None:
        return False
    await session.delete(alias)
    await record_owner_action(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        operation="alias_delete", reason=reason,
        affected_ids=[entity.id, alias_id], revisions={str(entity.id): entity.revision},
    )
    await _schedule_entity_change(
        session, entity, ["aliases"], "owner", scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    await commit_with_replay(
        session, [make_graph_change(entity_id=entity.id, scope=scope)], scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=fence,
    )
    return True


async def delete_entity(
    session: AsyncSession, entity_id: UUID, *, reason: str = "owner_entity_delete",
    scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Delegate canonical deletion and its support cleanup to the correction owner."""
    from modules.knowledge.entities.corrections import delete_canonical_entity

    return await delete_canonical_entity(
        session, entity_id, reason=reason, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
