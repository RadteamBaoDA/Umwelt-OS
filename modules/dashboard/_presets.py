"""Renderer/template/preset catalogs and preset preview/apply (private to the dashboard package)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.realtime import commit_with_replay, make_dashboard_change
from core.workspaces.admission import actor_id as _actor
from core.workspaces.schemas import Scope
from modules.connectors.public import metric_catalog, provider_metrics, source_data_shapes
from modules.dashboard import gadgets, layouts
from modules.dashboard._common import (
    DashboardConflict,
    DashboardMissing,
    _admit,
    _bump,
    _check_revision,
    _lock_dashboard,
    _lock_owner_creation_quota,
)
from modules.dashboard._reads import (
    _dashboard_read,
    _lock_selected_sources,
    _renderer_warnings,
    _source_states,
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
    DashboardDetail,
    DashboardPresetRead,
    GadgetScope,
    GadgetSourceOption,
    GadgetSourceOptionPage,
    GadgetTemplateRead,
    PresetApplyRequest,
    PresetPreviewRead,
    PresetPreviewRequest,
    RendererRead,
)
from modules.sources import public as sources


def renderer_reads() -> list[RendererRead]:
    """Project static renderer state and geometry without asserting runtime/provider acceptance."""
    return [
        RendererRead.model_validate(
            {
                "id": row.id,
                "config_version": row.config_version,
                "minimum_width": row.minimum_width,
                "minimum_height": row.minimum_height,
                "runtime_state": row.runtime_state,
                "capability_keys": list(row.capability_keys),
                "source_mode": row.source_mode,
                "max_sources": row.max_sources,
                "accepted_shapes": sorted(row.accepted_shapes)
                if row.accepted_shapes is not None
                else None,
            }
        )
        for row in gadgets.RENDERERS
    ]


def list_templates() -> list[GadgetTemplateRead]:
    """Project generic display templates with datasource compatibility taken from each renderer."""
    templates = []
    for row in gadgets.TEMPLATES:
        renderer = gadgets.renderer_descriptor(row.renderer)
        templates.append(
            GadgetTemplateRead.model_validate(
                {
                    "id": row.id,
                    "renderer": row.renderer,
                    "accepted_shapes": sorted(renderer.accepted_shapes)
                    if renderer.accepted_shapes is not None
                    else None,
                    "scope": row.scope,
                    "filters": row.filters,
                    "source_mode": renderer.source_mode,
                }
            )
        )
    return templates


async def list_gadget_sources(
    session: AsyncSession,
    limit: int,
    cursor: str | None,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> GadgetSourceOptionPage:
    """Return the source owner's bounded metadata-only page, adding data shapes computed here.

    Shapes are derived in the dashboard (connectors already imports sources; no reverse import).
    """
    page = await sources.list_gadget_sources(
        session, limit, cursor, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    return GadgetSourceOptionPage(
        items=[
            GadgetSourceOption(
                **item.model_dump(),
                data_shapes=sorted(source_data_shapes(item.provider, item.type)),
            )
            for item in page.items
        ],
        next_cursor=page.next_cursor,
    )


async def compatible_source_counts(
    session: AsyncSession,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> dict[str, int]:
    """Count active in-scope sources compatible with each source-binding renderer.

    Scans every active source (not a page) so the counts are exact; renderers with
    ``source_mode="none"`` are omitted and unconstrained renderers count all active sources.
    """
    kinds = await sources.list_active_source_kinds(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    shapes = [source_data_shapes(provider, kind) for _id, provider, kind in kinds]
    return {
        row.id: len(shapes)
        if row.accepted_shapes is None
        else sum(1 for item in shapes if item & row.accepted_shapes)
        for row in gadgets.RENDERERS
        if row.source_mode != "none"
    }


def metric_catalog_read() -> dict[str, dict[str, Any]]:
    """Serialise the provider metric catalog (dataclass fields pass through generically)."""
    return {provider: asdict(entry) for provider, entry in metric_catalog().items()}


# English display names matching the en-us gadget template names (web ``templateTypes``).
_RENDERER_DISPLAY_NAMES = {
    "news_feed": "Feed",
    "telegram_feed": "Message list",
    "text_panel": "Text",
    "table_panel": "Table",
    "video_panel": "Video",
    "finance_chart": "Chart",
    "map": "Map",
    "intelligence_panel": "Related signals",
    "highlights": "Highlights",
    "watch_rules": "Watchlist",
    "tasks": "Tasks",
    "goals": "Goals",
    "daily_brief": "Daily brief",
    "weather": "Weather",
    "provider_observation": "Metric",
    "github_project": "Activity",
    "job_list": "Jobs",
}


def preset_definition_name(renderer_id: str) -> str:
    """Name a preset-created definition after its template only; unknown renderers keep their id. Pure.

    Instances have no title of their own and show the definition name, so the dashboard/preset name
    must not be a prefix (it repeated on every gadget). Definition names carry no uniqueness constraint.
    """
    return _RENDERER_DISPLAY_NAMES.get(renderer_id, renderer_id)


def preset_catalog() -> list[DashboardPresetRead]:
    """Expose the canonical source-free preset identities and renderer slots (aliases excluded)."""
    return [
        DashboardPresetRead.model_validate(
            {
                "id": item.id,
                "label": item.label,
                "family": item.family,
                "slots": [
                    {"slot_id": slot.slot_id, "renderer": slot.renderer, "label": slot.label}
                    for slot in item.slots
                ],
            }
        )
        for item in gadgets.PRESETS
    ]


def _preview_preset_slot(
    slot: Any, source_ids: list[UUID], states: dict[UUID, Any]
) -> dict[str, Any]:
    """Build one slot of a preset preview from its resolved source states. Pure (no I/O).

    Adds ``source_unavailable``/``source_incompatible``/``missing_source``/``scope_required`` setup
    warnings; a single bound measurement source seeds the chart scope, and live map slots are
    provider-seeded with ``map_mode="live"`` instead of source-bound.
    """
    live = gadgets.preset_slot_is_live(slot)
    descriptor = gadgets.renderer_descriptor(slot.renderer)
    warnings = _renderer_warnings(slot.renderer)
    resolved = []
    for source_id in source_ids:
        source = states.get(source_id)
        if source is None or source.status != "active":
            warnings.append(
                {
                    "code": "source_unavailable",
                    "source_id": str(source_id),
                    "setup_group": "data_sources",
                }
            )
        resolved.append(
            {
                "id": source_id,
                "generation": source.generation if source else None,
                "status": source.status if source else "missing",
            }
        )
    known = [states[source_id] for source_id in source_ids if source_id in states]
    if known:
        try:
            gadgets.validate_renderer_sources(
                slot.renderer, [(row.provider, row.type) for row in known]
            )
        except gadgets.GadgetConfigError:
            warnings.append({"code": "source_incompatible", "setup_group": "data_sources"})
    elif not source_ids and descriptor.source_mode != "none" and not live:
        # Nothing was chosen and nothing compatible could be proposed: the owner must add a source.
        warnings.append({"code": "missing_source", "setup_group": "data_sources"})
    # A single bound measurement source seeds the chart scope; otherwise the gadget shows its setup message.
    slot_scope = (
        gadgets.preset_slot_scope(slot.renderer, known[0].provider)
        if len(source_ids) == 1
        and len(known) == 1
        and not any(w["code"] == "source_incompatible" for w in warnings)
        else {}
    )
    bound_catalog = (
        provider_metrics(known[0].provider) if len(known) == 1 and len(source_ids) == 1 else None
    )
    if (
        not slot_scope
        and bound_catalog is not None
        and slot.renderer == "finance_chart"
        and (bound_catalog.symbol_keyed or bound_catalog.region_keyed)
        and not any(w["code"] == "source_incompatible" for w in warnings)
    ):
        # Seeding found no suggested symbol/region: the chart cannot draw until one is chosen after apply.
        warnings.append({"code": "scope_required", "setup_group": "data_sources"})
    slot_filters: dict[str, Any] = {"keywords": [], "exclude_keywords": [], "limit": 25}
    if live:  # live maps are provider-seeded, never source-bound
        slot_scope = {"live_map_providers": list(gadgets.LIVE_MAP_DEFAULT_PROVIDERS)}
        slot_filters["map_mode"] = "live"
    return {
        "slot_id": slot.slot_id,
        "renderer": slot.renderer,
        "source_ids": source_ids,
        "scope": slot_scope,
        "filters": slot_filters,
        "highlight_rules": [],
        "sources": resolved,
        "warnings": warnings,
    }


async def preview_preset(
    session: AsyncSession,
    preset_id: str,
    payload: PresetPreviewRequest,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> PresetPreviewRead:
    """Resolve owner source choices into a canonical, nonpersistent preview.

    Slots without explicit sources get deterministic proposals (``gadgets.propose_slot_sources``) from the
    owner's active in-scope sources; the proposal is part of ``slots`` and thus of the fingerprint, and
    ``apply_preset`` recomputes it, so a changed source set yields ``preview_changed``. Live map slots
    (``gadgets.preset_slot_is_live``) bind no sources and are seeded with default live providers and
    ``map_mode="live"`` filters instead.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    preset = gadgets.dashboard_preset(preset_id)
    preset_id = preset.id  # alias ids share the canonical slot keys and fingerprint
    gadgets.validate_preset_slot_sources(preset_id, payload.slot_sources)
    # Explicit payload choices stay authoritative; slots left without any get the best active sources proposed.
    active_kinds: tuple[tuple[UUID, str | None, str], ...] = ()
    if any(
        not payload.slot_sources.get(slot.slot_id)
        and gadgets.renderer_descriptor(slot.renderer).source_mode != "none"
        and not gadgets.preset_slot_is_live(slot)
        for slot in preset.slots
    ):
        active_kinds = await sources.list_active_source_kinds(
            session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
    slot_source_ids: dict[str, list[UUID]] = {
        slot.slot_id: []
        if gadgets.preset_slot_is_live(slot)
        else list(
            payload.slot_sources.get(slot.slot_id)
            or gadgets.propose_slot_sources(slot.renderer, active_kinds, providers=slot.providers)
        )
        for slot in preset.slots
    }
    selected = sorted(
        {source_id for values in slot_source_ids.values() for source_id in values}, key=str
    )
    states: dict[UUID, Any] = {}
    for offset in range(0, len(selected), 32):
        states.update(
            await _source_states(
                session,
                selected[offset : offset + 32],
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        )
    target_revision = None
    if payload.target_dashboard_id:
        target = await session.scalar(
            select(Dashboard).where(
                Dashboard.id == payload.target_dashboard_id,
                Dashboard.workspace_id == scope.workspace_id,
                Dashboard.owner_id == _actor(scope),
            )
        )
        if target is None:
            raise DashboardMissing
        target_revision = target.revision
    slots = [
        _preview_preset_slot(slot, slot_source_ids[slot.slot_id], states) for slot in preset.slots
    ]
    minimums = {
        uuid5(NAMESPACE_URL, f"bbd-os-dashboard-preset:{preset_id}:{slot['slot_id']}"): (
            gadgets.renderer_descriptor(slot["renderer"]).minimum_width,
            gadgets.renderer_descriptor(slot["renderer"]).minimum_height,
        )
        for slot in slots
    }
    defaults = {
        key: gadgets.preset_slot_size(slot)
        for key, slot in zip(minimums, preset.slots, strict=True)
    }
    desktop = layouts.default_desktop_layout(defaults, minimum_sizes=minimums)
    mobile = layouts.default_mobile_layout(defaults, minimum_sizes=minimums)
    slot_ids_by_uuid = {
        uuid5(NAMESPACE_URL, f"bbd-os-dashboard-preset:{preset_id}:{slot['slot_id']}"): slot[
            "slot_id"
        ]
        for slot in slots
    }
    proposed = {
        "template_version": 1,
        "preset_id": preset_id,
        "name": preset.label,
        "slots": slots,
        "target_dashboard_id": payload.target_dashboard_id,
        "target_revision": target_revision,
        "layouts": {
            "desktop": {
                "columns": 20,
                "items": [
                    {
                        "slot_id": slot_ids_by_uuid[item.instance_id],
                        "x": item.x,
                        "y": item.y,
                        "w": item.w,
                        "h": item.h,
                    }
                    for item in desktop
                ],
            },
            "mobile": {
                "columns": 20,
                "items": [
                    {
                        "slot_id": slot_ids_by_uuid[item.instance_id],
                        "x": item.x,
                        "y": item.y,
                        "w": item.w,
                        "h": item.h,
                    }
                    for item in mobile
                ],
            },
        },
    }
    canonical = json.dumps(proposed, sort_keys=True, separators=(",", ":"), default=str).encode()
    return PresetPreviewRead.model_validate(
        {**proposed, "preview_fingerprint": hashlib.sha256(canonical).hexdigest()}
    )


async def _prepare_apply_target(
    session: AsyncSession,
    payload: PresetApplyRequest,
    current: dict[str, Any],
    dashboard: Dashboard | None,
    *,
    owner_id: int,
    scope: Scope,
) -> tuple[Dashboard, int, list[int]]:
    """Create, extend or reset the target dashboard for ``apply_preset``; return its new revision.

    ``create`` enforces the per-owner dashboard limit and inserts the dashboard plus empty desktop and
    mobile layouts; ``append`` enforces the instance and group limits on the locked target and bumps
    its revision (returning the existing group positions); ``replace`` bumps the revision, optionally
    renames and clears the target's groups and layouts. Raises ``DashboardConflict`` on a limit. Runs
    inside the caller's locked transaction (flushes only).
    """
    group_positions: list[int] = []
    if payload.mode == "create":
        count = (
            await session.scalar(
                select(func.count())
                .select_from(Dashboard)
                .where(
                    Dashboard.workspace_id == scope.workspace_id,
                    Dashboard.owner_id == owner_id,
                )
            )
            or 0
        )
        if count >= MAX_DASHBOARDS_PER_OWNER:
            raise DashboardConflict("dashboard_limit", "Dashboard limit reached")
        dashboard = Dashboard(
            workspace_id=scope.workspace_id, owner_id=owner_id, name=payload.name or current["name"]
        )
        session.add(dashboard)
        await session.flush()
        dashboard_revision = dashboard.revision
        session.add_all(
            [
                DashboardLayout(dashboard_id=dashboard.id, breakpoint=bp, columns=20)
                for bp in ("desktop", "mobile")
            ]
        )
    elif payload.mode == "append":
        if dashboard is None:
            raise RuntimeError("invariant violated: dashboard must not be None")
        instance_count = (
            await session.scalar(
                select(func.count())
                .select_from(GadgetInstance)
                .where(GadgetInstance.dashboard_id == dashboard.id)
            )
            or 0
        )
        if instance_count + len(current["slots"]) > MAX_INSTANCES_PER_DASHBOARD:
            raise DashboardConflict("instance_limit", "Instance limit reached", dashboard.revision)
        group_positions = list(
            (
                await session.scalars(
                    select(DashboardGroup.position).where(
                        DashboardGroup.dashboard_id == dashboard.id
                    )
                )
            ).all()
        )
        if len(group_positions) >= MAX_GROUPS_PER_DASHBOARD:
            raise DashboardConflict("group_limit", "Group limit reached", dashboard.revision)
        dashboard_revision = _bump(dashboard)
    else:
        if dashboard is None:
            raise RuntimeError("invariant violated: dashboard must not be None")
        dashboard_revision = _bump(dashboard)
        if payload.name is not None:
            dashboard.name = payload.name
        await session.execute(
            delete(DashboardGroup).where(DashboardGroup.dashboard_id == dashboard.id)
        )
        await session.execute(
            delete(DashboardLayout).where(DashboardLayout.dashboard_id == dashboard.id)
        )
        session.add_all(
            [
                DashboardLayout(dashboard_id=dashboard.id, breakpoint=bp, columns=20)
                for bp in ("desktop", "mobile")
            ]
        )
    if dashboard is None:
        raise RuntimeError("invariant violated: dashboard must not be None")
    return dashboard, dashboard_revision, group_positions


async def _place_applied_instances(
    session: AsyncSession,
    dashboard: Dashboard,
    created_instances: list[tuple[GadgetInstance, GadgetDefinition, Any]],
    preset_slots: Any,
    *,
    append: bool,
) -> None:
    """Insert desktop and mobile placements for the instances a preset created (flush-free ``add_all``).

    Appended blocks keep the target's column count and start below its lowest placement, so nothing
    overlaps; create/replace start from an empty layout at offset 0 with 20 columns.
    """
    minimums = {
        instance.id: (descriptor.minimum_width, descriptor.minimum_height)
        for instance, _, descriptor in created_instances
    }
    defaults = {
        instance.id: gadgets.preset_slot_size(preset_slot)
        for (instance, _, _), preset_slot in zip(created_instances, preset_slots, strict=True)
    }
    offsets = {"desktop": 0, "mobile": 0}
    columns = {"desktop": 20, "mobile": 20}
    if append:
        for breakpoint in offsets:
            offsets[breakpoint] = (
                await session.scalar(
                    select(func.max(GadgetPlacement.y + GadgetPlacement.h)).where(
                        GadgetPlacement.dashboard_id == dashboard.id,
                        GadgetPlacement.breakpoint == breakpoint,
                    )
                )
                or 0
            )
            columns[breakpoint] = (
                await session.scalar(
                    select(DashboardLayout.columns).where(
                        DashboardLayout.dashboard_id == dashboard.id,
                        DashboardLayout.breakpoint == breakpoint,
                    )
                )
                or 20
            )
    desktop = layouts.default_desktop_layout(
        {
            key: (min(width, columns["desktop"]), height)
            for key, (width, height) in defaults.items()
        },
        columns["desktop"],
        minimum_sizes=minimums,
    )
    mobile = layouts.default_mobile_layout(defaults, columns["mobile"], minimum_sizes=minimums)
    for breakpoint, items in (("desktop", desktop), ("mobile", mobile)):
        session.add_all(
            [
                GadgetPlacement(
                    dashboard_id=dashboard.id,
                    breakpoint=breakpoint,
                    instance_id=item.instance_id,
                    x=item.x,
                    y=item.y + offsets[breakpoint],
                    w=item.w,
                    h=item.h,
                )
                for item in items
            ]
        )


async def apply_preset(
    session: AsyncSession,
    preset_id: str,
    payload: PresetApplyRequest,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> DashboardDetail:
    """Recompute and fingerprint preset state under ordered source/dashboard locks before atomic apply.

    ``create`` makes a new dashboard; ``replace`` clears the target's groups and layouts first; ``append``
    keeps everything on the target and adds the preset as one new group placed below the existing gadgets
    (subject to the per-dashboard instance and group limits). Replace and append both check the revision.
    """
    fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
    )
    owner_id = _actor(scope)
    await _lock_owner_creation_quota(session, scope)
    gadgets.dashboard_preset(preset_id)
    await _lock_selected_sources(
        session,
        [source_id for values in payload.slot_sources.values() for source_id in values],
        require_active=True,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )
    # Slot sources must satisfy each slot renderer's datasource rule, like direct definition creation.
    for slot in gadgets.dashboard_preset(preset_id).slots:
        await _validate_selected_sources(
            session,
            slot.renderer,
            payload.slot_sources.get(slot.slot_id, ()),
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    target_dashboard_id = payload.target_dashboard_id
    if payload.mode in ("replace", "append"):
        # both modes require them
        if target_dashboard_id is None or payload.expected_revision is None:
            raise RuntimeError(
                "invariant violated: target_dashboard_id and payload.expected_revision must not be None"
            )
        dashboard = await _lock_dashboard(session, target_dashboard_id, scope=scope)
        _check_revision(dashboard, payload.expected_revision)
    else:
        dashboard = None
    current = (
        await preview_preset(
            session,
            preset_id,
            PresetPreviewRequest(
                slot_sources=payload.slot_sources, target_dashboard_id=target_dashboard_id
            ),
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    ).model_dump(mode="python")
    if current["preview_fingerprint"] != payload.preview_fingerprint:
        raise DashboardConflict(
            "preview_changed",
            "Preset preview changed; review it again",
            dashboard.revision if dashboard else None,
        )
    # Proposed (not payload-supplied) sources get the same lock and datasource-rule checks as explicit ones.
    proposed_ids = [
        sid
        for slot in current["slots"]
        for sid in slot["source_ids"]
        if sid not in {x for values in payload.slot_sources.values() for x in values}
    ]
    if proposed_ids:
        await _lock_selected_sources(
            session,
            proposed_ids,
            require_active=True,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=fence,
        )
        for slot in current["slots"]:
            await _validate_selected_sources(
                session,
                slot["renderer"],
                slot["source_ids"],
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
    # Seeded measurement scopes must satisfy the bound provider's catalog before they are persisted.
    for slot in current["slots"]:
        if slot["scope"]:
            await _validate_selected_sources(
                session,
                slot["renderer"],
                slot["source_ids"],
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
                gadget_scope=GadgetScope(**slot["scope"]),
                check_binding=False,
            )
    dashboard, dashboard_revision, group_positions = await _prepare_apply_target(
        session,
        payload,
        current,
        dashboard,
        owner_id=owner_id,
        scope=scope,
    )
    definition_count = (
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
    if definition_count + len(current["slots"]) > MAX_DEFINITIONS_PER_OWNER:
        raise DashboardConflict("definition_limit", "Definition limit reached", dashboard_revision)
    if dashboard is None:
        raise RuntimeError("invariant violated: dashboard must not be None")
    # Append puts the preset in its own group after the existing ones; create/replace start from an empty dashboard.
    group_position = max(group_positions, default=-1) + 1 if payload.mode == "append" else 0
    group = DashboardGroup(dashboard_id=dashboard.id, name=current["name"], position=group_position)
    session.add(group)
    await session.flush()
    created_instances: list[tuple[GadgetInstance, GadgetDefinition, Any]] = []
    preset_slots = gadgets.dashboard_preset(preset_id).slots
    for index, (slot, preset_slot) in enumerate(zip(current["slots"], preset_slots, strict=True)):
        descriptor = gadgets.renderer_descriptor(slot["renderer"])
        definition = GadgetDefinition(
            workspace_id=scope.workspace_id,
            owner_id=owner_id,
            name=preset_slot.label or preset_definition_name(slot["renderer"]),
            renderer=slot["renderer"],
            source_ids=[str(item) for item in slot["source_ids"]],
            scope=slot["scope"],
            filters=slot["filters"],
            highlight_rules=[],
        )
        session.add(definition)
        await session.flush()
        if dashboard is None:
            raise RuntimeError("invariant violated: dashboard must not be None")
        instance = GadgetInstance(
            dashboard_id=dashboard.id,
            group_id=group.id,
            definition_id=definition.id,
            title=None,
            position=index,
        )
        session.add(instance)
        await session.flush()
        created_instances.append((instance, definition, descriptor))
    await _place_applied_instances(
        session,
        dashboard,
        created_instances,
        preset_slots,
        append=payload.mode == "append",
    )
    drafts = [make_dashboard_change("dashboard", dashboard.id, dashboard_revision, scope=scope)]
    drafts.extend(
        make_dashboard_change("definition", definition.id, definition.revision, scope=scope)
        for _, definition, _descriptor in created_instances
    )
    await commit_with_replay(
        session,
        drafts,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )
    return await _dashboard_read(
        session, dashboard, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
