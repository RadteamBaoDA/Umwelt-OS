"""Owner-facing dashboard queries and atomic configuration mutations.

Routes use this module instead of importing dashboard persistence models. Every
mutation derives ownership from the authenticated session, serializes revision
changes under the dashboard/definition row locks, and publishes its revision
through the shared replay transaction.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID, uuid5, NAMESPACE_URL

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.realtime import commit_with_replay, make_dashboard_change
from modules.dashboard import gadgets, layouts
from modules.dashboard.models import (
    Dashboard,
    DashboardGroup,
    DashboardLayout,
    GadgetDefinition,
    GadgetInstance,
    GadgetPlacement,
)
from modules.dashboard.schemas import (
    DashboardCreate,
    DashboardPatch,
    GadgetDefinitionCreate,
    GadgetDefinitionPatch,
    GadgetConfiguration,
    GadgetFilters,
    GadgetScope,
    HighlightRule,
    DashboardDetail,
    DashboardGroupRead,
    DashboardSummary,
    GadgetDefinitionRead,
    DashboardHighlightRead,
    RendererRead,
    DashboardPresetRead,
    PresetPreviewRead,
    GroupCreate,
    GroupPatch,
    InstanceCreate,
    InstancePatch,
    LayoutReplace,
    PresetApplyRequest,
    PresetPreviewRequest,
    MAX_DASHBOARDS_PER_OWNER,
    MAX_DEFINITIONS_PER_OWNER,
    MAX_GROUPS_PER_DASHBOARD,
    MAX_INSTANCES_PER_DASHBOARD,
    MAX_RULES_PER_DEFINITION,
)
from modules.sources.schemas import GadgetSourceSelectionPage
from modules.sources import public as sources

MAX_REVISION = 9_007_199_254_740_991
DASHBOARD_QUOTA_LOCK_NAMESPACE = 4_603_202
MAX_HIGHLIGHT_NOTIFICATIONS_PER_TRANSACTION = 100
# A document can match every configured rule; keep a whole page below the insertion cap so advancing
# its cursor never drops an eligible rule/version notification.
HIGHLIGHT_SCAN_PAGE_LIMIT = max(
    1, MAX_HIGHLIGHT_NOTIFICATIONS_PER_TRANSACTION // MAX_RULES_PER_DEFINITION,
)
HIGHLIGHT_MATCHES_PER_PAGE_MAX = HIGHLIGHT_SCAN_PAGE_LIMIT * MAX_RULES_PER_DEFINITION


async def evaluate_gadget_highlights(
    session: AsyncSession, owner_id: int, definition_id: UUID, *, emit_notifications: bool = False,
) -> list[DashboardHighlightRead]:
    """Evaluate current evidence and durably page scans with at most 96 rule/version matches.

    Scheduled pages hold at most three documents and the schema caps each definition at 32 rules.
    The resulting 3-by-32 ceiling keeps every matching notification in the same cursor transaction.
    """
    from modules.dashboard.highlights import evaluate_highlights
    from modules.dashboard.models import GadgetHighlightProgress
    from modules.knowledge.documents import public as documents
    from modules.dashboard.schemas import HighlightRule
    from modules.notifications.public import NotificationEmit, emit

    if emit_notifications:
        # Definition writers serialize on this row. Keep it locked through evidence validation,
        # notification inserts, and cursor commit so edits cannot race an old scan into emission.
        definition = await session.scalar(select(GadgetDefinition).where(
            GadgetDefinition.id == definition_id, GadgetDefinition.owner_id == owner_id,
        ).with_for_update())
        if definition is None:
            raise DashboardMissing
        if definition.renderer not in {"highlights", "watch_rules"}:
            raise ValueError("Renderer does not support highlight evaluation")
        source_ids = tuple(UUID(str(value)) for value in definition.source_ids[:32])
        rules = [HighlightRule.model_validate(rule) for rule in definition.highlight_rules]
        if len(rules) > MAX_RULES_PER_DEFINITION:
            raise ValueError("Highlight rule count exceeds the validated definition bound")
        raw_item_scope = definition.scope.get("source_item_ids", [])
        item_scope = {str(value) for value in raw_item_scope} if isinstance(raw_item_scope, list) else set()
        if not source_ids or not rules:
            return []
        rules_fingerprint = hashlib.sha256(json.dumps(
            {"source_ids": [str(value) for value in source_ids], "scope": sorted(item_scope),
             "rules": [rule.model_dump(mode="json") for rule in rules]},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        progress = await session.get(GadgetHighlightProgress, definition.id, with_for_update=True)
        if progress is None:
            progress = GadgetHighlightProgress(
                definition_id=definition.id, definition_revision=definition.revision,
                rules_fingerprint=rules_fingerprint,
            )
            session.add(progress)
            await session.flush()
        elif (
            progress.definition_revision != definition.revision
            or progress.rules_fingerprint != rules_fingerprint
        ):
            progress.definition_revision = definition.revision
            progress.rules_fingerprint = rules_fingerprint
            progress.cursor_created_at = None
            progress.cursor_version_id = None
        page = await documents.list_gadget_highlight_projection_page(
            session, source_ids=source_ids, limit=HIGHLIGHT_SCAN_PAGE_LIMIT,
            cursor_created_at=progress.cursor_created_at,
            cursor_version_id=progress.cursor_version_id,
        )
        if len(page.items) * len(rules) > HIGHLIGHT_MATCHES_PER_PAGE_MAX:
            raise RuntimeError("Highlight scan page exceeds its notification coverage bound")
        if page.selection_fences and not await documents.validate_gadget_document_selection_fences(
            session, tuple(page.selection_fences), lock_rows=True,
            max_documents=HIGHLIGHT_SCAN_PAGE_LIMIT,
        ):
            raise RuntimeError("Dashboard highlight evidence changed during notification evaluation")
        progress.cursor_created_at = page.cursor_created_at if page.has_more else None
        progress.cursor_version_id = page.cursor_version_id if page.has_more else None

        matches: list[DashboardHighlightRead] = []
        for item in page.items:
            if item_scope and str(item.document_id) not in item_scope:
                continue
            for match in evaluate_highlights(item.excerpt, rules):
                matches.append(DashboardHighlightRead(
                    document_id=item.document_id, document_version_id=item.document_version_id,
                    source_id=item.source_id, title=item.title, observed_at=item.observed_at,
                    rule_id=match.rule_id, matched_keywords=list(match.matched_keywords),
                    severity=match.severity, notify=match.notify, reason=match.reason,
                ))
                if match.notify:
                    await emit(session, owner_id, NotificationEmit(
                        dedupe_key=(
                            f"highlight:{definition.id}:{definition.revision}:"
                            f"{rules_fingerprint}:{match.rule_id}:{item.document_version_id}"
                        ),
                        kind="dashboard_highlight", title=item.title[:300],
                        body=match.reason[:1000],
                        params={"severity": match.severity, "definition_id": str(definition.id),
                                "definition_revision": definition.revision},
                        link="/dashboard",
                    ))
        # Progress and notifications form one transaction: a retry can neither skip an alert nor
        # advance beyond a page whose notifications were not committed.
        await session.commit()
        return matches[:100]

    definition = await get_definition(session, owner_id, definition_id)
    if definition is None:
        raise DashboardMissing
    if definition.renderer not in {"highlights", "watch_rules"}:
        raise ValueError("Renderer does not support highlight evaluation")
    source_ids = tuple(definition.source_ids[:32])
    if not source_ids:
        return []
    projection_page = await documents.list_gadget_document_projections(
        session, owner_id=owner_id, source_ids=source_ids, limit=100,
    )
    rules = [HighlightRule.model_validate(rule) for rule in definition.highlight_rules]
    raw_item_scope = definition.scope.get("source_item_ids", [])
    item_scope = {str(value) for value in raw_item_scope} if isinstance(raw_item_scope, list) else set()
    matches = []
    for item in projection_page.items:
        if item_scope and str(item.document_id) not in item_scope:
            continue
        for match in evaluate_highlights(item.excerpt, rules):
            matches.append(DashboardHighlightRead(
                document_id=item.document_id, document_version_id=item.document_version_id,
                source_id=item.source_id, title=item.title, observed_at=item.observed_at,
                rule_id=match.rule_id, matched_keywords=list(match.matched_keywords),
                severity=match.severity, notify=match.notify, reason=match.reason,
            ))
    return matches[:100]


class DashboardConflict(Exception):
    """Represent a stale revision, exhausted counter, or invariant conflict for route mapping."""

    def __init__(self, code: str, message: str, current_revision: int | None = None) -> None:
        """Keep the stable machine code and optional current revision for HTTP 409 responses."""
        super().__init__(message)
        self.code = code
        self.current_revision = current_revision


class DashboardMissing(Exception):
    """Represent an absent or foreign-owned dashboard resource without disclosing its owner."""


async def _lock_dashboard(
    session: AsyncSession, owner_id: int, dashboard_id: UUID
) -> Dashboard:
    """Lock one owner-scoped dashboard row and refresh it before revision checks."""
    row = await session.scalar(
        select(Dashboard)
        .where(Dashboard.id == dashboard_id, Dashboard.owner_id == owner_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise DashboardMissing
    return row


async def _lock_definition(
    session: AsyncSession, owner_id: int, definition_id: UUID
) -> GadgetDefinition:
    """Lock one owner-scoped definition before querying dashboard references."""
    row = await session.scalar(
        select(GadgetDefinition)
        .where(GadgetDefinition.id == definition_id, GadgetDefinition.owner_id == owner_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise DashboardMissing
    return row


def _check_revision(row: Dashboard | GadgetDefinition, expected: int) -> None:
    """Reject stale edits and exhausted JavaScript-safe revision counters before mutation."""
    if row.revision != expected:
        raise DashboardConflict("revision_conflict", "The resource changed", row.revision)
    if row.revision >= MAX_REVISION:
        raise DashboardConflict("revision_exhausted", "The resource revision is exhausted", row.revision)


def _bump(row: Dashboard | GadgetDefinition) -> int:
    """Advance one already-locked revision exactly once and return its emitted value."""
    if row.revision >= MAX_REVISION:
        raise DashboardConflict("revision_exhausted", "The resource revision is exhausted", row.revision)
    row.revision += 1
    return row.revision


async def _lock_owner_creation_quota(session: AsyncSession, owner_id: int) -> None:
    """Serialize owner-wide dashboard/definition quota checks across concurrent requests.

    PostgreSQL transaction advisory locks use a namespace distinct from search
    indexing; all creation paths acquire this before source/entity locks and
    hold it until commit. This bounds quota oversubscription without a process lock.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:namespace, :owner_id)"),
        {"namespace": DASHBOARD_QUOTA_LOCK_NAMESPACE, "owner_id": owner_id},
    )


async def _instance_minima(
    session: AsyncSession, dashboard_id: UUID
) -> tuple[list[GadgetInstance], dict[UUID, tuple[int, int]], dict[UUID, GadgetDefinition]]:
    """Load bounded instances and their owner definitions for layout validation and projection."""
    rows = list(
        (await session.scalars(
            select(GadgetInstance)
            .where(GadgetInstance.dashboard_id == dashboard_id)
            .order_by(GadgetInstance.position, GadgetInstance.id)
        )).all()
    )
    definitions = await _definitions_for_instances(session, rows)
    minimums = {
        row.id: (
            gadgets.renderer_descriptor(definitions[row.definition_id].renderer).minimum_width,
            gadgets.renderer_descriptor(definitions[row.definition_id].renderer).minimum_height,
        )
        for row in rows
    }
    return rows, minimums, definitions


async def _definitions_for_instances(
    session: AsyncSession, rows: Sequence[GadgetInstance]
) -> dict[UUID, GadgetDefinition]:
    """Fetch definitions in one bounded query and return them keyed by their stable IDs."""
    identifiers = {row.definition_id for row in rows}
    if not identifiers:
        return {}
    result = await session.scalars(
        select(GadgetDefinition).where(GadgetDefinition.id.in_(identifiers))
    )
    return {row.id: row for row in result.all()}


async def _source_states(
    session: AsyncSession, source_ids: Sequence[UUID]
) -> dict[UUID, Any]:
    """Read the bounded owner source projection without copying connector secrets or content."""
    if not source_ids:
        return {}
    rows = await sources.get_gadget_sources(session, tuple(dict.fromkeys(source_ids)))
    return {row.id: row for row in rows}


async def _definition_source_states(
    session: AsyncSession, definitions: Sequence[GadgetDefinition]
) -> dict[UUID, Any]:
    """Resolve lifecycle metadata for a bounded owner definition set in 32-ID source batches."""
    source_ids = list(dict.fromkeys(
        UUID(str(source_id)) for definition in definitions for source_id in definition.source_ids
    ))
    states: dict[UUID, Any] = {}
    for offset in range(0, len(source_ids), 32):
        states.update(await _source_states(session, source_ids[offset : offset + 32]))
    return states


def _definition_warnings(
    definition: GadgetDefinition, source_states: Mapping[UUID, Any]
) -> list[dict[str, str]]:
    """Describe adapter planning plus missing or inactive saved sources as metadata only."""
    warnings = _renderer_warnings(definition.renderer)
    for source_id in definition.source_ids:
        identifier = UUID(str(source_id))
        source = source_states.get(identifier)
        if source is None:
            warnings.append({"code": "source_unavailable", "source_id": str(identifier)})
        elif source.status != "active":
            warnings.append({"code": "source_unavailable", "source_id": str(identifier)})
    return warnings


def _renderer_warnings(renderer_id: str) -> list[dict[str, str]]:
    """Warn only when the renderer or its production data adapter remains planned."""
    descriptor = gadgets.renderer_descriptor(renderer_id)
    if descriptor.runtime_state == "available":
        return []
    return [{"code": "renderer_planned"}, *[
        {"code": "capability_unavailable", "capability": key}
        for key in descriptor.capability_keys
    ]]


async def _dashboard_read(session: AsyncSession, dashboard: Dashboard) -> DashboardDetail:
    """Build dashboard detail with descriptor-derived renderer state and source lifecycle warnings."""
    await session.refresh(dashboard)
    groups = list((await session.scalars(
        select(DashboardGroup).where(DashboardGroup.dashboard_id == dashboard.id)
        .order_by(DashboardGroup.position, DashboardGroup.id)
    )).all())
    instances, _minimums, definitions = await _instance_minima(session, dashboard.id)
    source_ids = list(dict.fromkeys(
        UUID(str(source_id)) for definition in definitions.values() for source_id in definition.source_ids
    ))
    source_states: dict[UUID, Any] = {}
    # Each source projection stays within its owning module's 32-ID query ceiling.
    for offset in range(0, len(source_ids), 32):
        source_states.update(await _source_states(session, source_ids[offset : offset + 32]))
    placements = list((await session.scalars(
        select(GadgetPlacement).where(GadgetPlacement.dashboard_id == dashboard.id)
    )).all())
    by_breakpoint: dict[str, list[dict[str, Any]]] = {"desktop": [], "mobile": []}
    for item in placements:
        by_breakpoint[item.breakpoint].append({
            "instance_id": item.instance_id, "x": item.x, "y": item.y,
            "w": item.w, "h": item.h,
        })
    layout_rows = list((await session.scalars(
        select(DashboardLayout).where(DashboardLayout.dashboard_id == dashboard.id)
    )).all())
    columns = {item.breakpoint: item.columns for item in layout_rows}
    instance_payloads = []
    for instance in instances:
        definition = definitions[instance.definition_id]
        instance_payloads.append({
            "id": instance.id, "group_id": instance.group_id, "definition_id": definition.id,
            "title": instance.title, "position": instance.position,
            "definition": {
                "id": definition.id, "name": definition.name, "revision": definition.revision,
                "renderer": definition.renderer, "source_ids": [UUID(str(item)) for item in definition.source_ids],
                "scope": definition.scope, "filters": definition.filters,
                "highlight_rules": definition.highlight_rules,
                "runtime_state": gadgets.renderer_descriptor(definition.renderer).runtime_state,
                "warnings": _definition_warnings(definition, source_states),
            },
        })
    return DashboardDetail.model_validate({
        "id": dashboard.id, "name": dashboard.name, "revision": dashboard.revision,
        "created_at": dashboard.created_at, "updated_at": dashboard.updated_at,
        "groups": [{"id": row.id, "dashboard_id": dashboard.id, "name": row.name, "position": row.position} for row in groups],
        "instances": instance_payloads,
        "layouts": {
            key: {"columns": columns.get(key, 20), "items": by_breakpoint[key]}
            for key in ("desktop", "mobile")
        },
    })


async def list_dashboards(session: AsyncSession, owner_id: int) -> list[DashboardSummary]:
    """List the authenticated owner's bounded dashboard summaries in stable creation order."""
    rows = await session.scalars(
        select(Dashboard).where(Dashboard.owner_id == owner_id)
        .order_by(Dashboard.created_at, Dashboard.id).limit(MAX_DASHBOARDS_PER_OWNER)
    )
    return [DashboardSummary(id=row.id, name=row.name, revision=row.revision,
            created_at=row.created_at, updated_at=row.updated_at) for row in rows.all()]


async def get_dashboard(session: AsyncSession, owner_id: int, dashboard_id: UUID) -> DashboardDetail | None:
    """Return a detached owner-only dashboard configuration, without renderer payload data."""
    dashboard = await session.scalar(select(Dashboard).where(
        Dashboard.id == dashboard_id, Dashboard.owner_id == owner_id
    ))
    return await _dashboard_read(session, dashboard) if dashboard else None


async def create_dashboard(session: AsyncSession, owner_id: int, payload: DashboardCreate) -> DashboardDetail:
    """Create a dashboard with empty desktop/mobile layouts and publish revision one atomically."""
    await _lock_owner_creation_quota(session, owner_id)
    count = await session.scalar(select(func.count()).select_from(Dashboard).where(Dashboard.owner_id == owner_id))
    if count >= MAX_DASHBOARDS_PER_OWNER:
        raise DashboardConflict("dashboard_limit", "Dashboard limit reached")
    dashboard = Dashboard(owner_id=owner_id, name=payload.name)
    session.add(dashboard)
    await session.flush()
    session.add_all([
        DashboardLayout(dashboard_id=dashboard.id, breakpoint=key, columns=20)
        for key in ("desktop", "mobile")
    ])
    await commit_with_replay(session, [make_dashboard_change("dashboard", dashboard.id, dashboard.revision)])
    return await _dashboard_read(session, dashboard)


async def patch_dashboard(
    session: AsyncSession, owner_id: int, dashboard_id: UUID, payload: DashboardPatch
) -> DashboardDetail:
    """Rename an owned dashboard under its shared revision and publish one committed change."""
    dashboard = await _lock_dashboard(session, owner_id, dashboard_id)
    _check_revision(dashboard, payload.expected_revision)
    dashboard.name = payload.name
    revision = _bump(dashboard)
    await commit_with_replay(session, [make_dashboard_change("dashboard", dashboard.id, revision)])
    return await _dashboard_read(session, dashboard)


async def delete_dashboard(session: AsyncSession, owner_id: int, dashboard_id: UUID, expected_revision: int) -> None:
    """Delete one dashboard tree while preserving reusable definitions and emitting its terminal revision."""
    dashboard = await _lock_dashboard(session, owner_id, dashboard_id)
    _check_revision(dashboard, expected_revision)
    revision = _bump(dashboard)
    await session.delete(dashboard)
    await commit_with_replay(session, [make_dashboard_change("dashboard", dashboard_id, revision, deleted=True)])


async def list_groups(session: AsyncSession, owner_id: int, dashboard_id: UUID) -> list[DashboardGroupRead] | None:
    """List ordered groups only when the parent dashboard belongs to the authenticated owner."""
    if await session.scalar(select(Dashboard.id).where(Dashboard.id == dashboard_id, Dashboard.owner_id == owner_id)) is None:
        return None
    rows = await session.scalars(select(DashboardGroup).where(DashboardGroup.dashboard_id == dashboard_id).order_by(DashboardGroup.position, DashboardGroup.id))
    return [DashboardGroupRead(id=row.id, dashboard_id=row.dashboard_id, name=row.name, position=row.position) for row in rows.all()]


async def create_group(session: AsyncSession, owner_id: int, dashboard_id: UUID, payload: GroupCreate) -> DashboardGroupRead:
    """Add a group under dashboard revision lock and publish the resulting dashboard revision."""
    dashboard = await _lock_dashboard(session, owner_id, dashboard_id)
    _check_revision(dashboard, payload.expected_revision)
    count = await session.scalar(select(func.count()).select_from(DashboardGroup).where(DashboardGroup.dashboard_id == dashboard_id))
    if count >= MAX_GROUPS_PER_DASHBOARD:
        raise DashboardConflict("group_limit", "Group limit reached", dashboard.revision)
    group = DashboardGroup(dashboard_id=dashboard_id, name=payload.name, position=payload.position)
    session.add(group)
    revision = _bump(dashboard)
    await session.flush()
    await commit_with_replay(session, [make_dashboard_change("dashboard", dashboard_id, revision)])
    return DashboardGroupRead(id=group.id, dashboard_id=dashboard_id, name=group.name, position=group.position)


async def patch_group(session: AsyncSession, owner_id: int, dashboard_id: UUID, group_id: UUID, payload: GroupPatch) -> DashboardGroupRead:
    """Update an owned group with explicit nullable-field semantics and one dashboard revision."""
    dashboard = await _lock_dashboard(session, owner_id, dashboard_id)
    _check_revision(dashboard, payload.expected_revision)
    group = await session.scalar(select(DashboardGroup).where(DashboardGroup.id == group_id, DashboardGroup.dashboard_id == dashboard_id).with_for_update())
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
    await commit_with_replay(session, [make_dashboard_change("dashboard", dashboard_id, revision)])
    return DashboardGroupRead(id=group.id, dashboard_id=dashboard_id, name=group.name, position=group.position)


async def delete_group(session: AsyncSession, owner_id: int, dashboard_id: UUID, group_id: UUID, expected_revision: int) -> None:
    """Delete an empty group only; instances must be moved or explicitly removed first."""
    dashboard = await _lock_dashboard(session, owner_id, dashboard_id)
    _check_revision(dashboard, expected_revision)
    group = await session.scalar(select(DashboardGroup).where(DashboardGroup.id == group_id, DashboardGroup.dashboard_id == dashboard_id).with_for_update())
    if group is None:
        raise DashboardMissing
    if await session.scalar(select(GadgetInstance.id).where(GadgetInstance.group_id == group_id).limit(1)) is not None:
        raise DashboardConflict("group_not_empty", "Move or delete group instances first", dashboard.revision)
    await session.delete(group)
    revision = _bump(dashboard)
    await commit_with_replay(session, [make_dashboard_change("dashboard", dashboard_id, revision)])


async def list_definitions(session: AsyncSession, owner_id: int, limit: int = 200) -> list[GadgetDefinitionRead]:
    """List a bounded owner library page of reusable configuration definitions."""
    if not 1 <= limit <= MAX_DEFINITIONS_PER_OWNER:
        raise ValueError("Definition page limit must be between 1 and 200")
    rows = await session.scalars(select(GadgetDefinition).where(GadgetDefinition.owner_id == owner_id).order_by(GadgetDefinition.created_at, GadgetDefinition.id).limit(limit))
    definitions = list(rows.all())
    source_states = await _definition_source_states(session, definitions)
    return [_definition_read(row, source_states) for row in definitions]


def _definition_read(
    row: GadgetDefinition, source_states: Mapping[UUID, Any]
) -> GadgetDefinitionRead:
    """Project config, descriptor-derived adapter state and current source warnings, never content."""
    return GadgetDefinitionRead.model_validate({"id": row.id, "name": row.name, "revision": row.revision, "renderer": row.renderer,
            "source_ids": [UUID(str(item)) for item in row.source_ids], "scope": row.scope,
            "filters": row.filters, "highlight_rules": row.highlight_rules,
            "config_version": 1, "runtime_state": gadgets.renderer_descriptor(row.renderer).runtime_state,
            "warnings": _definition_warnings(row, source_states)})


async def get_definition(session: AsyncSession, owner_id: int, definition_id: UUID) -> GadgetDefinitionRead | None:
    """Return one owner-only reusable definition or no result for foreign identifiers."""
    row = await session.scalar(select(GadgetDefinition).where(GadgetDefinition.id == definition_id, GadgetDefinition.owner_id == owner_id))
    if row is None:
        return None
    return _definition_read(row, await _definition_source_states(session, [row]))


async def create_definition(session: AsyncSession, owner_id: int, payload: GadgetDefinitionCreate) -> GadgetDefinitionRead:
    """Validate a planned renderer configuration, enforce source lifecycle and quota, then publish it."""
    await _lock_owner_creation_quota(session, owner_id)
    descriptor = gadgets.renderer_descriptor(payload.renderer)
    configuration = gadgets.validate_renderer_configuration(
        payload.renderer,
        GadgetConfiguration(scope=payload.scope, filters=payload.filters, highlight_rules=payload.highlight_rules),
    )
    await _lock_selected_sources(session, payload.source_ids, require_active=True)
    count = await session.scalar(select(func.count()).select_from(GadgetDefinition).where(GadgetDefinition.owner_id == owner_id))
    if count >= MAX_DEFINITIONS_PER_OWNER:
        raise DashboardConflict("definition_limit", "Definition limit reached")
    row = GadgetDefinition(owner_id=owner_id, name=payload.name, renderer=descriptor.id,
        source_ids=[str(item) for item in payload.source_ids], scope=configuration.scope.model_dump(mode="json"),
        filters=configuration.filters.model_dump(mode="json"), highlight_rules=[item.model_dump(mode="json") for item in configuration.highlight_rules])
    session.add(row)
    await session.flush()
    await commit_with_replay(session, [make_dashboard_change("definition", row.id, row.revision)])
    return _definition_read(row, await _definition_source_states(session, [row]))


async def _lock_selected_sources(session: AsyncSession, source_ids: Sequence[UUID], *, require_active: bool) -> None:
    """Lock selected sources in UUID order before definitions or dashboards, then re-read lifecycle."""
    for source_id in sorted(set(source_ids), key=str):
        fence = await sources.lock_source(session, source_id)
        if fence is None or (require_active and fence.status != "active"):
            raise DashboardConflict("source_changed", "A selected source is unavailable")


async def patch_definition(session: AsyncSession, owner_id: int, definition_id: UUID, payload: GadgetDefinitionPatch) -> GadgetDefinitionRead:
    """Patch reusable configuration before locking consuming dashboards, preserving renderer minima."""
    # Source locks precede definition and dashboard locks, so acquire candidate IDs first.
    if "source_ids" in payload.model_fields_set and payload.source_ids is not None:
        await _lock_selected_sources(session, payload.source_ids, require_active=True)
    row = await _lock_definition(session, owner_id, definition_id)
    _check_revision(row, payload.expected_revision)
    if payload.model_fields_set <= {"expected_revision"}:
        raise ValueError("At least one definition field is required")
    if any(getattr(payload, name) is None for name in payload.model_fields_set - {"expected_revision"}):
        raise ValueError("Definition fields cannot be cleared")
    references = list((await session.scalars(select(GadgetInstance.dashboard_id).where(GadgetInstance.definition_id == definition_id).distinct())).all())
    for dashboard_id in sorted(set(references), key=str):
        await _lock_dashboard(session, owner_id, dashboard_id)
    if "renderer" in payload.model_fields_set and payload.renderer != row.renderer and references:
        raise DashboardConflict("renderer_in_use", "Renderer cannot change while referenced", row.revision)
    candidate = {
        "name": payload.name if payload.name is not None else row.name,
        "renderer": payload.renderer if payload.renderer is not None else row.renderer,
        "source_ids": payload.source_ids if payload.source_ids is not None else [UUID(str(item)) for item in row.source_ids],
        "scope": payload.scope if "scope" in payload.model_fields_set else None,
        "filters": payload.filters if "filters" in payload.model_fields_set else None,
        "highlight_rules": payload.highlight_rules if "highlight_rules" in payload.model_fields_set else None,
    }
    config = GadgetConfiguration(
        scope=candidate["scope"] if candidate["scope"] is not None else GadgetScope.model_validate(row.scope),
        filters=candidate["filters"] if candidate["filters"] is not None else GadgetFilters.model_validate(row.filters),
        highlight_rules=candidate["highlight_rules"] if candidate["highlight_rules"] is not None else [HighlightRule.model_validate(item) for item in row.highlight_rules],
    )
    gadgets.validate_renderer_configuration(candidate["renderer"], config)
    row.name = candidate["name"]
    row.renderer = candidate["renderer"]
    row.source_ids = [str(item) for item in candidate["source_ids"]]
    row.scope = config.scope.model_dump(mode="json")
    row.filters = config.filters.model_dump(mode="json")
    row.highlight_rules = [item.model_dump(mode="json") for item in config.highlight_rules]
    revision = _bump(row)
    await commit_with_replay(session, [make_dashboard_change("definition", definition_id, revision)])
    return _definition_read(row, await _definition_source_states(session, [row]))


async def delete_definition(session: AsyncSession, owner_id: int, definition_id: UUID, expected_revision: int) -> None:
    """Delete an unused definition under its lock; preserve references by rejecting in-use deletes."""
    row = await _lock_definition(session, owner_id, definition_id)
    _check_revision(row, expected_revision)
    if await session.scalar(select(GadgetInstance.id).where(GadgetInstance.definition_id == definition_id).limit(1)) is not None:
        raise DashboardConflict("definition_in_use", "Definition is used by a dashboard", row.revision)
    revision = _bump(row)
    await session.delete(row)
    await commit_with_replay(session, [make_dashboard_change("definition", definition_id, revision, deleted=True)])


async def create_instance(session: AsyncSession, owner_id: int, dashboard_id: UUID, payload: InstanceCreate) -> DashboardDetail:
    """Create a cross-checked instance and both default placements under definition-before-dashboard locks."""
    definition = await _lock_definition(session, owner_id, payload.definition_id)
    dashboard = await _lock_dashboard(session, owner_id, dashboard_id)
    _check_revision(dashboard, payload.expected_revision)
    group = await session.scalar(select(DashboardGroup).where(DashboardGroup.id == payload.group_id, DashboardGroup.dashboard_id == dashboard_id))
    if group is None:
        raise DashboardMissing
    count = await session.scalar(select(func.count()).select_from(GadgetInstance).where(GadgetInstance.dashboard_id == dashboard_id))
    if count >= MAX_INSTANCES_PER_DASHBOARD:
        raise DashboardConflict("instance_limit", "Instance limit reached", dashboard.revision)
    instance = GadgetInstance(dashboard_id=dashboard_id, group_id=payload.group_id, definition_id=definition.id, title=payload.title, position=payload.position)
    session.add(instance)
    await session.flush()
    descriptor = gadgets.renderer_descriptor(definition.renderer)
    existing = list((await session.scalars(select(GadgetPlacement).where(GadgetPlacement.dashboard_id == dashboard_id))).all())
    for breakpoint in ("desktop", "mobile"):
        layout_row = await session.get(DashboardLayout, (dashboard_id, breakpoint))
        if layout_row is None:
            layout_row = DashboardLayout(dashboard_id=dashboard_id, breakpoint=breakpoint, columns=20)
            session.add(layout_row)
        current = sorted((item for item in existing if item.breakpoint == breakpoint), key=lambda item: (item.y, item.x, str(item.instance_id)))
        width = descriptor.minimum_width if breakpoint == "desktop" else layout_row.columns
        height = descriptor.minimum_height
        if descriptor.minimum_width > layout_row.columns:
            raise ValueError("Renderer minimum does not fit the configured layout columns")
        # A free rectangle first appears at y=0 or an existing rectangle's bottom; 100 placements
        # and 20 columns cap this row-major candidate scan at 2,020 positions.
        y_candidates = sorted({0, *(item.y + item.h for item in current)})
        position = next((
            (x, y)
            for y in y_candidates
            if y + height <= 100_000
            for x in range(layout_row.columns - width + 1)
            if not any(
                x < item.x + item.w and item.x < x + width
                and y < item.y + item.h and item.y < y + height
                for item in current
            )
        ), None)
        if position is None:
            raise ValueError("No free layout position remains")
        x, y = position
        session.add(GadgetPlacement(dashboard_id=dashboard_id, breakpoint=breakpoint, instance_id=instance.id, x=x, y=y, w=width, h=height))
    revision = _bump(dashboard)
    await commit_with_replay(session, [make_dashboard_change("dashboard", dashboard_id, revision)])
    return await _dashboard_read(session, dashboard)


async def patch_instance(session: AsyncSession, owner_id: int, dashboard_id: UUID, instance_id: UUID, payload: InstancePatch) -> DashboardDetail:
    """Edit local title/group/order under the dashboard revision while preserving layout geometry."""
    instance_ref = await session.scalar(select(GadgetInstance.definition_id).where(GadgetInstance.id == instance_id, GadgetInstance.dashboard_id == dashboard_id))
    if instance_ref is None:
        raise DashboardMissing
    await _lock_definition(session, owner_id, instance_ref)
    dashboard = await _lock_dashboard(session, owner_id, dashboard_id)
    _check_revision(dashboard, payload.expected_revision)
    instance = await session.scalar(select(GadgetInstance).where(GadgetInstance.id == instance_id, GadgetInstance.dashboard_id == dashboard_id).with_for_update())
    if instance is None:
        raise DashboardMissing
    if not payload.model_fields_set - {"expected_revision"}:
        raise ValueError("At least one instance field is required")
    if "title" in payload.model_fields_set:
        instance.title = payload.title
    if "group_id" in payload.model_fields_set:
        if payload.group_id is None:
            raise ValueError("group_id cannot be cleared")
        if await session.scalar(select(DashboardGroup.id).where(DashboardGroup.id == payload.group_id, DashboardGroup.dashboard_id == dashboard_id)) is None:
            raise DashboardMissing
        instance.group_id = payload.group_id
    if "position" in payload.model_fields_set:
        if payload.position is None:
            raise ValueError("position cannot be cleared")
        instance.position = payload.position
    revision = _bump(dashboard)
    await commit_with_replay(session, [make_dashboard_change("dashboard", dashboard_id, revision)])
    return await _dashboard_read(session, dashboard)


async def delete_instance(session: AsyncSession, owner_id: int, dashboard_id: UUID, instance_id: UUID, expected_revision: int) -> DashboardDetail:
    """Remove an instance and both placements while retaining its reusable definition."""
    definition_id = await session.scalar(select(GadgetInstance.definition_id).where(GadgetInstance.id == instance_id, GadgetInstance.dashboard_id == dashboard_id))
    if definition_id is None:
        raise DashboardMissing
    await _lock_definition(session, owner_id, definition_id)
    dashboard = await _lock_dashboard(session, owner_id, dashboard_id)
    _check_revision(dashboard, expected_revision)
    await session.execute(delete(GadgetPlacement).where(GadgetPlacement.dashboard_id == dashboard_id, GadgetPlacement.instance_id == instance_id))
    instance = await session.scalar(select(GadgetInstance).where(GadgetInstance.id == instance_id, GadgetInstance.dashboard_id == dashboard_id).with_for_update())
    if instance is None:
        raise DashboardMissing
    await session.delete(instance)
    revision = _bump(dashboard)
    await commit_with_replay(session, [make_dashboard_change("dashboard", dashboard_id, revision)])
    return await _dashboard_read(session, dashboard)


async def replace_layout(session: AsyncSession, owner_id: int, dashboard_id: UUID, payload: LayoutReplace) -> DashboardDetail:
    """Replace only the selected breakpoint after exact membership, minima, bounds, and overlap checks.

    An exact geometry/column resave returns current detail without revision or replay changes.
    """
    dashboard = await _lock_dashboard(session, owner_id, dashboard_id)
    _check_revision(dashboard, payload.expected_revision)
    instances, minimums, _ = await _instance_minima(session, dashboard_id)
    item_ids = {item.instance_id for item in payload.items}
    if item_ids != set(minimums):
        raise ValueError("Layout must include every dashboard instance exactly once")
    layouts.validate_layout(payload.items, payload.columns, minimums)
    layout_row = await session.get(DashboardLayout, (dashboard_id, payload.breakpoint))
    if layout_row is None:
        layout_row = DashboardLayout(dashboard_id=dashboard_id, breakpoint=payload.breakpoint, columns=payload.columns)
        session.add(layout_row)
    unchanged = layout_row.columns == payload.columns
    if unchanged:
        current = list((await session.scalars(select(GadgetPlacement).where(GadgetPlacement.dashboard_id == dashboard_id, GadgetPlacement.breakpoint == payload.breakpoint))).all())
        current_by_id = {row.instance_id: (row.x, row.y, row.w, row.h) for row in current}
        unchanged = current_by_id == {item.instance_id: (item.x, item.y, item.w, item.h) for item in payload.items}
    if unchanged:
        return await _dashboard_read(session, dashboard)
    layout_row.columns = payload.columns
    await session.execute(delete(GadgetPlacement).where(GadgetPlacement.dashboard_id == dashboard_id, GadgetPlacement.breakpoint == payload.breakpoint))
    session.add_all([GadgetPlacement(dashboard_id=dashboard_id, breakpoint=payload.breakpoint, instance_id=item.instance_id, x=item.x, y=item.y, w=item.w, h=item.h) for item in payload.items])
    revision = _bump(dashboard)
    await commit_with_replay(session, [make_dashboard_change("dashboard", dashboard_id, revision)])
    return await _dashboard_read(session, dashboard)


def renderer_reads() -> list[RendererRead]:
    """Project static renderer state and geometry without asserting runtime/provider acceptance."""
    return [RendererRead.model_validate({"id": row.id, "config_version": row.config_version, "minimum_width": row.minimum_width,
             "minimum_height": row.minimum_height, "runtime_state": row.runtime_state,
             "capability_keys": list(row.capability_keys)}) for row in gadgets.RENDERERS]


async def list_gadget_sources(session: AsyncSession, limit: int, cursor: str | None) -> GadgetSourceSelectionPage:
    """Return the source owner's bounded metadata-only selection page."""
    return await sources.list_gadget_sources(session, limit, cursor)


def preset_catalog() -> list[DashboardPresetRead]:
    """Expose the ten stable source-free preset identities and renderer slots."""
    return [DashboardPresetRead.model_validate({"id": item.id, "label": item.label, "family": item.family,
             "slots": [{"slot_id": slot.slot_id, "renderer": slot.renderer} for slot in item.slots]})
            for item in gadgets.PRESETS]


async def preview_preset(session: AsyncSession, owner_id: int, preset_id: str, payload: PresetPreviewRequest) -> PresetPreviewRead:
    """Resolve an explicit owner source selection into a canonical, nonpersistent preview."""
    preset = gadgets.dashboard_preset(preset_id)
    gadgets.validate_preset_slot_sources(preset_id, payload.slot_sources)
    selected = sorted({source_id for values in payload.slot_sources.values() for source_id in values}, key=str)
    states: dict[UUID, Any] = {}
    for offset in range(0, len(selected), 32):
        states.update(await _source_states(session, selected[offset : offset + 32]))
    target_revision = None
    if payload.target_dashboard_id:
        target = await session.scalar(select(Dashboard).where(Dashboard.id == payload.target_dashboard_id, Dashboard.owner_id == owner_id))
        if target is None:
            raise DashboardMissing
        target_revision = target.revision
    slots = []
    for slot in preset.slots:
        source_ids = list(payload.slot_sources.get(slot.slot_id, []))
        warnings = _renderer_warnings(slot.renderer)
        if not source_ids:
            warnings.append({"code": "missing_source", "setup_group": "data_sources"})
        resolved = []
        for source_id in source_ids:
            source = states.get(source_id)
            if source is None or source.status != "active":
                warnings.append({"code": "source_unavailable", "source_id": str(source_id), "setup_group": "data_sources"})
            resolved.append({"id": source_id, "generation": source.generation if source else None, "status": source.status if source else "missing"})
        slots.append({"slot_id": slot.slot_id, "renderer": slot.renderer, "source_ids": source_ids,
                      "scope": {}, "filters": {"keywords": [], "exclude_keywords": [], "limit": 25},
                      "highlight_rules": [], "sources": resolved, "warnings": warnings})
    minimums = {
        uuid5(NAMESPACE_URL, f"bbd-os-dashboard-preset:{preset_id}:{slot['slot_id']}"):
        (gadgets.renderer_descriptor(slot["renderer"]).minimum_width,
         gadgets.renderer_descriptor(slot["renderer"]).minimum_height)
        for slot in slots
    }
    desktop = layouts.default_desktop_layout(minimums)
    mobile = layouts.default_mobile_layout(minimums)
    slot_ids_by_uuid = {
        uuid5(NAMESPACE_URL, f"bbd-os-dashboard-preset:{preset_id}:{slot['slot_id']}"): slot["slot_id"]
        for slot in slots
    }
    proposed = {"template_version": 1, "preset_id": preset_id, "name": preset.label,
                "slots": slots, "target_dashboard_id": payload.target_dashboard_id,
                "target_revision": target_revision,
                "layouts": {
                    "desktop": {"columns": 20, "items": [{"slot_id": slot_ids_by_uuid[item.instance_id], "x": item.x, "y": item.y, "w": item.w, "h": item.h} for item in desktop]},
                    "mobile": {"columns": 20, "items": [{"slot_id": slot_ids_by_uuid[item.instance_id], "x": item.x, "y": item.y, "w": item.w, "h": item.h} for item in mobile]},
                }}
    canonical = json.dumps(proposed, sort_keys=True, separators=(",", ":"), default=str).encode()
    return PresetPreviewRead.model_validate({**proposed, "preview_fingerprint": hashlib.sha256(canonical).hexdigest()})


async def apply_preset(session: AsyncSession, owner_id: int, preset_id: str, payload: PresetApplyRequest) -> DashboardDetail:
    """Recompute and fingerprint preset state under ordered source/dashboard locks before atomic apply."""
    await _lock_owner_creation_quota(session, owner_id)
    gadgets.dashboard_preset(preset_id)
    await _lock_selected_sources(session, [source_id for values in payload.slot_sources.values() for source_id in values], require_active=True)
    target_dashboard_id = payload.target_dashboard_id
    if payload.mode == "replace":
        dashboard = await _lock_dashboard(session, owner_id, target_dashboard_id)
        _check_revision(dashboard, payload.expected_revision)
    else:
        dashboard = None
    current = (await preview_preset(session, owner_id, preset_id, PresetPreviewRequest(
        slot_sources=payload.slot_sources, target_dashboard_id=target_dashboard_id
    ))).model_dump(mode="python")
    if current["preview_fingerprint"] != payload.preview_fingerprint:
        raise DashboardConflict("preview_changed", "Preset preview changed; review it again", dashboard.revision if dashboard else None)
    if payload.mode == "create":
        count = await session.scalar(select(func.count()).select_from(Dashboard).where(Dashboard.owner_id == owner_id))
        if count >= MAX_DASHBOARDS_PER_OWNER:
            raise DashboardConflict("dashboard_limit", "Dashboard limit reached")
        dashboard = Dashboard(owner_id=owner_id, name=payload.name or current["name"])
        session.add(dashboard)
        await session.flush()
        dashboard_revision = dashboard.revision
        session.add_all([DashboardLayout(dashboard_id=dashboard.id, breakpoint=bp, columns=20) for bp in ("desktop", "mobile")])
    else:
        dashboard_revision = _bump(dashboard)
        if payload.name is not None:
            dashboard.name = payload.name
        await session.execute(delete(DashboardGroup).where(DashboardGroup.dashboard_id == dashboard.id))
        await session.execute(delete(DashboardLayout).where(DashboardLayout.dashboard_id == dashboard.id))
        session.add_all([DashboardLayout(dashboard_id=dashboard.id, breakpoint=bp, columns=20) for bp in ("desktop", "mobile")])
    definition_count = await session.scalar(select(func.count()).select_from(GadgetDefinition).where(GadgetDefinition.owner_id == owner_id))
    if definition_count + len(current["slots"]) > MAX_DEFINITIONS_PER_OWNER:
        raise DashboardConflict("definition_limit", "Definition limit reached", dashboard_revision)
    group = DashboardGroup(dashboard_id=dashboard.id, name=current["name"], position=0)
    session.add(group)
    await session.flush()
    created_instances: list[tuple[GadgetInstance, GadgetDefinition, Any]] = []
    for index, slot in enumerate(current["slots"]):
        descriptor = gadgets.renderer_descriptor(slot["renderer"])
        definition = GadgetDefinition(owner_id=owner_id, name=f"{current['name']} · {slot['slot_id']}", renderer=slot["renderer"],
            source_ids=[str(item) for item in slot["source_ids"]], scope={}, filters={"keywords": [], "exclude_keywords": [], "limit": 25}, highlight_rules=[])
        session.add(definition)
        await session.flush()
        instance = GadgetInstance(dashboard_id=dashboard.id, group_id=group.id, definition_id=definition.id, title=None, position=index)
        session.add(instance)
        await session.flush()
        created_instances.append((instance, definition, descriptor))
    minimums = {instance.id: (descriptor.minimum_width, descriptor.minimum_height) for instance, _, descriptor in created_instances}
    desktop = layouts.default_desktop_layout(minimums)
    mobile = layouts.default_mobile_layout(minimums)
    for breakpoint, items in (("desktop", desktop), ("mobile", mobile)):
        session.add_all([
            GadgetPlacement(dashboard_id=dashboard.id, breakpoint=breakpoint,
                            instance_id=item.instance_id, x=item.x, y=item.y, w=item.w, h=item.h)
            for item in items
        ])
    drafts = [make_dashboard_change("dashboard", dashboard.id, dashboard_revision)]
    drafts.extend(make_dashboard_change("definition", definition.id, definition.revision) for _, definition, _descriptor in created_instances)
    await commit_with_replay(session, drafts)
    return await _dashboard_read(session, dashboard)


# Stable seams for P10 automation: daily context and briefs are consumed through this module only.
from modules.dashboard.briefs import (  # noqa: E402
    BriefEmpty,
    BriefSlotOwned,
    BriefUnavailable,
    claim_brief_slot,
    generate_brief,
    latest_brief,
    list_briefs,
    read_schedule,
    read_slot_owner,
    release_brief_slot,
)
from modules.dashboard.context import build_daily_context  # noqa: E402
from modules.dashboard.daily_schemas import BriefRead, DailyContext  # noqa: E402

__all__ = [
    "BriefEmpty", "BriefRead", "BriefUnavailable", "DailyContext", "build_daily_context",
    "BriefSlotOwned", "claim_brief_slot", "generate_brief", "latest_brief", "list_briefs", "read_schedule",
    "read_slot_owner", "release_brief_slot",
]
