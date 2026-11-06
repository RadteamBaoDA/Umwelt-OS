"""Dashboard read projections, listing and source-usage counts (private to the dashboard package)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.admission import actor_id as _actor
from core.workspaces.schemas import AccessFence, Scope
from modules.dashboard import gadgets
from modules.dashboard._common import DashboardConflict, _admit
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
    DashboardDetail,
    DashboardSummary,
    GadgetDefinitionRead,
    GadgetScope,
)
from modules.sources import public as sources


async def _instance_minima(
    session: AsyncSession, dashboard_id: UUID, *, scope: Scope
) -> tuple[list[GadgetInstance], dict[UUID, tuple[int, int]], dict[UUID, GadgetDefinition]]:
    """Load bounded instances and their owner definitions for layout validation and projection."""
    rows = list(
        (
            await session.scalars(
                select(GadgetInstance)
                .join(Dashboard, Dashboard.id == GadgetInstance.dashboard_id)
                .where(
                    GadgetInstance.dashboard_id == dashboard_id,
                    Dashboard.workspace_id == scope.workspace_id,
                )
                .order_by(GadgetInstance.position, GadgetInstance.id)
            )
        ).all()
    )
    definitions = await _definitions_for_instances(session, rows, scope=scope)
    minimums = {
        row.id: (
            gadgets.renderer_descriptor(definitions[row.definition_id].renderer).minimum_width,
            gadgets.renderer_descriptor(definitions[row.definition_id].renderer).minimum_height,
        )
        for row in rows
    }
    return rows, minimums, definitions


async def _definitions_for_instances(
    session: AsyncSession, rows: Sequence[GadgetInstance], *, scope: Scope
) -> dict[UUID, GadgetDefinition]:
    """Fetch definitions in one bounded query and return them keyed by their stable IDs."""
    identifiers = {row.definition_id for row in rows}
    if not identifiers:
        return {}
    result = await session.scalars(
        select(GadgetDefinition).where(
            GadgetDefinition.id.in_(identifiers),
            GadgetDefinition.workspace_id == scope.workspace_id,
        )
    )
    return {row.id: row for row in result.all()}


async def _source_states(
    session: AsyncSession,
    source_ids: Sequence[UUID],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> dict[UUID, Any]:
    """Read the bounded owner source projection without copying connector secrets or content."""
    if not source_ids:
        return {}
    rows = await sources.get_gadget_sources(
        session,
        tuple(dict.fromkeys(source_ids)),
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    return {row.id: row for row in rows}


async def _definition_source_states(
    session: AsyncSession,
    definitions: Sequence[GadgetDefinition],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> dict[UUID, Any]:
    """Resolve lifecycle metadata for a bounded owner definition set in 32-ID source batches."""
    source_ids = list(
        dict.fromkeys(
            UUID(str(source_id))
            for definition in definitions
            for source_id in definition.source_ids
        )
    )
    states: dict[UUID, Any] = {}
    for offset in range(0, len(source_ids), 32):
        states.update(
            await _source_states(
                session,
                source_ids[offset : offset + 32],
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        )
    return states


def _definition_warnings(
    definition: GadgetDefinition, source_states: Mapping[UUID, Any]
) -> list[dict[str, str]]:
    """Describe adapter planning plus missing or inactive saved sources as metadata only."""
    warnings = _renderer_warnings(definition.renderer)
    for source_id in definition.source_ids:
        identifier = UUID(str(source_id))
        source = source_states.get(identifier)
        if source is None or source.status != "active":
            warnings.append({"code": "source_unavailable", "source_id": str(identifier)})
    return warnings


def _renderer_warnings(renderer_id: str) -> list[dict[str, str]]:
    """Warn only when the renderer or its production data adapter remains planned."""
    descriptor = gadgets.renderer_descriptor(renderer_id)
    if descriptor.runtime_state == "available":
        return []
    return [
        {"code": "renderer_planned"},
        *[
            {"code": "capability_unavailable", "capability": key}
            for key in descriptor.capability_keys
        ],
    ]


async def _dashboard_read(
    session: AsyncSession,
    dashboard: Dashboard,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> DashboardDetail:
    """Build dashboard detail with descriptor-derived renderer state and source lifecycle warnings."""
    await session.refresh(dashboard)
    groups = list(
        (
            await session.scalars(
                select(DashboardGroup)
                .join(Dashboard, Dashboard.id == DashboardGroup.dashboard_id)
                .where(
                    DashboardGroup.dashboard_id == dashboard.id,
                    Dashboard.workspace_id == scope.workspace_id,
                )
                .order_by(DashboardGroup.position, DashboardGroup.id)
            )
        ).all()
    )
    instances, _minimums, definitions = await _instance_minima(session, dashboard.id, scope=scope)
    source_ids = list(
        dict.fromkeys(
            UUID(str(source_id))
            for definition in definitions.values()
            for source_id in definition.source_ids
        )
    )
    source_states: dict[UUID, Any] = {}
    # Each source projection stays within its owning module's 32-ID query ceiling.
    for offset in range(0, len(source_ids), 32):
        source_states.update(
            await _source_states(
                session,
                source_ids[offset : offset + 32],
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        )
    placements = list(
        (
            await session.scalars(
                select(GadgetPlacement)
                .join(Dashboard, Dashboard.id == GadgetPlacement.dashboard_id)
                .where(
                    GadgetPlacement.dashboard_id == dashboard.id,
                    Dashboard.workspace_id == scope.workspace_id,
                )
            )
        ).all()
    )
    by_breakpoint: dict[str, list[dict[str, Any]]] = {"desktop": [], "mobile": []}
    for item in placements:
        by_breakpoint[item.breakpoint].append(
            {
                "instance_id": item.instance_id,
                "x": item.x,
                "y": item.y,
                "w": item.w,
                "h": item.h,
            }
        )
    layout_rows = list(
        (
            await session.scalars(
                select(DashboardLayout)
                .join(Dashboard, Dashboard.id == DashboardLayout.dashboard_id)
                .where(
                    DashboardLayout.dashboard_id == dashboard.id,
                    Dashboard.workspace_id == scope.workspace_id,
                )
            )
        ).all()
    )
    columns = {item.breakpoint: item.columns for item in layout_rows}
    instance_payloads = []
    for instance in instances:
        definition = definitions[instance.definition_id]
        instance_payloads.append(
            {
                "id": instance.id,
                "group_id": instance.group_id,
                "definition_id": definition.id,
                "title": instance.title,
                "position": instance.position,
                "definition": {
                    "id": definition.id,
                    "name": definition.name,
                    "revision": definition.revision,
                    "renderer": definition.renderer,
                    "source_ids": [UUID(str(item)) for item in definition.source_ids],
                    "scope": definition.scope,
                    "filters": definition.filters,
                    "highlight_rules": definition.highlight_rules,
                    "runtime_state": gadgets.renderer_descriptor(definition.renderer).runtime_state,
                    "warnings": _definition_warnings(definition, source_states),
                },
            }
        )
    return DashboardDetail.model_validate(
        {
            "id": dashboard.id,
            "name": dashboard.name,
            "revision": dashboard.revision,
            "created_at": dashboard.created_at,
            "updated_at": dashboard.updated_at,
            "groups": [
                {
                    "id": row.id,
                    "dashboard_id": dashboard.id,
                    "name": row.name,
                    "position": row.position,
                }
                for row in groups
            ],
            "instances": instance_payloads,
            "layouts": {
                key: {"columns": columns.get(key, 20), "items": by_breakpoint[key]}
                for key in ("desktop", "mobile")
            },
        }
    )


async def list_dashboards(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool
) -> list[DashboardSummary]:
    """List the authenticated owner's bounded dashboard summaries in stable creation order."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    rows = await session.scalars(
        select(Dashboard)
        .where(Dashboard.workspace_id == scope.workspace_id, Dashboard.owner_id == _actor(scope))
        .order_by(Dashboard.created_at, Dashboard.id)
        .limit(MAX_DASHBOARDS_PER_OWNER)
    )
    return [
        DashboardSummary(
            id=row.id,
            name=row.name,
            revision=row.revision,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows.all()
    ]


async def count_source_gadgets(
    session: AsyncSession,
    source_id: UUID,
    *,
    scope: Scope,
    cap: int = 1000,
) -> tuple[int, int]:
    """Return workspace-scoped (definition, distinct instance) counts that select a source, saturating at ``cap``."""
    selects = GadgetDefinition.source_ids.contains([str(source_id)])
    definitions = (
        select(GadgetDefinition.id)
        .where(GadgetDefinition.workspace_id == scope.workspace_id, selects)
        .limit(cap)
        .subquery()
    )
    placements = (
        select(GadgetInstance.id)
        .join(GadgetDefinition, GadgetDefinition.id == GadgetInstance.definition_id)
        .where(GadgetDefinition.workspace_id == scope.workspace_id, selects)
        .limit(cap)
        .subquery()
    )
    return (
        int(await session.scalar(select(func.count()).select_from(definitions)) or 0),
        int(await session.scalar(select(func.count()).select_from(placements)) or 0),
    )


async def list_definitions(
    session: AsyncSession, limit: int = 200, *, scope: Scope, multi_workspace_enabled: bool
) -> list[GadgetDefinitionRead]:
    """List a bounded owner library page of reusable configuration definitions."""
    if not 1 <= limit <= MAX_DEFINITIONS_PER_OWNER:
        raise ValueError("Definition page limit must be between 1 and 200")
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    rows = await session.scalars(
        select(GadgetDefinition)
        .where(
            GadgetDefinition.workspace_id == scope.workspace_id,
            GadgetDefinition.owner_id == _actor(scope),
        )
        .order_by(GadgetDefinition.created_at, GadgetDefinition.id)
        .limit(limit)
    )
    definitions = list(rows.all())
    source_states = await _definition_source_states(
        session, definitions, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    return [_definition_read(row, source_states) for row in definitions]


def _definition_read(
    row: GadgetDefinition, source_states: Mapping[UUID, Any]
) -> GadgetDefinitionRead:
    """Project config, descriptor-derived adapter state and current source warnings, never content."""
    return GadgetDefinitionRead.model_validate(
        {
            "id": row.id,
            "name": row.name,
            "revision": row.revision,
            "renderer": row.renderer,
            "source_ids": [UUID(str(item)) for item in row.source_ids],
            "scope": row.scope,
            "filters": row.filters,
            "highlight_rules": row.highlight_rules,
            "config_version": 1,
            "runtime_state": gadgets.renderer_descriptor(row.renderer).runtime_state,
            "warnings": _definition_warnings(row, source_states),
        }
    )


async def get_definition(
    session: AsyncSession, definition_id: UUID, *, scope: Scope, multi_workspace_enabled: bool
) -> GadgetDefinitionRead | None:
    """Return one owner-only reusable definition or no result for foreign identifiers."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    row = await session.scalar(
        select(GadgetDefinition).where(
            GadgetDefinition.id == definition_id,
            GadgetDefinition.workspace_id == scope.workspace_id,
            GadgetDefinition.owner_id == _actor(scope),
        )
    )
    if row is None:
        return None
    return _definition_read(
        row,
        await _definition_source_states(
            session, [row], scope=scope, multi_workspace_enabled=multi_workspace_enabled
        ),
    )


async def _lock_selected_sources(
    session: AsyncSession,
    source_ids: Sequence[UUID],
    *,
    require_active: bool,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> None:
    """Lock selected sources in UUID order before definitions or dashboards, then re-read lifecycle."""
    for source_id in sorted(set(source_ids), key=str):
        fence = await sources.lock_source(
            session,
            source_id,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            expected_access_fence=access_fence,
        )
        if fence is None or (require_active and fence.status != "active"):
            raise DashboardConflict("source_changed", "A selected source is unavailable")


async def _validate_selected_sources(
    session: AsyncSession,
    renderer: str,
    source_ids: Sequence[UUID],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    gadget_scope: GadgetScope | None = None,
    check_binding: bool = True,
) -> None:
    """Check selected sources' providers against the renderer's datasource rule (ValueError on mismatch).

    ``check_binding=False`` skips the datasource-compatibility rule (unchanged binding of a saved definition)
    and validates only the measurement scope.

    Missing/foreign IDs are skipped here; lifecycle checks in ``_lock_selected_sources`` own those errors.
    """
    rows = await sources.get_gadget_sources(
        session,
        tuple(dict.fromkeys(source_ids)),
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if check_binding:
        gadgets.validate_renderer_sources(renderer, [(row.provider, row.type) for row in rows])
    if (
        gadget_scope is not None
    ):  # measurement scope is checked against each bound provider's catalog
        gadgets.validate_measurement_scopes(renderer, [row.provider for row in rows], gadget_scope)
