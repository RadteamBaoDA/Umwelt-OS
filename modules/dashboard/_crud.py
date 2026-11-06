"""Owner mutations for dashboards, groups, gadget definitions, instances and layouts (private to the dashboard package)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.realtime import commit_with_replay, make_dashboard_change
from core.workspaces.admission import actor_id as _actor
from core.workspaces.schemas import Scope
from modules.dashboard import gadgets, layouts
from modules.dashboard._common import (
    DashboardConflict,
    DashboardMissing,
    _admit,
    _bump,
    _check_revision,
    _lock_dashboard,
    _lock_definition,
    _lock_owner_creation_quota,
)
from modules.dashboard._highlights import validate_highlight_rules
from modules.dashboard._reads import (
    _dashboard_read,
    _definition_read,
    _definition_source_states,
    _instance_minima,
    _lock_selected_sources,
    _validate_selected_sources,
)
from modules.dashboard.models import (
    Dashboard,
    DashboardGroup,
    DashboardLayout,
    GadgetDefinition,
    GadgetInstance,
    GadgetPlacement,
)
from modules.dashboard.schemas import (
    MAX_DASHBOARDS_PER_OWNER,
    MAX_DEFINITIONS_PER_OWNER,
    MAX_GROUPS_PER_DASHBOARD,
    MAX_INSTANCES_PER_DASHBOARD,
    DashboardCreate,
    DashboardDetail,
    DashboardGroupRead,
    DashboardPatch,
    GadgetConfiguration,
    GadgetDefinitionCreate,
    GadgetDefinitionPatch,
    GadgetDefinitionRead,
    GadgetFilters,
    GadgetScope,
    GroupCreate,
    GroupPatch,
    HighlightRule,
    InstanceCreate,
    InstancePatch,
    LayoutReplace,
)


async def get_dashboard(
    session: AsyncSession, dashboard_id: UUID, *, scope: Scope, multi_workspace_enabled: bool
) -> DashboardDetail | None:
    """Return a detached owner-only dashboard configuration, without renderer payload data."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    dashboard = await session.scalar(
        select(Dashboard).where(
            Dashboard.id == dashboard_id,
            Dashboard.workspace_id == scope.workspace_id,
            Dashboard.owner_id == _actor(scope),
        )
    )
    return (
        await _dashboard_read(
            session, dashboard, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        if dashboard
        else None
    )


async def create_dashboard(
    session: AsyncSession, payload: DashboardCreate, *, scope: Scope, multi_workspace_enabled: bool
) -> DashboardDetail:
    """Create a dashboard with empty desktop/mobile layouts and publish revision one atomically."""
    fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
    )
    owner_id = _actor(scope)
    await _lock_owner_creation_quota(session, scope)
    count = (
        await session.scalar(
            select(func.count())
            .select_from(Dashboard)
            .where(Dashboard.workspace_id == scope.workspace_id, Dashboard.owner_id == owner_id)
        )
        or 0
    )
    if count >= MAX_DASHBOARDS_PER_OWNER:
        raise DashboardConflict("dashboard_limit", "Dashboard limit reached")
    dashboard = Dashboard(workspace_id=scope.workspace_id, owner_id=owner_id, name=payload.name)
    session.add(dashboard)
    await session.flush()
    session.add_all(
        [
            DashboardLayout(dashboard_id=dashboard.id, breakpoint=key, columns=20)
            for key in ("desktop", "mobile")
        ]
    )
    await commit_with_replay(
        session,
        [make_dashboard_change("dashboard", dashboard.id, dashboard.revision, scope=scope)],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )
    return await _dashboard_read(
        session, dashboard, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )


async def patch_dashboard(
    session: AsyncSession,
    dashboard_id: UUID,
    payload: DashboardPatch,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> DashboardDetail:
    """Rename an owned dashboard under its shared revision and publish one committed change."""
    fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
    )
    dashboard = await _lock_dashboard(session, dashboard_id, scope=scope)
    _check_revision(dashboard, payload.expected_revision)
    dashboard.name = payload.name
    revision = _bump(dashboard)
    await commit_with_replay(
        session,
        [make_dashboard_change("dashboard", dashboard.id, revision, scope=scope)],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )
    return await _dashboard_read(
        session, dashboard, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )


async def delete_dashboard(
    session: AsyncSession,
    dashboard_id: UUID,
    expected_revision: int,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Delete one dashboard tree while preserving reusable definitions and emitting its terminal revision."""
    fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
    )
    dashboard = await _lock_dashboard(session, dashboard_id, scope=scope)
    _check_revision(dashboard, expected_revision)
    revision = _bump(dashboard)
    await session.delete(dashboard)
    await commit_with_replay(
        session,
        [make_dashboard_change("dashboard", dashboard_id, revision, deleted=True, scope=scope)],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )


async def list_groups(
    session: AsyncSession, dashboard_id: UUID, *, scope: Scope, multi_workspace_enabled: bool
) -> list[DashboardGroupRead] | None:
    """List ordered groups only when the parent dashboard belongs to the authenticated owner."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if (
        await session.scalar(
            select(Dashboard.id).where(
                Dashboard.id == dashboard_id,
                Dashboard.workspace_id == scope.workspace_id,
                Dashboard.owner_id == _actor(scope),
            )
        )
        is None
    ):
        return None
    rows = await session.scalars(
        select(DashboardGroup)
        .join(Dashboard, Dashboard.id == DashboardGroup.dashboard_id)
        .where(
            DashboardGroup.dashboard_id == dashboard_id,
            Dashboard.workspace_id == scope.workspace_id,
        )
        .order_by(DashboardGroup.position, DashboardGroup.id)
    )
    return [
        DashboardGroupRead(
            id=row.id, dashboard_id=row.dashboard_id, name=row.name, position=row.position
        )
        for row in rows.all()
    ]


async def create_group(
    session: AsyncSession,
    dashboard_id: UUID,
    payload: GroupCreate,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> DashboardGroupRead:
    """Add a group under dashboard revision lock and publish the resulting dashboard revision."""
    fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
    )
    dashboard = await _lock_dashboard(session, dashboard_id, scope=scope)
    _check_revision(dashboard, payload.expected_revision)
    count = (
        await session.scalar(
            select(func.count())
            .select_from(DashboardGroup)
            .join(Dashboard, Dashboard.id == DashboardGroup.dashboard_id)
            .where(
                DashboardGroup.dashboard_id == dashboard_id,
                Dashboard.workspace_id == scope.workspace_id,
            )
        )
        or 0
    )
    if count >= MAX_GROUPS_PER_DASHBOARD:
        raise DashboardConflict("group_limit", "Group limit reached", dashboard.revision)
    group = DashboardGroup(dashboard_id=dashboard_id, name=payload.name, position=payload.position)
    session.add(group)
    revision = _bump(dashboard)
    await session.flush()
    await commit_with_replay(
        session,
        [make_dashboard_change("dashboard", dashboard_id, revision, scope=scope)],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )
    return DashboardGroupRead(
        id=group.id, dashboard_id=dashboard_id, name=group.name, position=group.position
    )


async def patch_group(
    session: AsyncSession,
    dashboard_id: UUID,
    group_id: UUID,
    payload: GroupPatch,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> DashboardGroupRead:
    """Update an owned group with explicit nullable-field semantics and one dashboard revision."""
    fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
    )
    dashboard = await _lock_dashboard(session, dashboard_id, scope=scope)
    _check_revision(dashboard, payload.expected_revision)
    group = await session.scalar(
        select(DashboardGroup)
        .join(Dashboard, Dashboard.id == DashboardGroup.dashboard_id)
        .where(
            DashboardGroup.id == group_id,
            DashboardGroup.dashboard_id == dashboard_id,
            Dashboard.workspace_id == scope.workspace_id,
        )
        .with_for_update()
    )
    if group is None:
        raise DashboardMissing
    if not payload.model_fields_set - {"expected_revision"}:
        raise ValueError("At least one group field is required")
    if "name" in payload.model_fields_set:
        if payload.name is None:
            raise ValueError("name cannot be cleared")
        group.name = payload.name
    if "position" in payload.model_fields_set:
        if payload.position is None:
            raise ValueError("position cannot be cleared")
        group.position = payload.position
    revision = _bump(dashboard)
    await commit_with_replay(
        session,
        [make_dashboard_change("dashboard", dashboard_id, revision, scope=scope)],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )
    return DashboardGroupRead(
        id=group.id, dashboard_id=dashboard_id, name=group.name, position=group.position
    )


async def delete_group(
    session: AsyncSession,
    dashboard_id: UUID,
    group_id: UUID,
    expected_revision: int,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Delete an empty group only; instances must be moved or explicitly removed first."""
    fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
    )
    dashboard = await _lock_dashboard(session, dashboard_id, scope=scope)
    _check_revision(dashboard, expected_revision)
    group = await session.scalar(
        select(DashboardGroup)
        .join(Dashboard, Dashboard.id == DashboardGroup.dashboard_id)
        .where(
            DashboardGroup.id == group_id,
            DashboardGroup.dashboard_id == dashboard_id,
            Dashboard.workspace_id == scope.workspace_id,
        )
        .with_for_update()
    )
    if group is None:
        raise DashboardMissing
    if (
        await session.scalar(
            select(GadgetInstance.id)
            .join(Dashboard, Dashboard.id == GadgetInstance.dashboard_id)
            .where(
                GadgetInstance.group_id == group_id, Dashboard.workspace_id == scope.workspace_id
            )
            .limit(1)
        )
        is not None
    ):
        raise DashboardConflict(
            "group_not_empty", "Move or delete group instances first", dashboard.revision
        )
    await session.delete(group)
    revision = _bump(dashboard)
    await commit_with_replay(
        session,
        [make_dashboard_change("dashboard", dashboard_id, revision, scope=scope)],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )


async def create_definition(
    session: AsyncSession,
    payload: GadgetDefinitionCreate,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> GadgetDefinitionRead:
    """Validate a planned renderer configuration, enforce source lifecycle and quota, then publish it."""
    fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
    )
    owner_id = _actor(scope)
    await _lock_owner_creation_quota(session, scope)
    descriptor = gadgets.renderer_descriptor(payload.renderer)
    configuration = gadgets.validate_renderer_configuration(
        payload.renderer,
        GadgetConfiguration(
            scope=payload.scope, filters=payload.filters, highlight_rules=payload.highlight_rules
        ),
    )
    await _lock_selected_sources(
        session,
        payload.source_ids,
        require_active=True,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )
    await _validate_selected_sources(
        session,
        payload.renderer,
        payload.source_ids,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        gadget_scope=configuration.scope,
    )
    await validate_highlight_rules(
        session, payload.source_ids, configuration.highlight_rules, scope=scope
    )
    count = (
        await session.scalar(
            select(func.count())
            .select_from(GadgetDefinition)
            .where(
                GadgetDefinition.workspace_id == scope.workspace_id,
                GadgetDefinition.owner_id == owner_id,
            )
        )
        or 0
    )
    if count >= MAX_DEFINITIONS_PER_OWNER:
        raise DashboardConflict("definition_limit", "Definition limit reached")
    row = GadgetDefinition(
        workspace_id=scope.workspace_id,
        owner_id=owner_id,
        name=payload.name,
        renderer=descriptor.id,
        source_ids=[str(item) for item in payload.source_ids],
        scope=configuration.scope.model_dump(mode="json"),
        filters=configuration.filters.model_dump(mode="json"),
        highlight_rules=[item.model_dump(mode="json") for item in configuration.highlight_rules],
    )
    session.add(row)
    await session.flush()
    await commit_with_replay(
        session,
        [make_dashboard_change("definition", row.id, row.revision, scope=scope)],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )
    return _definition_read(
        row,
        await _definition_source_states(
            session, [row], scope=scope, multi_workspace_enabled=multi_workspace_enabled
        ),
    )


async def _definition_dashboard_ids(
    session: AsyncSession, definition_id: UUID, *, scope: Scope
) -> list[UUID]:
    """Return the ids of workspace dashboards whose gadget instances reference ``definition_id`` (unlocked read)."""
    return list(
        (
            await session.scalars(
                select(GadgetInstance.dashboard_id)
                .join(Dashboard, Dashboard.id == GadgetInstance.dashboard_id)
                .where(
                    GadgetInstance.definition_id == definition_id,
                    Dashboard.workspace_id == scope.workspace_id,
                )
                .distinct()
            )
        ).all()
    )


def _patched_definition_candidate(
    payload: GadgetDefinitionPatch, row: GadgetDefinition
) -> tuple[dict[str, Any], GadgetConfiguration]:
    """Merge a definition patch over the stored row into ``(candidate fields, validated configuration)``.

    Omitted fields keep their stored value; ``scope``/``filters``/``highlight_rules`` fall back to the stored JSON.
    Pure apart from pydantic validation, which may raise ``ValidationError`` for a corrupt stored value.
    """
    candidate: dict[str, Any] = {
        "name": payload.name if payload.name is not None else row.name,
        "renderer": payload.renderer if payload.renderer is not None else row.renderer,
        "source_ids": payload.source_ids
        if payload.source_ids is not None
        else [UUID(str(item)) for item in row.source_ids],
        "scope": payload.scope if "scope" in payload.model_fields_set else None,
        "filters": payload.filters if "filters" in payload.model_fields_set else None,
        "highlight_rules": payload.highlight_rules
        if "highlight_rules" in payload.model_fields_set
        else None,
    }
    config = GadgetConfiguration(
        scope=candidate["scope"]
        if candidate["scope"] is not None
        else GadgetScope.model_validate(row.scope),
        filters=candidate["filters"]
        if candidate["filters"] is not None
        else GadgetFilters.model_validate(row.filters),
        highlight_rules=candidate["highlight_rules"]
        if candidate["highlight_rules"] is not None
        else [HighlightRule.model_validate(item) for item in row.highlight_rules],
    )
    return candidate, config


def _definition_changes(
    candidate: dict[str, Any], row: GadgetDefinition, config: GadgetConfiguration
) -> tuple[bool, bool]:
    """Return ``(binding_changed, scope_changed)`` for a patched definition against its stored row.

    Pure comparison of values (not field presence), so unchanged resent fields never trigger revalidation.
    """
    binding_changed = {str(item) for item in candidate["source_ids"]} != {
        str(item) for item in row.source_ids
    } or candidate["renderer"] != row.renderer
    # Only metrics/symbols/regions feed the measurement catalog rule; lookback and other selectors may differ
    # (the editor resends lookback_days) without re-validating a legacy row.
    stored_scope = GadgetScope.model_validate(row.scope)
    scope_changed = (
        config.scope.metrics != stored_scope.metrics
        or config.scope.symbols != stored_scope.symbols
        or config.scope.regions != stored_scope.regions
    )
    return binding_changed, scope_changed


async def patch_definition(
    session: AsyncSession,
    definition_id: UUID,
    payload: GadgetDefinitionPatch,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> GadgetDefinitionRead:
    """Patch reusable configuration before locking consuming dashboards, preserving renderer minima."""
    # Source locks precede definition and dashboard locks, so acquire candidate IDs first.
    fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
    )
    if "source_ids" in payload.model_fields_set and payload.source_ids is not None:
        await _lock_selected_sources(
            session,
            payload.source_ids,
            require_active=True,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=fence,
        )
    row = await _lock_definition(session, definition_id, scope=scope)
    _check_revision(row, payload.expected_revision)
    if payload.model_fields_set <= {"expected_revision"}:
        raise ValueError("At least one definition field is required")
    if any(
        getattr(payload, name) is None for name in payload.model_fields_set - {"expected_revision"}
    ):
        raise ValueError("Definition fields cannot be cleared")
    references = await _definition_dashboard_ids(session, definition_id, scope=scope)
    for dashboard_id in sorted(set(references), key=str):
        await _lock_dashboard(session, dashboard_id, scope=scope)
    if "renderer" in payload.model_fields_set and payload.renderer != row.renderer and references:
        raise DashboardConflict(
            "renderer_in_use", "Renderer cannot change while referenced", row.revision
        )
    candidate, config = _patched_definition_candidate(payload, row)
    gadgets.validate_renderer_configuration(candidate["renderer"], config)
    # Only re-check datasource compatibility when the binding changes, so older saved definitions keep saving.
    # Measurement scope is re-checked only when metrics/symbols or the binding changed, so legacy rows still rename.
    # Compare values, not field presence: the editor resends unchanged source_ids/scope on every save.
    binding_changed, scope_changed = _definition_changes(candidate, row, config)
    if binding_changed or scope_changed:
        await _validate_selected_sources(
            session,
            candidate["renderer"],
            candidate["source_ids"],
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            gadget_scope=config.scope,
            check_binding=binding_changed,
        )
    stored_topics = {
        UUID(str(topic)) for item in row.highlight_rules for topic in item.get("topic_ids", [])
    }
    await validate_highlight_rules(
        session,
        candidate["source_ids"],
        config.highlight_rules,
        scope=scope,
        known_topic_ids=stored_topics,
    )
    row.name = candidate["name"]
    row.renderer = candidate["renderer"]
    row.source_ids = [str(item) for item in candidate["source_ids"]]
    row.scope = config.scope.model_dump(mode="json")
    row.filters = config.filters.model_dump(mode="json")
    row.highlight_rules = [item.model_dump(mode="json") for item in config.highlight_rules]
    revision = _bump(row)
    await commit_with_replay(
        session,
        [make_dashboard_change("definition", definition_id, revision, scope=scope)],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )
    return _definition_read(
        row,
        await _definition_source_states(
            session, [row], scope=scope, multi_workspace_enabled=multi_workspace_enabled
        ),
    )


async def delete_definition(
    session: AsyncSession,
    definition_id: UUID,
    expected_revision: int,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Delete an unused definition under its lock; preserve references by rejecting in-use deletes."""
    fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
    )
    row = await _lock_definition(session, definition_id, scope=scope)
    _check_revision(row, expected_revision)
    if (
        await session.scalar(
            select(GadgetInstance.id)
            .join(Dashboard, Dashboard.id == GadgetInstance.dashboard_id)
            .where(
                GadgetInstance.definition_id == definition_id,
                Dashboard.workspace_id == scope.workspace_id,
            )
            .limit(1)
        )
        is not None
    ):
        raise DashboardConflict(
            "definition_in_use", "Definition is used by a dashboard", row.revision
        )
    revision = _bump(row)
    await session.delete(row)
    await commit_with_replay(
        session,
        [make_dashboard_change("definition", definition_id, revision, deleted=True, scope=scope)],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )


async def _resolve_instance_group(
    session: AsyncSession,
    dashboard: Dashboard,
    group_id: UUID | None,
    *,
    scope: Scope,
) -> DashboardGroup:
    """Return the target group for a new instance under the caller's dashboard lock.

    An explicit ``group_id`` must belong to the dashboard (else ``DashboardMissing``). When it is
    ``None`` the lowest-position group (id as tiebreak) is used; a dashboard with no groups gets a
    "General" group at position 0 added to the session. The caller owns the single revision bump
    and commit, so creating the group here adds no extra revision.
    """
    query = (
        select(DashboardGroup)
        .join(Dashboard, Dashboard.id == DashboardGroup.dashboard_id)
        .where(
            DashboardGroup.dashboard_id == dashboard.id,
            Dashboard.workspace_id == scope.workspace_id,
        )
    )
    if group_id is not None:
        group = await session.scalar(query.where(DashboardGroup.id == group_id))
        if group is None:
            raise DashboardMissing
        return group
    group = await session.scalar(
        query.order_by(DashboardGroup.position, DashboardGroup.id).limit(1)
    )
    if group is None:
        group = DashboardGroup(dashboard_id=dashboard.id, name="General", position=0)
        session.add(group)
        await session.flush()
    return group


async def create_instance(
    session: AsyncSession,
    dashboard_id: UUID,
    payload: InstanceCreate,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> DashboardDetail:
    """Create a cross-checked instance and both default placements under definition-before-dashboard locks.

    An omitted ``group_id`` falls back to the dashboard's first group, creating "General" if none exists.
    """
    fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
    )
    definition = await _lock_definition(session, payload.definition_id, scope=scope)
    dashboard = await _lock_dashboard(session, dashboard_id, scope=scope)
    _check_revision(dashboard, payload.expected_revision)
    group = await _resolve_instance_group(session, dashboard, payload.group_id, scope=scope)
    count = (
        await session.scalar(
            select(func.count())
            .select_from(GadgetInstance)
            .join(
                Dashboard,
                Dashboard.id == GadgetInstance.dashboard_id,
            )
            .where(
                GadgetInstance.dashboard_id == dashboard_id,
                Dashboard.workspace_id == scope.workspace_id,
            )
        )
        or 0
    )
    if count >= MAX_INSTANCES_PER_DASHBOARD:
        raise DashboardConflict("instance_limit", "Instance limit reached", dashboard.revision)
    instance = GadgetInstance(
        dashboard_id=dashboard_id,
        group_id=group.id,
        definition_id=definition.id,
        title=payload.title,
        position=payload.position,
    )
    session.add(instance)
    await session.flush()
    descriptor = gadgets.renderer_descriptor(definition.renderer)
    existing = list(
        (
            await session.scalars(
                select(GadgetPlacement)
                .join(
                    Dashboard,
                    Dashboard.id == GadgetPlacement.dashboard_id,
                )
                .where(
                    GadgetPlacement.dashboard_id == dashboard_id,
                    Dashboard.workspace_id == scope.workspace_id,
                )
            )
        ).all()
    )
    for breakpoint in ("desktop", "mobile"):
        layout_row = await session.scalar(
            select(DashboardLayout)
            .join(
                Dashboard,
                Dashboard.id == DashboardLayout.dashboard_id,
            )
            .where(
                DashboardLayout.dashboard_id == dashboard_id,
                DashboardLayout.breakpoint == breakpoint,
                Dashboard.workspace_id == scope.workspace_id,
            )
            .with_for_update()
        )
        if layout_row is None:
            layout_row = DashboardLayout(
                dashboard_id=dashboard_id, breakpoint=breakpoint, columns=20
            )
            session.add(layout_row)
        current = sorted(
            (item for item in existing if item.breakpoint == breakpoint),
            key=lambda item: (item.y, item.x, str(item.instance_id)),
        )
        if descriptor.minimum_width > layout_row.columns:
            raise ValueError("Renderer minimum does not fit the configured layout columns")
        default_width, default_height = gadgets.renderer_default_size(definition.renderer)
        occupied = [(item.x, item.y, item.w, item.h) for item in current]
        if breakpoint == "desktop":
            # Prefer the roomy default; fall back to the true minimum when only that fits.
            candidates = [
                (min(default_width, layout_row.columns), default_height),
                (descriptor.minimum_width, descriptor.minimum_height),
            ]
        else:
            candidates = [(layout_row.columns, default_height)]
        found = next(
            (
                (size, pos)
                for size in candidates
                if (pos := layouts.find_free_position(occupied, layout_row.columns, *size))
                is not None
            ),
            None,
        )
        if found is None:
            raise ValueError("No free layout position remains")
        (width, height), position = found
        x, y = position
        session.add(
            GadgetPlacement(
                dashboard_id=dashboard_id,
                breakpoint=breakpoint,
                instance_id=instance.id,
                x=x,
                y=y,
                w=width,
                h=height,
            )
        )
    revision = _bump(dashboard)
    await commit_with_replay(
        session,
        [make_dashboard_change("dashboard", dashboard_id, revision, scope=scope)],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )
    return await _dashboard_read(
        session, dashboard, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )


async def patch_instance(
    session: AsyncSession,
    dashboard_id: UUID,
    instance_id: UUID,
    payload: InstancePatch,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> DashboardDetail:
    """Edit local title/group/order under the dashboard revision while preserving layout geometry."""
    fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
    )
    instance_ref = await session.scalar(
        select(GadgetInstance.definition_id)
        .join(
            Dashboard,
            Dashboard.id == GadgetInstance.dashboard_id,
        )
        .where(
            GadgetInstance.id == instance_id,
            GadgetInstance.dashboard_id == dashboard_id,
            Dashboard.workspace_id == scope.workspace_id,
        )
    )
    if instance_ref is None:
        raise DashboardMissing
    await _lock_definition(session, instance_ref, scope=scope)
    dashboard = await _lock_dashboard(session, dashboard_id, scope=scope)
    _check_revision(dashboard, payload.expected_revision)
    instance = await session.scalar(
        select(GadgetInstance)
        .join(
            Dashboard,
            Dashboard.id == GadgetInstance.dashboard_id,
        )
        .where(
            GadgetInstance.id == instance_id,
            GadgetInstance.dashboard_id == dashboard_id,
            Dashboard.workspace_id == scope.workspace_id,
        )
        .with_for_update()
    )
    if instance is None:
        raise DashboardMissing
    if not payload.model_fields_set - {"expected_revision"}:
        raise ValueError("At least one instance field is required")
    if "title" in payload.model_fields_set:
        instance.title = payload.title
    if "group_id" in payload.model_fields_set:
        if payload.group_id is None:
            raise ValueError("group_id cannot be cleared")
        if (
            await session.scalar(
                select(DashboardGroup.id)
                .join(Dashboard, Dashboard.id == DashboardGroup.dashboard_id)
                .where(
                    DashboardGroup.id == payload.group_id,
                    DashboardGroup.dashboard_id == dashboard_id,
                    Dashboard.workspace_id == scope.workspace_id,
                )
            )
            is None
        ):
            raise DashboardMissing
        instance.group_id = payload.group_id
    if "position" in payload.model_fields_set:
        if payload.position is None:
            raise ValueError("position cannot be cleared")
        instance.position = payload.position
    revision = _bump(dashboard)
    await commit_with_replay(
        session,
        [make_dashboard_change("dashboard", dashboard_id, revision, scope=scope)],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )
    return await _dashboard_read(
        session, dashboard, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )


async def delete_instance(
    session: AsyncSession,
    dashboard_id: UUID,
    instance_id: UUID,
    expected_revision: int,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> DashboardDetail:
    """Remove an instance and both placements while retaining its reusable definition."""
    fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
    )
    definition_id = await session.scalar(
        select(GadgetInstance.definition_id)
        .join(
            Dashboard,
            Dashboard.id == GadgetInstance.dashboard_id,
        )
        .where(
            GadgetInstance.id == instance_id,
            GadgetInstance.dashboard_id == dashboard_id,
            Dashboard.workspace_id == scope.workspace_id,
        )
    )
    if definition_id is None:
        raise DashboardMissing
    await _lock_definition(session, definition_id, scope=scope)
    dashboard = await _lock_dashboard(session, dashboard_id, scope=scope)
    _check_revision(dashboard, expected_revision)
    await session.execute(
        delete(GadgetPlacement).where(
            GadgetPlacement.dashboard_id == dashboard_id, GadgetPlacement.instance_id == instance_id
        )
    )
    instance = await session.scalar(
        select(GadgetInstance)
        .join(
            Dashboard,
            Dashboard.id == GadgetInstance.dashboard_id,
        )
        .where(
            GadgetInstance.id == instance_id,
            GadgetInstance.dashboard_id == dashboard_id,
            Dashboard.workspace_id == scope.workspace_id,
        )
        .with_for_update()
    )
    if instance is None:
        raise DashboardMissing
    await session.delete(instance)
    revision = _bump(dashboard)
    await commit_with_replay(
        session,
        [make_dashboard_change("dashboard", dashboard_id, revision, scope=scope)],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )
    return await _dashboard_read(
        session, dashboard, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )


async def replace_layout(
    session: AsyncSession,
    dashboard_id: UUID,
    payload: LayoutReplace,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> DashboardDetail:
    """Replace only the selected breakpoint after exact membership, minima, bounds, and overlap checks.

    An exact geometry/column resave returns current detail without revision or replay changes.
    """
    fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
    )
    dashboard = await _lock_dashboard(session, dashboard_id, scope=scope)
    _check_revision(dashboard, payload.expected_revision)
    _instances, minimums, _ = await _instance_minima(session, dashboard_id, scope=scope)
    item_ids = {item.instance_id for item in payload.items}
    if item_ids != set(minimums):
        raise ValueError("Layout must include every dashboard instance exactly once")
    layouts.validate_layout(payload.items, payload.columns, minimums)
    layout_row = await session.scalar(
        select(DashboardLayout)
        .join(
            Dashboard,
            Dashboard.id == DashboardLayout.dashboard_id,
        )
        .where(
            DashboardLayout.dashboard_id == dashboard_id,
            DashboardLayout.breakpoint == payload.breakpoint,
            Dashboard.workspace_id == scope.workspace_id,
        )
        .with_for_update()
    )
    if layout_row is None:
        layout_row = DashboardLayout(
            dashboard_id=dashboard_id, breakpoint=payload.breakpoint, columns=payload.columns
        )
        session.add(layout_row)
    unchanged = layout_row.columns == payload.columns
    if unchanged:
        current = list(
            (
                await session.scalars(
                    select(GadgetPlacement).where(
                        GadgetPlacement.dashboard_id == dashboard_id,
                        GadgetPlacement.breakpoint == payload.breakpoint,
                    )
                )
            ).all()
        )
        current_by_id = {row.instance_id: (row.x, row.y, row.w, row.h) for row in current}
        unchanged = current_by_id == {
            item.instance_id: (item.x, item.y, item.w, item.h) for item in payload.items
        }
    if unchanged:
        return await _dashboard_read(
            session, dashboard, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
    layout_row.columns = payload.columns
    await session.execute(
        delete(GadgetPlacement).where(
            GadgetPlacement.dashboard_id == dashboard_id,
            GadgetPlacement.breakpoint == payload.breakpoint,
        )
    )
    session.add_all(
        [
            GadgetPlacement(
                dashboard_id=dashboard_id,
                breakpoint=payload.breakpoint,
                instance_id=item.instance_id,
                x=item.x,
                y=item.y,
                w=item.w,
                h=item.h,
            )
            for item in payload.items
        ]
    )
    revision = _bump(dashboard)
    await commit_with_replay(
        session,
        [make_dashboard_change("dashboard", dashboard_id, revision, scope=scope)],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )
    return await _dashboard_read(
        session, dashboard, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
