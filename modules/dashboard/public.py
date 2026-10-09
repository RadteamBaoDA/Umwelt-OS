"""Owner-facing dashboard queries and atomic configuration mutations.

Routes use this module instead of importing dashboard persistence models. Every
mutation derives ownership from the authenticated session, serializes revision
changes under the dashboard/definition row locks, and publishes its revision
through the shared replay transaction.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from sqlalchemy import ColumnElement, delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.expression import Exists

from core.realtime import commit_with_replay, make_dashboard_change
from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, Scope, WorkspaceContext
from modules.dashboard import briefs, gadgets, layouts
from modules.dashboard.daily_schemas import (
    BriefExportFence,
    BriefExportPage,
    BriefExportValidation,
    BriefSchedule,
    BriefScheduleExport,
    BriefScheduleExportFence,
    BriefScheduleExportPage,
    BriefScheduleExportValidation,
    DailyBriefExport,
)
from modules.dashboard.models import (
    BriefSchedule as BriefScheduleRow,
)
from modules.dashboard.models import (
    DailyBrief,
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
    MAX_RULES_PER_DEFINITION,
    DashboardCreate,
    DashboardDetail,
    DashboardExportFence,
    DashboardExportPage,
    DashboardExportValidation,
    DashboardGroupRead,
    DashboardHighlightRead,
    DashboardPatch,
    DashboardPresetRead,
    DashboardSummary,
    GadgetConfiguration,
    GadgetDefinitionCreate,
    GadgetDefinitionExport,
    GadgetDefinitionExportFence,
    GadgetDefinitionExportPage,
    GadgetDefinitionExportValidation,
    GadgetDefinitionPatch,
    GadgetDefinitionRead,
    GadgetDefinitionUsageRead,
    GadgetFilters,
    GadgetScope,
    GroupCreate,
    GroupPatch,
    HighlightPreviewRead,
    HighlightPreviewRequest,
    HighlightPreviewRuleRead,
    HighlightRule,
    InstanceCreate,
    InstancePatch,
    LayoutReplace,
    PresetApplyRequest,
    PresetPreviewRead,
    PresetPreviewRequest,
    RendererRead,
)
from modules.settings import public as settings_public
from modules.sources import public as sources
from modules.sources.schemas import GadgetSourceSelectionPage

MAX_REVISION = 9_007_199_254_740_991
DASHBOARD_QUOTA_LOCK_NAMESPACE = 4_603_202
MAX_HIGHLIGHT_NOTIFICATIONS_PER_TRANSACTION = 100
# A document can match every configured rule; keep a whole page below the insertion cap so advancing
# its cursor never drops an eligible rule/version notification.
HIGHLIGHT_SCAN_PAGE_LIMIT = max(
    1, MAX_HIGHLIGHT_NOTIFICATIONS_PER_TRANSACTION // MAX_RULES_PER_DEFINITION,
)
HIGHLIGHT_MATCHES_PER_PAGE_MAX = HIGHLIGHT_SCAN_PAGE_LIMIT * MAX_RULES_PER_DEFINITION
PREVIEW_PAGE_SIZE = 100
PREVIEW_MATCH_TIMEOUT_SECONDS = 5
PREVIEW_MAX_PAGES = 2  # 200 current versions at most
PREVIEW_MAX_MATCHES = 100


def _owner_tzinfo(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return UTC


def _actor(scope: Scope) -> int:
    """Return the admitted owner actor used by dashboard-owned rows."""
    return scope.user_id if isinstance(scope, WorkspaceContext) else scope.actor_user_id


async def _admit(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool,
    lock: bool = False, expected: AccessFence | None = None,
) -> AccessFence:
    """Admit owner scope before reading or locking dashboard-owned data."""
    if isinstance(scope, WorkspaceContext) and scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")
    if type(multi_workspace_enabled) is not bool:
        raise TypeError("The configured multi-workspace feature flag must be a boolean")
    if lock:
        return await workspaces.lock_access_fence(
            session, scope=scope, expected=expected, multi_workspace_enabled=multi_workspace_enabled,
        )
    return await workspaces.read_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )


async def evaluate_gadget_highlights(
    session: AsyncSession, definition_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
    emit_notifications: bool = False,
) -> list[DashboardHighlightRead]:
    """Evaluate current evidence and durably page scans with at most 96 rule/version matches.

    Scheduled pages hold at most three documents and the schema caps each definition at 32 rules.
    The resulting 3-by-32 ceiling keeps every matching notification in the same cursor transaction.
    Highlight notification titles carry exact private Document/version provenance; Notifications
    rechecks the selected current version and keeps that provenance out of its public DTO.
    """
    from modules.dashboard.highlights import compile_rules, match_compiled, notification_allowed
    from modules.dashboard.models import GadgetHighlightProgress, GadgetHighlightSuppression
    from modules.dashboard.schemas import HighlightRule
    from modules.knowledge.documents import public as documents
    from modules.notifications.public import NotificationEmit, NotificationEvidence, emit

    access_fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        lock=emit_notifications,
    )
    owner_id = _actor(scope)
    if emit_notifications:
        # Snapshot the owner definition and scan position without holding either row lock.
        # Sources and Documents must be settled before the Definition/Progress locks below.
        definition = await session.scalar(select(GadgetDefinition).where(
            GadgetDefinition.id == definition_id,
            GadgetDefinition.workspace_id == scope.workspace_id,
            GadgetDefinition.owner_id == owner_id,
        ).execution_options(populate_existing=True))
        if definition is None:
            raise DashboardMissing
        definition_revision = definition.revision
        source_ids = tuple(UUID(str(value)) for value in definition.source_ids[:32])
        selected_sources = await sources.get_gadget_sources(
            session, source_ids, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        ) if source_ids else []
        selected_source_snapshot = {item.id: item for item in selected_sources}
        scan_source_ids = tuple(item.id for item in selected_sources if item.status == "active")
        raw_rules = json.loads(json.dumps(definition.highlight_rules, sort_keys=True))
        raw_scope = json.loads(json.dumps(definition.scope, sort_keys=True))
        rules = [HighlightRule.model_validate(rule) for rule in raw_rules]
        if len(rules) > MAX_RULES_PER_DEFINITION:
            raise ValueError("Highlight rule count exceeds the validated definition bound")
        raw_item_scope = raw_scope.get("source_item_ids", []) if isinstance(raw_scope, dict) else []
        item_scope = {str(value) for value in raw_item_scope} if isinstance(raw_item_scope, list) else set()
        if not scan_source_ids or not rules:
            return []
        # Read-only topic resolution happens before any definition/progress lock is taken.
        compiled = compile_rules(rules, await _rule_topic_terms(
            session, rules, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        ))
        definition_snapshot = json.dumps(
            {"revision": definition_revision, "source_ids": [str(value) for value in source_ids],
             "scope": raw_scope, "rules": raw_rules},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )
        rules_fingerprint = hashlib.sha256(json.dumps(
            {"source_ids": [str(value) for value in source_ids], "scope": sorted(item_scope),
             "rules": [rule.model_dump(mode="json", exclude_defaults=True) for rule in rules]},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")).hexdigest()

        progress_snapshot_row = await session.scalar(select(GadgetHighlightProgress).where(
            GadgetHighlightProgress.definition_id == definition.id,
        ).execution_options(populate_existing=True))
        progress_snapshot = None if progress_snapshot_row is None else (
            progress_snapshot_row.definition_revision, progress_snapshot_row.rules_fingerprint,
            progress_snapshot_row.cursor_created_at, progress_snapshot_row.cursor_version_id,
        )
        progress_is_current = progress_snapshot is not None and (
            progress_snapshot[0] == definition_revision
            and progress_snapshot[1] == rules_fingerprint
        )
        initial_cursor_created_at = progress_snapshot[2] if progress_is_current else None
        initial_cursor_version_id = progress_snapshot[3] if progress_is_current else None
        page = await documents.list_gadget_highlight_projection_page(
            session, source_ids=scan_source_ids, limit=HIGHLIGHT_SCAN_PAGE_LIMIT,
            cursor_created_at=initial_cursor_created_at,
            cursor_version_id=initial_cursor_version_id,
            scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        if len(page.items) * len(rules) > HIGHLIGHT_MATCHES_PER_PAGE_MAX:
            raise RuntimeError("Highlight scan page exceeds its notification coverage bound")

        # The page may contain only a subset of selected Sources. Lock all selected
        # Source identities first, then let Documents lock and verify selected versions.
        source_fences = {}
        for source_id in sorted(scan_source_ids, key=str):
            source_fences[source_id] = await sources.lock_source(
                session, source_id, scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
                expected_access_fence=access_fence,
            )
        if any(
            fence is None or fence.status != "active"
            or fence.generation != selected_source_snapshot[source_id].generation
            or fence.local_only != selected_source_snapshot[source_id].local_only
            for source_id, fence in source_fences.items()
        ):
            raise HTTPException(status_code=409, detail="Highlight source selection changed")
        if page.selection_fences and not await documents.validate_gadget_document_selection_fences(
            session, tuple(page.selection_fences), lock_rows=True,
            max_documents=HIGHLIGHT_SCAN_PAGE_LIMIT,
            scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        ):
            raise HTTPException(status_code=409, detail="Dashboard highlight evidence changed during evaluation")

        definition = await session.scalar(select(GadgetDefinition).where(
            GadgetDefinition.id == definition_id,
            GadgetDefinition.workspace_id == scope.workspace_id,
            GadgetDefinition.owner_id == owner_id,
        ).with_for_update().execution_options(populate_existing=True))
        if definition is None:
            raise DashboardMissing
        current_snapshot = json.dumps(
            {"revision": definition.revision, "source_ids": definition.source_ids,
             "scope": definition.scope, "rules": definition.highlight_rules},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )
        if current_snapshot != definition_snapshot:
            raise HTTPException(status_code=409, detail="Highlight definition changed during evaluation")

        progress = await session.scalar(select(GadgetHighlightProgress).where(
            GadgetHighlightProgress.definition_id == definition.id,
        ).with_for_update().execution_options(populate_existing=True))
        current_progress_snapshot = None if progress is None else (
            progress.definition_revision, progress.rules_fingerprint,
            progress.cursor_created_at, progress.cursor_version_id,
        )
        if current_progress_snapshot != progress_snapshot:
            raise HTTPException(status_code=409, detail="Highlight scan progress changed during evaluation")
        if progress is None:
            progress = GadgetHighlightProgress(
                definition_id=definition.id, definition_revision=definition.revision,
                rules_fingerprint=rules_fingerprint, rule_last_notified={},
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
            progress.rule_last_notified = {}
            await session.execute(delete(GadgetHighlightSuppression).where(
                GadgetHighlightSuppression.definition_id == definition.id,
            ))
        progress.cursor_created_at = page.cursor_created_at if page.has_more else None
        progress.cursor_version_id = page.cursor_version_id if page.has_more else None

        # Delivery policy (T6b): reads only the already-locked progress row plus an unlocked
        # preferences SELECT, so the definition -> progress lock order is unchanged.
        now = datetime.now(UTC)
        rules_by_id = {rule.id: rule for rule in rules}
        last_notified = {
            key: datetime.fromisoformat(value) for key, value in progress.rule_last_notified.items()
            if UUID(key) in rules_by_id
        }
        tz: tzinfo = UTC
        if any(rule.quiet_start for rule in rules):
            tz = _owner_tzinfo((await settings_public.read_owner_preferences(
                session, actor_user_id=owner_id, multi_workspace_enabled=multi_workspace_enabled,
            )).timezone)
        found = [
            (item, match) for item in page.items
            if not item_scope or str(item.document_id) in item_scope
            for match in match_compiled(item.excerpt, compiled, source_id=item.source_id)
        ]

        def key_of(item: Any, match: Any) -> str:
            return (
                f"highlight:{definition.id}:{definition.revision}:"
                f"{rules_fingerprint}:{match.rule_id}:{item.document_version_id}"
            )

        # BM-34: quiet hours/cooldown/expiry SUPPRESS. The decision is stored so a later rescan of
        # the same version cannot deliver it. Writes happen under the definition lock held above.
        suppressed_keys: set[str] = set()
        if found:
            suppressed_keys = set((await session.execute(
                select(GadgetHighlightSuppression.dedupe_key).where(
                    GadgetHighlightSuppression.definition_id == definition.id,
                    GadgetHighlightSuppression.dedupe_key.in_([key_of(i, m) for i, m in found]),
                )
            )).scalars())
        matches: list[DashboardHighlightRead] = []
        for item, match in found:
            dedupe_key = key_of(item, match)
            deliver = False
            if match.notify and dedupe_key not in suppressed_keys:
                deliver = notification_allowed(
                    rules_by_id[match.rule_id], now, tz, last_notified.get(str(match.rule_id)),
                )
                expires = rules_by_id[match.rule_id].expires_at
                # ponytail: quiet/cooldown rows grow unbounded; upgrade is version-based pruning (R1 option 2).
                if not deliver and not (expires is not None and now >= expires):  # expiry is permanent until edit
                    # Keyed by the locked, scoped definition.id above; the table has no workspace column.
                    await session.execute(pg_insert(GadgetHighlightSuppression).values(
                        definition_id=definition.id, dedupe_key=dedupe_key,
                    ).on_conflict_do_nothing())
            matches.append(DashboardHighlightRead(
                document_id=item.document_id, document_version_id=item.document_version_id,
                source_id=item.source_id, title=item.title, observed_at=item.observed_at,
                rule_id=match.rule_id, matched_keywords=list(match.matched_keywords),
                severity=match.severity, notify=match.notify, reason=match.reason,
            ))
            if deliver and await emit(session, NotificationEmit(
                dedupe_key=dedupe_key,
                kind="dashboard_highlight", title=item.title[:300],
                body=match.reason[:1000],
                params={"severity": match.severity, "definition_id": str(definition.id),
                        "definition_revision": definition.revision},
                link="/dashboard",
            ), evidence=NotificationEvidence(
                document_id=item.document_id, document_version_id=item.document_version_id,
            ), scope=scope, multi_workspace_enabled=multi_workspace_enabled):
                last_notified[str(match.rule_id)] = now
        progress.rule_last_notified = {key: value.isoformat() for key, value in last_notified.items()}
        # Progress and notifications commit together under the original access fence.
        await commit_with_replay(
            session, [], scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
        )
        return matches[:100]

    definition_read = await get_definition(session, definition_id, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled)
    if definition_read is None:
        raise DashboardMissing
    if definition_read.renderer not in {"highlights", "watch_rules"}:
        raise ValueError("Renderer does not support highlight evaluation")
    requested_source_ids = tuple(definition_read.source_ids[:32])
    if not requested_source_ids:
        return []
    selected_sources = await sources.get_gadget_sources(
        session, requested_source_ids, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    source_ids = tuple(item.id for item in selected_sources if item.status == "active")
    if not source_ids:
        return []
    # "Not relevant" scope: hides versions from dashboard feed gadgets and this highlights view only
    # (include_dismissed stays False). Notifications, brief, search and chat are unchanged.
    projection_page = await documents.list_gadget_document_projections(
        session, source_ids=source_ids, limit=100, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    rules = [HighlightRule.model_validate(rule) for rule in definition_read.highlight_rules]
    raw_item_scope = definition_read.scope.get("source_item_ids", [])
    item_scope = {str(value) for value in raw_item_scope} if isinstance(raw_item_scope, list) else set()
    compiled = compile_rules(rules, await _rule_topic_terms(
        session, rules, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    ))
    matches = []
    scoped = [item for item in projection_page.items if not item_scope or str(item.document_id) in item_scope]
    # Regex work is CPU-bound; keep it off the API event loop.
    per_item = await asyncio.to_thread(_match_items, scoped, compiled)
    for item, item_matches in zip(scoped, per_item, strict=True):
        for match in item_matches:
            matches.append(DashboardHighlightRead(
                document_id=item.document_id, document_version_id=item.document_version_id,
                source_id=item.source_id, title=item.title, observed_at=item.observed_at,
                rule_id=match.rule_id, matched_keywords=list(match.matched_keywords),
                severity=match.severity, notify=match.notify, reason=match.reason,
            ))
    return matches[:100]


async def _rule_topic_terms(
    session: AsyncSession, rules: Sequence[HighlightRule], *, scope: Scope, multi_workspace_enabled: bool,
) -> dict[UUID, list[str]]:
    """Resolve the actor's live, active topics referenced by rules in this workspace (read-only)."""
    from modules.news import public as news
    topic_ids = list(dict.fromkeys(topic for rule in rules for topic in rule.topic_ids))
    return await news.resolve_topic_terms(
        session, topic_ids, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )


class HighlightRuleError(ValueError):
    """A rule breaks a scope or ownership invariant; ``code`` lets the web client localize it."""

    def __init__(self, code: str, message: str) -> None:
        """Keep the stable machine code beside the English message."""
        super().__init__(message)
        self.code = code


async def validate_highlight_rules(
    session: AsyncSession, source_ids: Sequence[UUID], rules: Sequence[HighlightRule],
    *, scope: Scope, known_topic_ids: Collection[UUID] | None = (),
) -> None:
    """Trust-boundary check: rule sources stay inside the definition scope; new topics must be the owner's.

    Topic ids in ``known_topic_ids`` (already stored on the definition) are not re-checked, so a topic
    deleted later never blocks edits; ``None`` skips the topic check (preview reports them unresolved).
    """
    from modules.news import public as news
    allowed = set(source_ids)
    for rule in rules:
        if not set(rule.source_ids) <= allowed or not set(rule.exclude_source_ids) <= allowed:
            raise HighlightRuleError("rule_sources_not_subset", "Rule sources must be a subset of the definition sources")
        if allowed and allowed <= set(rule.exclude_source_ids):
            raise HighlightRuleError("rule_excludes_all_sources", "A rule cannot exclude every definition source")
    if known_topic_ids is None:
        return
    topic_ids = list(dict.fromkeys(
        topic for rule in rules for topic in rule.topic_ids if topic not in known_topic_ids
    ))
    if topic_ids and set(topic_ids) - await news.live_topic_ids(session, topic_ids, scope=scope):
        raise HighlightRuleError("rule_topic_unknown", "Rule topic_ids must reference your existing topics")


def _match_items(items: Sequence[Any], compiled: Sequence[Any]) -> list[list[Any]]:
    """Match every item against pre-compiled rules (pure CPU; safe to run in a thread)."""
    from modules.dashboard.highlights import match_compiled
    return [match_compiled(item.excerpt, compiled, source_id=item.source_id) for item in items]


async def preview_highlights(
    session: AsyncSession, payload: HighlightPreviewRequest, *, scope: Scope, multi_workspace_enabled: bool,
) -> HighlightPreviewRead:
    """Dry-run draft rules over recent current evidence: read-only, bounded, no notifications, no egress.

    Uses the same projection reads as the display path, so purged, paused or superseded content is
    never scanned. Nothing is written or committed and ``emit`` is never imported.
    """
    from modules.dashboard.highlights import compile_rules
    from modules.knowledge.documents import public as documents
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    await validate_highlight_rules(session, payload.source_ids, payload.rules, scope=scope, known_topic_ids=None)
    since = datetime.now(UTC) - timedelta(days=payload.days)
    # Only active sources visible in this workspace are scanned; foreign or paused ids drop out.
    selected = await sources.get_gadget_sources(
        session, tuple(payload.source_ids), scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    ) if payload.source_ids else []
    source_ids = tuple(item.id for item in selected if item.status == "active")
    topic_terms = await _rule_topic_terms(
        session, payload.rules, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    compiled = compile_rules(payload.rules, topic_terms)
    item_scope = {str(value) for value in payload.source_item_ids}
    counts = {rule.id: 0 for rule in payload.rules}
    matches: list[DashboardHighlightRead] = []
    scanned = 0
    cursor: str | None = None
    truncated = False
    for _ in range(PREVIEW_MAX_PAGES):
        page = await documents.list_gadget_document_projections(
            session, source_ids=source_ids, limit=PREVIEW_PAGE_SIZE,
            cursor=cursor, since=since, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        items = [item for item in page.items if not item_scope or str(item.document_id) in item_scope]
        scanned += len(items)
        # Regex work is CPU-bound; keep it off the event loop.
        try:
            per_item = await asyncio.wait_for(
                asyncio.to_thread(_match_items, items, compiled), PREVIEW_MATCH_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            raise HTTPException(
                status_code=504, detail={"code": "preview_timeout", "message": "Preview took too long", "details": {}},
            ) from exc
        for item, item_matches in zip(items, per_item, strict=True):
            for match in item_matches:
                counts[match.rule_id] += 1
                if len(matches) < PREVIEW_MAX_MATCHES:
                    matches.append(DashboardHighlightRead(
                        document_id=item.document_id, document_version_id=item.document_version_id,
                        source_id=item.source_id, title=item.title, observed_at=item.observed_at,
                        rule_id=match.rule_id, matched_keywords=list(match.matched_keywords),
                        severity=match.severity, notify=match.notify, reason=match.reason,
                    ))
        cursor = page.next_cursor
        if cursor is None:
            break
    else:
        truncated = True
    return HighlightPreviewRead(
        window_days=payload.days, scanned=scanned, truncated=truncated, matches=matches,
        rules=[HighlightPreviewRuleRead(
            rule_id=rule.id, match_count=counts[rule.id],
            unresolved_topic_ids=[topic for topic in rule.topic_ids if topic not in topic_terms],
        ) for rule in payload.rules],
    )


async def definition_usage(
    session: AsyncSession, definition_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> list[GadgetDefinitionUsageRead] | None:
    """List the actor's dashboards that place this definition (and so evaluate its rules)."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if await session.scalar(select(GadgetDefinition.id).where(
        GadgetDefinition.id == definition_id, GadgetDefinition.workspace_id == scope.workspace_id,
        GadgetDefinition.owner_id == _actor(scope),
    )) is None:
        return None
    rows = (await session.execute(
        select(Dashboard.id, Dashboard.name, func.count(GadgetInstance.id))
        .join(GadgetInstance, GadgetInstance.dashboard_id == Dashboard.id)
        .where(GadgetInstance.definition_id == definition_id, Dashboard.workspace_id == scope.workspace_id,
               Dashboard.owner_id == _actor(scope))
        .group_by(Dashboard.id, Dashboard.name)
        .order_by(Dashboard.name, Dashboard.id).limit(MAX_DASHBOARDS_PER_OWNER)
    )).all()
    return [GadgetDefinitionUsageRead(dashboard_id=r[0], name=r[1], instance_count=r[2]) for r in rows]


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
    session: AsyncSession, dashboard_id: UUID, *, scope: Scope,
) -> Dashboard:
    """Lock one owner-scoped dashboard row and refresh it before revision checks."""
    row = await session.scalar(
        select(Dashboard)
        .where(Dashboard.id == dashboard_id, Dashboard.workspace_id == scope.workspace_id,
               Dashboard.owner_id == _actor(scope))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise DashboardMissing
    return row


async def _lock_definition(
    session: AsyncSession, definition_id: UUID, *, scope: Scope,
) -> GadgetDefinition:
    """Lock one owner-scoped definition before querying dashboard references."""
    row = await session.scalar(
        select(GadgetDefinition)
        .where(GadgetDefinition.id == definition_id, GadgetDefinition.workspace_id == scope.workspace_id,
               GadgetDefinition.owner_id == _actor(scope))
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


async def _lock_owner_creation_quota(session: AsyncSession, scope: Scope) -> None:
    """Serialize dashboard/definition quota checks for one workspace.

    PostgreSQL transaction advisory locks use a namespace distinct from search
    indexing; all creation paths acquire this before source/entity locks and
    hold it until commit. This bounds quota oversubscription without a process lock.
    """
    await session.execute(text(
        "SELECT pg_advisory_xact_lock(hashtextextended('dashboard.quota:' || :workspace_id, 0))"
    ), {"workspace_id": str(scope.workspace_id)})


async def _instance_minima(
    session: AsyncSession, dashboard_id: UUID, *, scope: Scope
) -> tuple[list[GadgetInstance], dict[UUID, tuple[int, int]], dict[UUID, GadgetDefinition]]:
    """Load bounded instances and their owner definitions for layout validation and projection."""
    rows = list(
        (await session.scalars(
            select(GadgetInstance)
            .join(Dashboard, Dashboard.id == GadgetInstance.dashboard_id)
            .where(GadgetInstance.dashboard_id == dashboard_id, Dashboard.workspace_id == scope.workspace_id)
            .order_by(GadgetInstance.position, GadgetInstance.id)
        )).all()
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
        select(GadgetDefinition).where(GadgetDefinition.id.in_(identifiers),
            GadgetDefinition.workspace_id == scope.workspace_id)
    )
    return {row.id: row for row in result.all()}


async def _source_states(
    session: AsyncSession, source_ids: Sequence[UUID], *, scope: Scope, multi_workspace_enabled: bool
) -> dict[UUID, Any]:
    """Read the bounded owner source projection without copying connector secrets or content."""
    if not source_ids:
        return {}
    rows = await sources.get_gadget_sources(session, tuple(dict.fromkeys(source_ids)),
        scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return {row.id: row for row in rows}


async def _definition_source_states(
    session: AsyncSession, definitions: Sequence[GadgetDefinition], *, scope: Scope, multi_workspace_enabled: bool
) -> dict[UUID, Any]:
    """Resolve lifecycle metadata for a bounded owner definition set in 32-ID source batches."""
    source_ids = list(dict.fromkeys(
        UUID(str(source_id)) for definition in definitions for source_id in definition.source_ids
    ))
    states: dict[UUID, Any] = {}
    for offset in range(0, len(source_ids), 32):
        states.update(await _source_states(session, source_ids[offset : offset + 32],
            scope=scope, multi_workspace_enabled=multi_workspace_enabled))
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
    return [{"code": "renderer_planned"}, *[
        {"code": "capability_unavailable", "capability": key}
        for key in descriptor.capability_keys
    ]]


async def _dashboard_read(
    session: AsyncSession, dashboard: Dashboard, *, scope: Scope, multi_workspace_enabled: bool,
) -> DashboardDetail:
    """Build dashboard detail with descriptor-derived renderer state and source lifecycle warnings."""
    await session.refresh(dashboard)
    groups = list((await session.scalars(
        select(DashboardGroup).join(Dashboard, Dashboard.id == DashboardGroup.dashboard_id)
        .where(DashboardGroup.dashboard_id == dashboard.id, Dashboard.workspace_id == scope.workspace_id)
        .order_by(DashboardGroup.position, DashboardGroup.id)
    )).all())
    instances, _minimums, definitions = await _instance_minima(session, dashboard.id, scope=scope)
    source_ids = list(dict.fromkeys(
        UUID(str(source_id)) for definition in definitions.values() for source_id in definition.source_ids
    ))
    source_states: dict[UUID, Any] = {}
    # Each source projection stays within its owning module's 32-ID query ceiling.
    for offset in range(0, len(source_ids), 32):
        source_states.update(await _source_states(session, source_ids[offset : offset + 32],
            scope=scope, multi_workspace_enabled=multi_workspace_enabled))
    placements = list((await session.scalars(
        select(GadgetPlacement).join(Dashboard, Dashboard.id == GadgetPlacement.dashboard_id)
        .where(GadgetPlacement.dashboard_id == dashboard.id, Dashboard.workspace_id == scope.workspace_id)
    )).all())
    by_breakpoint: dict[str, list[dict[str, Any]]] = {"desktop": [], "mobile": []}
    for item in placements:
        by_breakpoint[item.breakpoint].append({
            "instance_id": item.instance_id, "x": item.x, "y": item.y,
            "w": item.w, "h": item.h,
        })
    layout_rows = list((await session.scalars(
        select(DashboardLayout).join(Dashboard, Dashboard.id == DashboardLayout.dashboard_id)
        .where(DashboardLayout.dashboard_id == dashboard.id, Dashboard.workspace_id == scope.workspace_id)
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


async def list_dashboards(session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool) -> list[DashboardSummary]:
    """List the authenticated owner's bounded dashboard summaries in stable creation order."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    rows = await session.scalars(
        select(Dashboard).where(Dashboard.workspace_id == scope.workspace_id, Dashboard.owner_id == _actor(scope))
        .order_by(Dashboard.created_at, Dashboard.id).limit(MAX_DASHBOARDS_PER_OWNER)
    )
    return [DashboardSummary(id=row.id, name=row.name, revision=row.revision,
            created_at=row.created_at, updated_at=row.updated_at) for row in rows.all()]


def _encode_dashboard_export_cursor(
    snapshot_at: datetime, created_at: datetime, identifier: UUID, access_fence: AccessFence,
) -> str:
    """Bind dashboard export position to owner scope, dataset, sort and cutoff."""
    raw = json.dumps([
        2, "dashboards", str(access_fence.workspace_id), access_fence.user_id,
        access_fence.membership_revision, access_fence.configuration_revision,
        {}, "created_at_asc_id_asc", snapshot_at.isoformat(), created_at.isoformat(), str(identifier),
    ], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_dashboard_export_cursor(
    cursor: str, access_fence: AccessFence,
) -> tuple[datetime, datetime, UUID]:
    """Reject legacy, cross-workspace, stale-admission, or altered dashboard cursors."""
    try:
        if len(cursor) > 1024 or "=" in cursor:
            raise ValueError
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != cursor:
            raise ValueError
        value = json.loads(raw)
        expected_scope = [
            2, "dashboards", str(access_fence.workspace_id), access_fence.user_id,
            access_fence.membership_revision, access_fence.configuration_revision,
            {}, "created_at_asc_id_asc",
        ]
        if not isinstance(value, list) or len(value) != 11 or value[:8] != expected_scope:
            raise ValueError
        snapshot_at, created_at = datetime.fromisoformat(value[8]), datetime.fromisoformat(value[9])
        identifier = UUID(value[10])
        if (any(item.tzinfo is None or item.utcoffset() is None for item in (snapshot_at, created_at))
                or snapshot_at.isoformat() != value[8] or created_at.isoformat() != value[9]
                or created_at > snapshot_at or snapshot_at > datetime.now(UTC)
                or str(identifier) != value[10]
                or _encode_dashboard_export_cursor(snapshot_at, created_at, identifier, access_fence) != cursor):
            raise ValueError
        return snapshot_at, created_at, identifier
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error) as exc:
        raise HTTPException(status_code=422, detail="Dashboard export cursor is invalid") from exc


def _encode_owner_export_cursor(
    record_kind: str, snapshot_at: datetime, position_at: datetime, identifier: UUID,
    access_fence: AccessFence,
) -> str:
    """Bind a portable owner keyset position to its dataset and admitted scope."""
    sort = "generated_at_asc_id_asc" if record_kind == "daily_briefs" else "created_at_asc_id_asc"
    raw = json.dumps([
        2, record_kind, str(access_fence.workspace_id), access_fence.user_id,
        access_fence.membership_revision, access_fence.configuration_revision,
        {}, sort, snapshot_at.isoformat(), position_at.isoformat(), str(identifier),
    ], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_owner_export_cursor(
    cursor: str, record_kind: str, access_fence: AccessFence,
) -> tuple[datetime, datetime, UUID]:
    """Reject legacy, cross-workspace, stale-admission, or altered owner cursors."""
    try:
        if len(cursor) > 1024 or "=" in cursor:
            raise ValueError
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != cursor:
            raise ValueError
        sort = "generated_at_asc_id_asc" if record_kind == "daily_briefs" else "created_at_asc_id_asc"
        expected_scope = [
            2, record_kind, str(access_fence.workspace_id), access_fence.user_id,
            access_fence.membership_revision, access_fence.configuration_revision,
            {}, sort,
        ]
        value = json.loads(raw)
        if not isinstance(value, list) or len(value) != 11 or value[:8] != expected_scope:
            raise ValueError
        snapshot_at, position_at = datetime.fromisoformat(value[8]), datetime.fromisoformat(value[9])
        identifier = UUID(value[10])
        if (any(item.tzinfo is None or item.utcoffset() is None for item in (snapshot_at, position_at))
                or snapshot_at.isoformat() != value[8] or position_at.isoformat() != value[9]
                or position_at > snapshot_at or snapshot_at > datetime.now(UTC)
                or str(identifier) != value[10]
                or _encode_owner_export_cursor(record_kind, snapshot_at, position_at, identifier, access_fence) != cursor):
            raise ValueError
        return snapshot_at, position_at, identifier
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error) as exc:
        raise HTTPException(status_code=422, detail="Owner export cursor is invalid") from exc


def _dashboard_export_scope(owner_id: int, snapshot_at: datetime, workspace_id: UUID) -> tuple[ColumnElement[bool], ...]:
    """Select workspace-local parent dashboard revisions that existed unchanged at the cutoff."""
    return (
        Dashboard.workspace_id == workspace_id, Dashboard.owner_id == owner_id,
        Dashboard.created_at <= snapshot_at, Dashboard.updated_at <= snapshot_at,
    )


def _definition_export_scope(owner_id: int, snapshot_at: datetime, workspace_id: UUID) -> tuple[ColumnElement[bool], ...]:
    """Select the complete set of saved owner definitions created by the cutoff."""
    return (
        GadgetDefinition.workspace_id == workspace_id,
        GadgetDefinition.owner_id == owner_id,
        GadgetDefinition.created_at <= snapshot_at,
    )


def _definition_export_read(row: GadgetDefinition) -> GadgetDefinitionExport:
    """Project persisted selectors only, excluding placements, live warnings and renderer output."""
    return GadgetDefinitionExport(
        id=row.id, name=row.name, revision=row.revision, renderer=row.renderer,
        source_ids=[UUID(str(item)) for item in row.source_ids],
        scope=GadgetScope.model_validate(row.scope), filters=GadgetFilters.model_validate(row.filters),
        highlight_rules=[HighlightRule.model_validate(item) for item in row.highlight_rules],
        created_at=row.created_at, updated_at=row.updated_at,
    )


def _definition_export_row_digest(row: GadgetDefinition) -> str:
    """Hash the exact persisted selector fields and both revision timestamps for final fencing."""
    value = {
        "id": str(row.id), "name": row.name, "revision": row.revision, "renderer": row.renderer,
        "source_ids": row.source_ids, "scope": row.scope, "filters": row.filters,
        "highlight_rules": row.highlight_rules, "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


async def _definition_export_page(
    session: AsyncSession, *, owner_id: int, limit: int, cursor: str | None,
    scope: Scope, multi_workspace_enabled: bool,
) -> GadgetDefinitionExportPage:
    """Page all definitions, including unplaced; omit changed-after-cutoff configs and fence them."""
    access_fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if owner_id != _actor(scope) or not 1 <= limit <= 100:
        raise ValueError("Gadget definition export owner or page limit is invalid")
    if cursor is None:
        snapshot_at, position = datetime.now(UTC), None
    else:
        snapshot_at, position_at, position_id = _decode_owner_export_cursor(cursor, "gadget_definitions", access_fence)
        position = (position_at, position_id)
    predicates = _definition_export_scope(owner_id, snapshot_at, scope.workspace_id)
    snapshot_count = int(await session.scalar(
        select(func.count()).select_from(GadgetDefinition).where(*predicates)
    ) or 0)
    statement = select(GadgetDefinition).where(*predicates)
    if position is not None:
        from sqlalchemy import tuple_
        statement = statement.where(tuple_(GadgetDefinition.created_at, GadgetDefinition.id) > position)
    rows = list((await session.scalars(
        statement.order_by(GadgetDefinition.created_at, GadgetDefinition.id).limit(limit + 1)
        .execution_options(populate_existing=True)
    )).all())
    has_more, rows = len(rows) > limit, rows[:limit]
    eligible_rows = [row for row in rows if row.updated_at <= snapshot_at]
    items = [_definition_export_read(row) for row in eligible_rows]
    encoded = [item.model_dump_json().encode("utf-8") for item in items]
    payload_bytes = 2 + sum(map(len, encoded)) + max(0, len(items) - 1)
    if payload_bytes > 16_777_216:
        raise HTTPException(status_code=413, detail="Gadget definition export page exceeds its byte bound")
    fences = [GadgetDefinitionExportFence(
        id=row.id, created_at=row.created_at, updated_at=row.updated_at, revision=row.revision,
        content_digest=_definition_export_row_digest(row), eligible=row.updated_at <= snapshot_at,
    ) for row in rows]
    return GadgetDefinitionExportPage(
        owner_id=owner_id, record_kind="gadget_definitions", snapshot_at=snapshot_at,
        snapshot_count=snapshot_count, omitted_count=len(rows) - len(eligible_rows),
        items=items, fences=fences, payload_bytes=payload_bytes, available=True,
        next_cursor=_encode_owner_export_cursor("gadget_definitions", snapshot_at, rows[-1].created_at, rows[-1].id, access_fence)
        if has_more and rows else None,
        omission_reason="definition_changed_after_snapshot" if len(rows) != len(eligible_rows) else None,
    )


async def _definition_export_validation(
    session: AsyncSession, *, owner_id: int, snapshot_at: datetime,
    expected_snapshot_count: int, fences: list[GadgetDefinitionExportFence],
    scope: Scope, multi_workspace_enabled: bool,
) -> GadgetDefinitionExportValidation:
    """Recheck the definition cutoff inventory and exact saved-selector digest before publication."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if owner_id != _actor(scope) or len(fences) > 100:
        raise ValueError("Gadget definition export validation input is invalid")
    predicates = _definition_export_scope(owner_id, snapshot_at, scope.workspace_id)
    observed = int(await session.scalar(
        select(func.count()).select_from(GadgetDefinition).where(*predicates)
    ) or 0)
    if observed != expected_snapshot_count:
        return GadgetDefinitionExportValidation(
            valid=False, reason="snapshot_count_changed", observed_snapshot_count=observed,
        )
    for fence in fences:
        row = await session.scalar(select(GadgetDefinition).where(
            GadgetDefinition.id == fence.id, *predicates,
        ).execution_options(populate_existing=True))
        if row is None:
            return GadgetDefinitionExportValidation(
                valid=False, reason="record_changed", observed_snapshot_count=observed,
            )
        eligible = row.updated_at <= snapshot_at
        if (row.created_at != fence.created_at or row.updated_at != fence.updated_at
                or row.revision != fence.revision or eligible != fence.eligible
                or _definition_export_row_digest(row) != fence.content_digest):
            return GadgetDefinitionExportValidation(
                valid=False, reason="record_changed", observed_snapshot_count=observed,
            )
    return GadgetDefinitionExportValidation(valid=True, reason="valid", observed_snapshot_count=observed)


def _newer_export_definition_exists(owner_id: int, snapshot_at: datetime, workspace_id: UUID) -> Exists:
    """Find child definitions updated after the cutoff but used by a retained dashboard."""
    return select(GadgetInstance.id).join(
        GadgetDefinition, GadgetDefinition.id == GadgetInstance.definition_id,
    ).where(
        GadgetInstance.dashboard_id == Dashboard.id,
        GadgetDefinition.workspace_id == workspace_id,
        GadgetDefinition.owner_id == owner_id,
        GadgetDefinition.updated_at > snapshot_at,
    ).correlate(Dashboard).exists()


async def _brief_export_eligibility(
    session: AsyncSession, row: DailyBrief, *, scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Allow export only when captured prompt lineage remains structurally and currently valid.

    Historical independent facts remain exportable; current story/event support is
    revalidated by the manifest checker without comparing unrelated dashboard facts.
    """
    citations = _brief_export_citations(row)
    return bool(
        citations is not None
        and row.status == "current"
        and await briefs._captured_inputs_match(session, row, lock=False, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled)
    )


def _brief_export_citations(row: DailyBrief) -> list[dict[str, Any]] | None:
    """Allowlist the saved citation identity fields, rejecting malformed/duplicate references early."""
    if not isinstance(row.citations, list) or not row.citations or len(row.citations) > 40:
        return None
    expected_keys = {"ref", "kind", "id", "title", "source_ids"}
    citations: list[dict[str, Any]] = []
    seen_references: set[int] = set()
    for citation in row.citations:
        if not isinstance(citation, dict) or set(citation) != expected_keys:
            return None
        reference, kind, identifier = citation["ref"], citation["kind"], citation["id"]
        title, source_ids = citation["title"], citation["source_ids"]
        if (type(reference) is not int or not 1 <= reference <= 40 or reference in seen_references
                or not isinstance(kind, str) or not isinstance(identifier, str)
                or not isinstance(title, str) or not isinstance(source_ids, list)
                or any(not isinstance(value, str) for value in source_ids)):
            return None
        seen_references.add(reference)
        citations.append({
            "ref": reference, "kind": kind, "id": identifier,
            "title": title, "source_ids": list(source_ids),
        })
    return citations


def _brief_export_row_digest(row: DailyBrief) -> str:
    """Hash only persisted brief fields intentionally eligible for portable projection."""
    value = {
        "id": str(row.id), "brief_date": row.brief_date.isoformat(), "timezone": row.timezone,
        "revision": row.revision, "status": row.status, "content": row.content,
        "citations": row.citations, "model_alias": row.model_alias,
        "generated_at": row.generated_at.isoformat(),
        "evidence_capture_version": row.evidence_capture_version,
        "evidence_capture_status": row.evidence_capture_status,
        "evidence_fact_count": row.evidence_fact_count,
    }
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


async def _brief_export_page(
    session: AsyncSession, *, owner_id: int, limit: int, cursor: str | None,
    scope: Scope, multi_workspace_enabled: bool,
) -> BriefExportPage:
    """Page every saved revision at one cutoff and omit text whose exact live citation set fails."""
    access_fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if owner_id != _actor(scope) or not 1 <= limit <= 100:
        raise ValueError("Daily brief export owner or page limit is invalid")
    if cursor is None:
        snapshot_at, position = datetime.now(UTC), None
    else:
        snapshot_at, position_at, position_id = _decode_owner_export_cursor(cursor, "daily_briefs", access_fence)
        position = (position_at, position_id)
    predicates = (DailyBrief.workspace_id == scope.workspace_id, DailyBrief.owner_id == owner_id,
        DailyBrief.generated_at <= snapshot_at)
    snapshot_count = int(await session.scalar(
        select(func.count()).select_from(DailyBrief).where(*predicates)
    ) or 0)
    statement = select(DailyBrief).where(*predicates)
    if position is not None:
        from sqlalchemy import tuple_
        statement = statement.where(tuple_(DailyBrief.generated_at, DailyBrief.id) > position)
    rows = list((await session.scalars(
        statement.order_by(DailyBrief.generated_at, DailyBrief.id).limit(limit + 1)
        .execution_options(populate_existing=True)
    )).all())
    has_more, rows = len(rows) > limit, rows[:limit]
    items: list[DailyBriefExport] = []
    fences: list[BriefExportFence] = []
    omitted_count = 0
    # Lock-free eligibility: export spans many pages in one transaction, so it must not accumulate
    # locks; `_brief_export_validation` rejects anything that changed after this scan.
    for row in rows:
        eligible = await _brief_export_eligibility(session, row, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled)
        fences.append(BriefExportFence(
            id=row.id, generated_at=row.generated_at, revision=row.revision,
            content_digest=_brief_export_row_digest(row), eligible=eligible,
        ))
        if not eligible:
            omitted_count += 1
            continue
        citations = _brief_export_citations(row)
        if citations is None:
            raise HTTPException(status_code=409, detail="Daily brief changed during export; retry the download")
        items.append(DailyBriefExport(
            id=row.id, brief_date=row.brief_date, timezone=row.timezone, revision=row.revision,
            status="current", content=row.content, citations=citations,
            model_alias=row.model_alias,
            generated_at=row.generated_at,
        ))
    encoded = [item.model_dump_json().encode("utf-8") for item in items]
    payload_bytes = 2 + sum(map(len, encoded)) + max(0, len(items) - 1)
    if payload_bytes > 16_777_216:
        raise HTTPException(status_code=413, detail="Daily brief export page exceeds its byte bound")
    return BriefExportPage(
        owner_id=owner_id, record_kind="daily_briefs", snapshot_at=snapshot_at,
        snapshot_count=snapshot_count, omitted_count=omitted_count, items=items, fences=fences,
        payload_bytes=payload_bytes,
        next_cursor=_encode_owner_export_cursor("daily_briefs", snapshot_at, rows[-1].generated_at, rows[-1].id, access_fence)
        if has_more and rows else None,
        omission_reason="unsupported_or_deleted_citation" if omitted_count else None,
    )


async def _brief_export_validation(
    session: AsyncSession, *, owner_id: int, snapshot_at: datetime,
    expected_snapshot_count: int, fences: list[BriefExportFence],
    scope: Scope, multi_workspace_enabled: bool,
) -> BriefExportValidation:
    """Recheck all scanned revisions, including omitted rows, so deletion or support changes abort."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if owner_id != _actor(scope) or len(fences) > 100:
        raise ValueError("Daily brief export validation input is invalid")
    predicates = (DailyBrief.workspace_id == scope.workspace_id, DailyBrief.owner_id == owner_id,
        DailyBrief.generated_at <= snapshot_at)
    observed = int(await session.scalar(select(func.count()).select_from(DailyBrief).where(*predicates)) or 0)
    if observed != expected_snapshot_count:
        return BriefExportValidation(valid=False, reason="snapshot_count_changed", observed_snapshot_count=observed)
    for fence in fences:
        row = await session.scalar(select(DailyBrief).where(
            DailyBrief.id == fence.id, *predicates,
        ).execution_options(populate_existing=True))
        if row is None:
            return BriefExportValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
        eligible = await _brief_export_eligibility(session, row, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled)
        if (row.generated_at != fence.generated_at or row.revision != fence.revision
                or eligible != fence.eligible or _brief_export_row_digest(row) != fence.content_digest):
            return BriefExportValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
    return BriefExportValidation(valid=True, reason="valid", observed_snapshot_count=observed)


def _brief_schedule_export_value(row: BriefScheduleRow | None) -> BriefScheduleExport:
    """Project the saved/default editable time without its private scheduler ownership fields."""
    schedule = BriefSchedule.model_validate(row) if row is not None else BriefSchedule()
    return BriefScheduleExport.model_validate(schedule.model_dump())


def _brief_schedule_export_fence(row: BriefScheduleRow | None) -> BriefScheduleExportFence:
    """Digest the safe schedule projection together with its persisted-row identity and timestamp."""
    value = _brief_schedule_export_value(row)
    updated_at = row.updated_at if row is not None else None
    data = {"persisted": row is not None, "updated_at": updated_at.isoformat() if updated_at else None,
            "schedule": value.model_dump(mode="json")}
    return BriefScheduleExportFence(
        persisted=row is not None, updated_at=updated_at,
        content_digest=hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
    )


async def _brief_schedule_export_page(
    session: AsyncSession, *, owner_id: int, scope: Scope, multi_workspace_enabled: bool,
) -> BriefScheduleExportPage:
    """Return the default or persisted owner schedule and reject updates racing the cutoff."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if owner_id != _actor(scope):
        raise ValueError("Brief schedule export owner is invalid")
    snapshot_at = datetime.now(UTC)
    row = await session.scalar(select(BriefScheduleRow).where(
        BriefScheduleRow.workspace_id == scope.workspace_id, BriefScheduleRow.owner_id == owner_id,
    ))
    if row is not None and row.updated_at > snapshot_at:
        raise HTTPException(status_code=409, detail="Brief schedule changed during export; retry the download")
    item = _brief_schedule_export_value(row)
    payload_bytes = 2 + len(item.model_dump_json().encode("utf-8"))
    return BriefScheduleExportPage(
        owner_id=owner_id, record_kind="brief_schedule", snapshot_at=snapshot_at,
        snapshot_count=1, items=[item], fences=[_brief_schedule_export_fence(row)],
        payload_bytes=payload_bytes, next_cursor=None, available=True, omission_reason=None,
    )


async def _brief_schedule_export_validation(
    session: AsyncSession, *, owner_id: int, snapshot_at: datetime,
    expected_snapshot_count: int, fences: list[BriefScheduleExportFence],
    scope: Scope, multi_workspace_enabled: bool,
) -> BriefScheduleExportValidation:
    """Compare persisted/default schedule state immediately before the aggregate download is returned."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if owner_id != _actor(scope) or expected_snapshot_count != 1 or len(fences) != 1:
        raise ValueError("Brief schedule export validation input is invalid")
    row = await session.scalar(select(BriefScheduleRow).where(
        BriefScheduleRow.workspace_id == scope.workspace_id, BriefScheduleRow.owner_id == owner_id,
    ).execution_options(populate_existing=True))
    observed = 1
    current = _brief_schedule_export_fence(row)
    expected = fences[0]
    valid = (
        (row is None or row.updated_at <= snapshot_at)
        and current.persisted == expected.persisted
        and current.updated_at == expected.updated_at
        and current.content_digest == expected.content_digest
    )
    return BriefScheduleExportValidation(
        valid=valid, reason="valid" if valid else "record_changed", observed_snapshot_count=observed,
    )


async def export_page(
    session: AsyncSession, *, owner_id: int, record_kind: str, limit: int = 50, cursor: str | None = None,
    scope: Scope, multi_workspace_enabled: bool,
) -> DashboardExportPage | GadgetDefinitionExportPage | BriefExportPage | BriefScheduleExportPage:
    """Return one cutoff-bound dashboard, definition, retained-brief or schedule page.

    Each dataset is owner-scoped and size-bounded. Dashboard placements exclude newer unretained
    definition revisions; definitions include unplaced saved selectors; brief prose is emitted only
    when every actual fact kind/ID/title/source citation remains currently eligible; schedule output
    excludes automation ownership identifiers. The aggregate caller must revalidate returned fences.
    """
    access_fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if owner_id != _actor(scope):
        raise ValueError("Dashboard export actor does not match the admitted workspace")
    if record_kind == "gadget_definitions":
        return await _definition_export_page(session, owner_id=owner_id, limit=limit, cursor=cursor,
            scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if record_kind == "daily_briefs":
        return await _brief_export_page(session, owner_id=owner_id, limit=limit, cursor=cursor,
            scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if record_kind == "brief_schedule":
        if cursor is not None:
            raise ValueError("Brief schedule export does not accept a cursor")
        return await _brief_schedule_export_page(session, owner_id=owner_id, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled)
    if record_kind != "dashboards" or not 1 <= limit <= 100:
        raise ValueError("Dashboard export owner, kind or page limit is invalid")
    if cursor is None:
        snapshot_at, position = datetime.now(UTC), None
    else:
        snapshot_at, position_at, position_id = _decode_dashboard_export_cursor(cursor, access_fence)
        position = (position_at, position_id)
    predicates = _dashboard_export_scope(owner_id, snapshot_at, scope.workspace_id)
    snapshot_count = int(await session.scalar(select(func.count()).select_from(Dashboard).where(*predicates)) or 0)
    # Gadget definitions have independent revisions. If one changed after the owner cutoff,
    # omit the whole dataset because the prior definition version is not retained here.
    if await session.scalar(select(Dashboard.id).where(*predicates,
            _newer_export_definition_exists(owner_id, snapshot_at, scope.workspace_id)).limit(1)) is not None:
        return DashboardExportPage(
            owner_id=owner_id, record_kind="dashboards", snapshot_at=snapshot_at,
            snapshot_count=snapshot_count, items=[], fences=[], payload_bytes=2,
            available=False, omission_reason="definition_changed_after_snapshot",
        )
    statement = select(Dashboard).where(*predicates)
    if position is not None:
        from sqlalchemy import tuple_
        statement = statement.where(tuple_(Dashboard.created_at, Dashboard.id) > position)
    rows = list((await session.scalars(
        statement.order_by(Dashboard.created_at, Dashboard.id).limit(limit + 1)
        .execution_options(populate_existing=True)
    )).all())
    has_more, rows = len(rows) > limit, rows[:limit]
    items = [await _dashboard_read(session, row, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled) for row in rows]
    encoded = [item.model_dump_json().encode("utf-8") for item in items]
    payload_bytes = 2 + sum(map(len, encoded)) + max(0, len(items) - 1)
    if payload_bytes > 16_777_216:
        raise HTTPException(status_code=413, detail="Dashboard export page exceeds its byte bound")
    fences = [DashboardExportFence(
        id=row.id, created_at=row.created_at, updated_at=row.updated_at, revision=row.revision,
        content_digest=hashlib.sha256(raw).hexdigest(),
    ) for row, raw in zip(rows, encoded, strict=True)]
    return DashboardExportPage(
        owner_id=owner_id, record_kind="dashboards", snapshot_at=snapshot_at,
        snapshot_count=snapshot_count, items=items, fences=fences, payload_bytes=payload_bytes,
        next_cursor=_encode_dashboard_export_cursor(snapshot_at, rows[-1].created_at, rows[-1].id, access_fence)
        if has_more and rows else None,
    )


async def validate_export_fences(
    session: AsyncSession, *, owner_id: int, record_kind: str, snapshot_at: datetime,
    expected_snapshot_count: int, fences: list[DashboardExportFence],
    scope: Scope, multi_workspace_enabled: bool,
) -> (DashboardExportValidation | GadgetDefinitionExportValidation | BriefExportValidation
      | BriefScheduleExportValidation):
    """Recheck the cutoff inventory and exact public projection/evidence fences for one dataset.

    The export aggregator calls this only after rendering, immediately before constructing the
    response. Bounds cap each fence batch; invalid owner, dataset or changed evidence fails closed.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if owner_id != _actor(scope):
        raise ValueError("Dashboard export actor does not match the admitted workspace")
    if record_kind == "gadget_definitions":
        return await _definition_export_validation(
            session, owner_id=owner_id, snapshot_at=snapshot_at,
            expected_snapshot_count=expected_snapshot_count,
            fences=fences,  # type: ignore[arg-type]
            scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
    if record_kind == "daily_briefs":
        return await _brief_export_validation(
            session, owner_id=owner_id, snapshot_at=snapshot_at,
            expected_snapshot_count=expected_snapshot_count,
            fences=fences,  # type: ignore[arg-type]
            scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
    if record_kind == "brief_schedule":
        return await _brief_schedule_export_validation(
            session, owner_id=owner_id, snapshot_at=snapshot_at,
            expected_snapshot_count=expected_snapshot_count,
            fences=fences,  # type: ignore[arg-type]
            scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
    if record_kind != "dashboards" or len(fences) > 100:
        raise ValueError("Dashboard export validation input is invalid")
    observed = int(await session.scalar(
        select(func.count()).select_from(Dashboard).where(*_dashboard_export_scope(owner_id, snapshot_at, scope.workspace_id))
    ) or 0)
    if observed != expected_snapshot_count:
        return DashboardExportValidation(valid=False, reason="snapshot_count_changed", observed_snapshot_count=observed)
    if fences and await session.scalar(select(Dashboard.id).where(
        *_dashboard_export_scope(owner_id, snapshot_at, scope.workspace_id),
        _newer_export_definition_exists(owner_id, snapshot_at, scope.workspace_id),
    ).limit(1)) is not None:
        return DashboardExportValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
    for fence in fences:
        row = await session.scalar(select(Dashboard).where(
            Dashboard.id == fence.id, *_dashboard_export_scope(owner_id, snapshot_at, scope.workspace_id),
        ).execution_options(populate_existing=True))
        if row is None:
            return DashboardExportValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
        item = await _dashboard_read(session, row, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled)
        if (item.created_at != fence.created_at or item.updated_at != fence.updated_at
                or item.revision != fence.revision
                or hashlib.sha256(item.model_dump_json().encode("utf-8")).hexdigest() != fence.content_digest):
            return DashboardExportValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
    return DashboardExportValidation(valid=True, reason="valid", observed_snapshot_count=observed)


async def get_dashboard(session: AsyncSession, dashboard_id: UUID, *, scope: Scope, multi_workspace_enabled: bool) -> DashboardDetail | None:
    """Return a detached owner-only dashboard configuration, without renderer payload data."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    dashboard = await session.scalar(select(Dashboard).where(
        Dashboard.id == dashboard_id, Dashboard.workspace_id == scope.workspace_id, Dashboard.owner_id == _actor(scope)
    ))
    return await _dashboard_read(session, dashboard, scope=scope, multi_workspace_enabled=multi_workspace_enabled) if dashboard else None


async def create_dashboard(session: AsyncSession, payload: DashboardCreate, *, scope: Scope, multi_workspace_enabled: bool) -> DashboardDetail:
    """Create a dashboard with empty desktop/mobile layouts and publish revision one atomically."""
    fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    owner_id = _actor(scope)
    await _lock_owner_creation_quota(session, scope)
    count = await session.scalar(select(func.count()).select_from(Dashboard).where(Dashboard.workspace_id == scope.workspace_id, Dashboard.owner_id == owner_id)) or 0
    if count >= MAX_DASHBOARDS_PER_OWNER:
        raise DashboardConflict("dashboard_limit", "Dashboard limit reached")
    dashboard = Dashboard(workspace_id=scope.workspace_id, owner_id=owner_id, name=payload.name)
    session.add(dashboard)
    await session.flush()
    session.add_all([
        DashboardLayout(dashboard_id=dashboard.id, breakpoint=key, columns=20)
        for key in ("desktop", "mobile")
    ])
    await commit_with_replay(session, [make_dashboard_change("dashboard", dashboard.id, dashboard.revision, scope=scope)], scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence)
    return await _dashboard_read(session, dashboard, scope=scope, multi_workspace_enabled=multi_workspace_enabled)


async def patch_dashboard(
    session: AsyncSession, dashboard_id: UUID, payload: DashboardPatch, *, scope: Scope, multi_workspace_enabled: bool
) -> DashboardDetail:
    """Rename an owned dashboard under its shared revision and publish one committed change."""
    fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    dashboard = await _lock_dashboard(session, dashboard_id, scope=scope)
    _check_revision(dashboard, payload.expected_revision)
    dashboard.name = payload.name
    revision = _bump(dashboard)
    await commit_with_replay(session, [make_dashboard_change("dashboard", dashboard.id, revision, scope=scope)], scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence)
    return await _dashboard_read(session, dashboard, scope=scope, multi_workspace_enabled=multi_workspace_enabled)


async def delete_dashboard(session: AsyncSession, dashboard_id: UUID, expected_revision: int, *, scope: Scope, multi_workspace_enabled: bool) -> None:
    """Delete one dashboard tree while preserving reusable definitions and emitting its terminal revision."""
    fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    dashboard = await _lock_dashboard(session, dashboard_id, scope=scope)
    _check_revision(dashboard, expected_revision)
    revision = _bump(dashboard)
    await session.delete(dashboard)
    await commit_with_replay(session, [make_dashboard_change("dashboard", dashboard_id, revision, deleted=True, scope=scope)], scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence)


async def list_groups(session: AsyncSession, dashboard_id: UUID, *, scope: Scope, multi_workspace_enabled: bool) -> list[DashboardGroupRead] | None:
    """List ordered groups only when the parent dashboard belongs to the authenticated owner."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if await session.scalar(select(Dashboard.id).where(Dashboard.id == dashboard_id,
            Dashboard.workspace_id == scope.workspace_id, Dashboard.owner_id == _actor(scope))) is None:
        return None
    rows = await session.scalars(select(DashboardGroup).join(Dashboard, Dashboard.id == DashboardGroup.dashboard_id)
        .where(DashboardGroup.dashboard_id == dashboard_id, Dashboard.workspace_id == scope.workspace_id)
        .order_by(DashboardGroup.position, DashboardGroup.id))
    return [DashboardGroupRead(id=row.id, dashboard_id=row.dashboard_id, name=row.name, position=row.position) for row in rows.all()]


async def create_group(session: AsyncSession, dashboard_id: UUID, payload: GroupCreate, *, scope: Scope, multi_workspace_enabled: bool) -> DashboardGroupRead:
    """Add a group under dashboard revision lock and publish the resulting dashboard revision."""
    fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    dashboard = await _lock_dashboard(session, dashboard_id, scope=scope)
    _check_revision(dashboard, payload.expected_revision)
    count = await session.scalar(select(func.count()).select_from(DashboardGroup).join(Dashboard, Dashboard.id == DashboardGroup.dashboard_id)
        .where(DashboardGroup.dashboard_id == dashboard_id, Dashboard.workspace_id == scope.workspace_id)) or 0
    if count >= MAX_GROUPS_PER_DASHBOARD:
        raise DashboardConflict("group_limit", "Group limit reached", dashboard.revision)
    group = DashboardGroup(dashboard_id=dashboard_id, name=payload.name, position=payload.position)
    session.add(group)
    revision = _bump(dashboard)
    await session.flush()
    await commit_with_replay(session, [make_dashboard_change("dashboard", dashboard_id, revision, scope=scope)], scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence)
    return DashboardGroupRead(id=group.id, dashboard_id=dashboard_id, name=group.name, position=group.position)


async def patch_group(session: AsyncSession, dashboard_id: UUID, group_id: UUID, payload: GroupPatch, *, scope: Scope, multi_workspace_enabled: bool) -> DashboardGroupRead:
    """Update an owned group with explicit nullable-field semantics and one dashboard revision."""
    fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    dashboard = await _lock_dashboard(session, dashboard_id, scope=scope)
    _check_revision(dashboard, payload.expected_revision)
    group = await session.scalar(select(DashboardGroup).join(Dashboard, Dashboard.id == DashboardGroup.dashboard_id)
        .where(DashboardGroup.id == group_id, DashboardGroup.dashboard_id == dashboard_id,
               Dashboard.workspace_id == scope.workspace_id).with_for_update())
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
    await commit_with_replay(session, [make_dashboard_change("dashboard", dashboard_id, revision, scope=scope)], scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence)
    return DashboardGroupRead(id=group.id, dashboard_id=dashboard_id, name=group.name, position=group.position)


async def delete_group(session: AsyncSession, dashboard_id: UUID, group_id: UUID, expected_revision: int, *, scope: Scope, multi_workspace_enabled: bool) -> None:
    """Delete an empty group only; instances must be moved or explicitly removed first."""
    fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    dashboard = await _lock_dashboard(session, dashboard_id, scope=scope)
    _check_revision(dashboard, expected_revision)
    group = await session.scalar(select(DashboardGroup).join(Dashboard, Dashboard.id == DashboardGroup.dashboard_id)
        .where(DashboardGroup.id == group_id, DashboardGroup.dashboard_id == dashboard_id,
               Dashboard.workspace_id == scope.workspace_id).with_for_update())
    if group is None:
        raise DashboardMissing
    if await session.scalar(select(GadgetInstance.id).join(Dashboard, Dashboard.id == GadgetInstance.dashboard_id)
            .where(GadgetInstance.group_id == group_id, Dashboard.workspace_id == scope.workspace_id).limit(1)) is not None:
        raise DashboardConflict("group_not_empty", "Move or delete group instances first", dashboard.revision)
    await session.delete(group)
    revision = _bump(dashboard)
    await commit_with_replay(session, [make_dashboard_change("dashboard", dashboard_id, revision, scope=scope)], scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence)


async def list_definitions(session: AsyncSession, limit: int = 200, *, scope: Scope, multi_workspace_enabled: bool) -> list[GadgetDefinitionRead]:
    """List a bounded owner library page of reusable configuration definitions."""
    if not 1 <= limit <= MAX_DEFINITIONS_PER_OWNER:
        raise ValueError("Definition page limit must be between 1 and 200")
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    rows = await session.scalars(select(GadgetDefinition).where(
        GadgetDefinition.workspace_id == scope.workspace_id, GadgetDefinition.owner_id == _actor(scope),
    ).order_by(GadgetDefinition.created_at, GadgetDefinition.id).limit(limit))
    definitions = list(rows.all())
    source_states = await _definition_source_states(session, definitions, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled)
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


async def get_definition(session: AsyncSession, definition_id: UUID, *, scope: Scope, multi_workspace_enabled: bool) -> GadgetDefinitionRead | None:
    """Return one owner-only reusable definition or no result for foreign identifiers."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    row = await session.scalar(select(GadgetDefinition).where(GadgetDefinition.id == definition_id,
        GadgetDefinition.workspace_id == scope.workspace_id, GadgetDefinition.owner_id == _actor(scope)))
    if row is None:
        return None
    return _definition_read(row, await _definition_source_states(session, [row], scope=scope,
        multi_workspace_enabled=multi_workspace_enabled))


async def create_definition(session: AsyncSession, payload: GadgetDefinitionCreate, *, scope: Scope, multi_workspace_enabled: bool) -> GadgetDefinitionRead:
    """Validate a planned renderer configuration, enforce source lifecycle and quota, then publish it."""
    fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    owner_id = _actor(scope)
    await _lock_owner_creation_quota(session, scope)
    descriptor = gadgets.renderer_descriptor(payload.renderer)
    configuration = gadgets.validate_renderer_configuration(
        payload.renderer,
        GadgetConfiguration(scope=payload.scope, filters=payload.filters, highlight_rules=payload.highlight_rules),
    )
    await _lock_selected_sources(session, payload.source_ids, require_active=True, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=fence)
    await validate_highlight_rules(session, payload.source_ids, configuration.highlight_rules, scope=scope)
    count = await session.scalar(select(func.count()).select_from(GadgetDefinition).where(
        GadgetDefinition.workspace_id == scope.workspace_id, GadgetDefinition.owner_id == owner_id)) or 0
    if count >= MAX_DEFINITIONS_PER_OWNER:
        raise DashboardConflict("definition_limit", "Definition limit reached")
    row = GadgetDefinition(workspace_id=scope.workspace_id, owner_id=owner_id, name=payload.name, renderer=descriptor.id,
        source_ids=[str(item) for item in payload.source_ids], scope=configuration.scope.model_dump(mode="json"),
        filters=configuration.filters.model_dump(mode="json"), highlight_rules=[item.model_dump(mode="json") for item in configuration.highlight_rules])
    session.add(row)
    await session.flush()
    await commit_with_replay(session, [make_dashboard_change("definition", row.id, row.revision, scope=scope)],
        scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence)
    return _definition_read(row, await _definition_source_states(session, [row], scope=scope,
        multi_workspace_enabled=multi_workspace_enabled))


async def _lock_selected_sources(
    session: AsyncSession, source_ids: Sequence[UUID], *, require_active: bool,
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence,
) -> None:
    """Lock selected sources in UUID order before definitions or dashboards, then re-read lifecycle."""
    for source_id in sorted(set(source_ids), key=str):
        fence = await sources.lock_source(session, source_id, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, expected_access_fence=access_fence)
        if fence is None or (require_active and fence.status != "active"):
            raise DashboardConflict("source_changed", "A selected source is unavailable")


async def patch_definition(session: AsyncSession, definition_id: UUID, payload: GadgetDefinitionPatch, *, scope: Scope, multi_workspace_enabled: bool) -> GadgetDefinitionRead:
    """Patch reusable configuration before locking consuming dashboards, preserving renderer minima."""
    # Source locks precede definition and dashboard locks, so acquire candidate IDs first.
    fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    if "source_ids" in payload.model_fields_set and payload.source_ids is not None:
        await _lock_selected_sources(session, payload.source_ids, require_active=True, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=fence)
    row = await _lock_definition(session, definition_id, scope=scope)
    _check_revision(row, payload.expected_revision)
    if payload.model_fields_set <= {"expected_revision"}:
        raise ValueError("At least one definition field is required")
    if any(getattr(payload, name) is None for name in payload.model_fields_set - {"expected_revision"}):
        raise ValueError("Definition fields cannot be cleared")
    references = list((await session.scalars(select(GadgetInstance.dashboard_id).join(Dashboard, Dashboard.id == GadgetInstance.dashboard_id)
        .where(GadgetInstance.definition_id == definition_id, Dashboard.workspace_id == scope.workspace_id).distinct())).all())
    for dashboard_id in sorted(set(references), key=str):
        await _lock_dashboard(session, dashboard_id, scope=scope)
    if "renderer" in payload.model_fields_set and payload.renderer != row.renderer and references:
        raise DashboardConflict("renderer_in_use", "Renderer cannot change while referenced", row.revision)
    candidate: dict[str, Any] = {
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
    stored_topics = {UUID(str(topic)) for item in row.highlight_rules for topic in item.get("topic_ids", [])}
    await validate_highlight_rules(
        session, candidate["source_ids"], config.highlight_rules, scope=scope, known_topic_ids=stored_topics,
    )
    row.name = candidate["name"]
    row.renderer = candidate["renderer"]
    row.source_ids = [str(item) for item in candidate["source_ids"]]
    row.scope = config.scope.model_dump(mode="json")
    row.filters = config.filters.model_dump(mode="json")
    row.highlight_rules = [item.model_dump(mode="json") for item in config.highlight_rules]
    revision = _bump(row)
    await commit_with_replay(session, [make_dashboard_change("definition", definition_id, revision, scope=scope)],
        scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence)
    return _definition_read(row, await _definition_source_states(session, [row], scope=scope,
        multi_workspace_enabled=multi_workspace_enabled))


async def delete_definition(session: AsyncSession, definition_id: UUID, expected_revision: int, *, scope: Scope, multi_workspace_enabled: bool) -> None:
    """Delete an unused definition under its lock; preserve references by rejecting in-use deletes."""
    fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    row = await _lock_definition(session, definition_id, scope=scope)
    _check_revision(row, expected_revision)
    if await session.scalar(select(GadgetInstance.id).join(Dashboard, Dashboard.id == GadgetInstance.dashboard_id)
            .where(GadgetInstance.definition_id == definition_id, Dashboard.workspace_id == scope.workspace_id).limit(1)) is not None:
        raise DashboardConflict("definition_in_use", "Definition is used by a dashboard", row.revision)
    revision = _bump(row)
    await session.delete(row)
    await commit_with_replay(session, [make_dashboard_change("definition", definition_id, revision, deleted=True, scope=scope)],
        scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence)


async def create_instance(session: AsyncSession, dashboard_id: UUID, payload: InstanceCreate, *, scope: Scope, multi_workspace_enabled: bool) -> DashboardDetail:
    """Create a cross-checked instance and both default placements under definition-before-dashboard locks."""
    fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    definition = await _lock_definition(session, payload.definition_id, scope=scope)
    dashboard = await _lock_dashboard(session, dashboard_id, scope=scope)
    _check_revision(dashboard, payload.expected_revision)
    group = await session.scalar(select(DashboardGroup).join(Dashboard, Dashboard.id == DashboardGroup.dashboard_id).where(
        DashboardGroup.id == payload.group_id, DashboardGroup.dashboard_id == dashboard_id,
        Dashboard.workspace_id == scope.workspace_id,
    ))
    if group is None:
        raise DashboardMissing
    count = await session.scalar(select(func.count()).select_from(GadgetInstance).join(
        Dashboard, Dashboard.id == GadgetInstance.dashboard_id,
    ).where(GadgetInstance.dashboard_id == dashboard_id, Dashboard.workspace_id == scope.workspace_id)) or 0
    if count >= MAX_INSTANCES_PER_DASHBOARD:
        raise DashboardConflict("instance_limit", "Instance limit reached", dashboard.revision)
    instance = GadgetInstance(dashboard_id=dashboard_id, group_id=payload.group_id, definition_id=definition.id, title=payload.title, position=payload.position)
    session.add(instance)
    await session.flush()
    descriptor = gadgets.renderer_descriptor(definition.renderer)
    existing = list((await session.scalars(select(GadgetPlacement).join(
        Dashboard, Dashboard.id == GadgetPlacement.dashboard_id,
    ).where(GadgetPlacement.dashboard_id == dashboard_id, Dashboard.workspace_id == scope.workspace_id))).all())
    for breakpoint in ("desktop", "mobile"):
        layout_row = await session.scalar(select(DashboardLayout).join(
            Dashboard, Dashboard.id == DashboardLayout.dashboard_id,
        ).where(DashboardLayout.dashboard_id == dashboard_id, DashboardLayout.breakpoint == breakpoint,
            Dashboard.workspace_id == scope.workspace_id).with_for_update())
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
    await commit_with_replay(session, [make_dashboard_change("dashboard", dashboard_id, revision, scope=scope)],
        scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence)
    return await _dashboard_read(session, dashboard, scope=scope, multi_workspace_enabled=multi_workspace_enabled)


async def patch_instance(session: AsyncSession, dashboard_id: UUID, instance_id: UUID, payload: InstancePatch, *, scope: Scope, multi_workspace_enabled: bool) -> DashboardDetail:
    """Edit local title/group/order under the dashboard revision while preserving layout geometry."""
    fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    instance_ref = await session.scalar(select(GadgetInstance.definition_id).join(
        Dashboard, Dashboard.id == GadgetInstance.dashboard_id,
    ).where(GadgetInstance.id == instance_id, GadgetInstance.dashboard_id == dashboard_id,
        Dashboard.workspace_id == scope.workspace_id))
    if instance_ref is None:
        raise DashboardMissing
    await _lock_definition(session, instance_ref, scope=scope)
    dashboard = await _lock_dashboard(session, dashboard_id, scope=scope)
    _check_revision(dashboard, payload.expected_revision)
    instance = await session.scalar(select(GadgetInstance).join(
        Dashboard, Dashboard.id == GadgetInstance.dashboard_id,
    ).where(GadgetInstance.id == instance_id, GadgetInstance.dashboard_id == dashboard_id,
        Dashboard.workspace_id == scope.workspace_id).with_for_update())
    if instance is None:
        raise DashboardMissing
    if not payload.model_fields_set - {"expected_revision"}:
        raise ValueError("At least one instance field is required")
    if "title" in payload.model_fields_set:
        instance.title = payload.title
    if "group_id" in payload.model_fields_set:
        if payload.group_id is None:
            raise ValueError("group_id cannot be cleared")
        if await session.scalar(select(DashboardGroup.id).join(Dashboard, Dashboard.id == DashboardGroup.dashboard_id).where(
            DashboardGroup.id == payload.group_id, DashboardGroup.dashboard_id == dashboard_id,
            Dashboard.workspace_id == scope.workspace_id,
        )) is None:
            raise DashboardMissing
        instance.group_id = payload.group_id
    if "position" in payload.model_fields_set:
        if payload.position is None:
            raise ValueError("position cannot be cleared")
        instance.position = payload.position
    revision = _bump(dashboard)
    await commit_with_replay(session, [make_dashboard_change("dashboard", dashboard_id, revision, scope=scope)],
        scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence)
    return await _dashboard_read(session, dashboard, scope=scope, multi_workspace_enabled=multi_workspace_enabled)


async def delete_instance(session: AsyncSession, dashboard_id: UUID, instance_id: UUID, expected_revision: int, *, scope: Scope, multi_workspace_enabled: bool) -> DashboardDetail:
    """Remove an instance and both placements while retaining its reusable definition."""
    fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    definition_id = await session.scalar(select(GadgetInstance.definition_id).join(
        Dashboard, Dashboard.id == GadgetInstance.dashboard_id,
    ).where(GadgetInstance.id == instance_id, GadgetInstance.dashboard_id == dashboard_id,
        Dashboard.workspace_id == scope.workspace_id))
    if definition_id is None:
        raise DashboardMissing
    await _lock_definition(session, definition_id, scope=scope)
    dashboard = await _lock_dashboard(session, dashboard_id, scope=scope)
    _check_revision(dashboard, expected_revision)
    await session.execute(delete(GadgetPlacement).where(GadgetPlacement.dashboard_id == dashboard_id, GadgetPlacement.instance_id == instance_id))
    instance = await session.scalar(select(GadgetInstance).join(
        Dashboard, Dashboard.id == GadgetInstance.dashboard_id,
    ).where(GadgetInstance.id == instance_id, GadgetInstance.dashboard_id == dashboard_id,
        Dashboard.workspace_id == scope.workspace_id).with_for_update())
    if instance is None:
        raise DashboardMissing
    await session.delete(instance)
    revision = _bump(dashboard)
    await commit_with_replay(session, [make_dashboard_change("dashboard", dashboard_id, revision, scope=scope)],
        scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence)
    return await _dashboard_read(session, dashboard, scope=scope, multi_workspace_enabled=multi_workspace_enabled)


async def replace_layout(session: AsyncSession, dashboard_id: UUID, payload: LayoutReplace, *, scope: Scope, multi_workspace_enabled: bool) -> DashboardDetail:
    """Replace only the selected breakpoint after exact membership, minima, bounds, and overlap checks.

    An exact geometry/column resave returns current detail without revision or replay changes.
    """
    fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    dashboard = await _lock_dashboard(session, dashboard_id, scope=scope)
    _check_revision(dashboard, payload.expected_revision)
    _instances, minimums, _ = await _instance_minima(session, dashboard_id, scope=scope)
    item_ids = {item.instance_id for item in payload.items}
    if item_ids != set(minimums):
        raise ValueError("Layout must include every dashboard instance exactly once")
    layouts.validate_layout(payload.items, payload.columns, minimums)
    layout_row = await session.scalar(select(DashboardLayout).join(
        Dashboard, Dashboard.id == DashboardLayout.dashboard_id,
    ).where(DashboardLayout.dashboard_id == dashboard_id, DashboardLayout.breakpoint == payload.breakpoint,
        Dashboard.workspace_id == scope.workspace_id).with_for_update())
    if layout_row is None:
        layout_row = DashboardLayout(dashboard_id=dashboard_id, breakpoint=payload.breakpoint, columns=payload.columns)
        session.add(layout_row)
    unchanged = layout_row.columns == payload.columns
    if unchanged:
        current = list((await session.scalars(select(GadgetPlacement).where(GadgetPlacement.dashboard_id == dashboard_id, GadgetPlacement.breakpoint == payload.breakpoint))).all())
        current_by_id = {row.instance_id: (row.x, row.y, row.w, row.h) for row in current}
        unchanged = current_by_id == {item.instance_id: (item.x, item.y, item.w, item.h) for item in payload.items}
    if unchanged:
        return await _dashboard_read(session, dashboard, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    layout_row.columns = payload.columns
    await session.execute(delete(GadgetPlacement).where(GadgetPlacement.dashboard_id == dashboard_id, GadgetPlacement.breakpoint == payload.breakpoint))
    session.add_all([GadgetPlacement(dashboard_id=dashboard_id, breakpoint=payload.breakpoint, instance_id=item.instance_id, x=item.x, y=item.y, w=item.w, h=item.h) for item in payload.items])
    revision = _bump(dashboard)
    await commit_with_replay(session, [make_dashboard_change("dashboard", dashboard_id, revision, scope=scope)],
        scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence)
    return await _dashboard_read(session, dashboard, scope=scope, multi_workspace_enabled=multi_workspace_enabled)


def renderer_reads() -> list[RendererRead]:
    """Project static renderer state and geometry without asserting runtime/provider acceptance."""
    return [RendererRead.model_validate({"id": row.id, "config_version": row.config_version, "minimum_width": row.minimum_width,
             "minimum_height": row.minimum_height, "runtime_state": row.runtime_state,
             "capability_keys": list(row.capability_keys)}) for row in gadgets.RENDERERS]


async def list_gadget_sources(
    session: AsyncSession, limit: int, cursor: str | None, *, scope: Scope, multi_workspace_enabled: bool,
) -> GadgetSourceSelectionPage:
    """Return the source owner's bounded metadata-only selection page."""
    return await sources.list_gadget_sources(session, limit, cursor, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled)


def preset_catalog() -> list[DashboardPresetRead]:
    """Expose the ten stable source-free preset identities and renderer slots."""
    return [DashboardPresetRead.model_validate({"id": item.id, "label": item.label, "family": item.family,
             "slots": [{"slot_id": slot.slot_id, "renderer": slot.renderer} for slot in item.slots]})
            for item in gadgets.PRESETS]


async def preview_preset(
    session: AsyncSession, preset_id: str, payload: PresetPreviewRequest, *,
    scope: Scope, multi_workspace_enabled: bool,
) -> PresetPreviewRead:
    """Resolve an explicit owner source selection into a canonical, nonpersistent preview."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    preset = gadgets.dashboard_preset(preset_id)
    gadgets.validate_preset_slot_sources(preset_id, payload.slot_sources)
    selected = sorted({source_id for values in payload.slot_sources.values() for source_id in values}, key=str)
    states: dict[UUID, Any] = {}
    for offset in range(0, len(selected), 32):
        states.update(await _source_states(session, selected[offset : offset + 32], scope=scope,
            multi_workspace_enabled=multi_workspace_enabled))
    target_revision = None
    if payload.target_dashboard_id:
        target = await session.scalar(select(Dashboard).where(
            Dashboard.id == payload.target_dashboard_id, Dashboard.workspace_id == scope.workspace_id,
            Dashboard.owner_id == _actor(scope),
        ))
        if target is None:
            raise DashboardMissing
        target_revision = target.revision
    slots: list[dict[str, Any]] = []
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


async def apply_preset(
    session: AsyncSession, preset_id: str, payload: PresetApplyRequest, *,
    scope: Scope, multi_workspace_enabled: bool,
) -> DashboardDetail:
    """Recompute and fingerprint preset state under ordered source/dashboard locks before atomic apply."""
    fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    owner_id = _actor(scope)
    await _lock_owner_creation_quota(session, scope)
    gadgets.dashboard_preset(preset_id)
    await _lock_selected_sources(session, [source_id for values in payload.slot_sources.values() for source_id in values],
        require_active=True, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence)
    target_dashboard_id = payload.target_dashboard_id
    if payload.mode == "replace":
        assert target_dashboard_id is not None and payload.expected_revision is not None  # replace mode requires both
        dashboard = await _lock_dashboard(session, target_dashboard_id, scope=scope)
        _check_revision(dashboard, payload.expected_revision)
    else:
        dashboard = None
    current = (await preview_preset(session, preset_id, PresetPreviewRequest(
        slot_sources=payload.slot_sources, target_dashboard_id=target_dashboard_id
    ), scope=scope, multi_workspace_enabled=multi_workspace_enabled)).model_dump(mode="python")
    if current["preview_fingerprint"] != payload.preview_fingerprint:
        raise DashboardConflict("preview_changed", "Preset preview changed; review it again", dashboard.revision if dashboard else None)
    if payload.mode == "create":
        count = await session.scalar(select(func.count()).select_from(Dashboard).where(
            Dashboard.workspace_id == scope.workspace_id, Dashboard.owner_id == owner_id,
        )) or 0
        if count >= MAX_DASHBOARDS_PER_OWNER:
            raise DashboardConflict("dashboard_limit", "Dashboard limit reached")
        dashboard = Dashboard(workspace_id=scope.workspace_id, owner_id=owner_id, name=payload.name or current["name"])
        session.add(dashboard)
        await session.flush()
        dashboard_revision = dashboard.revision
        session.add_all([DashboardLayout(dashboard_id=dashboard.id, breakpoint=bp, columns=20) for bp in ("desktop", "mobile")])
    else:
        assert dashboard is not None
        dashboard_revision = _bump(dashboard)
        if payload.name is not None:
            assert dashboard is not None
            dashboard.name = payload.name
        await session.execute(delete(DashboardGroup).where(DashboardGroup.dashboard_id == dashboard.id))
        await session.execute(delete(DashboardLayout).where(DashboardLayout.dashboard_id == dashboard.id))
        session.add_all([DashboardLayout(dashboard_id=dashboard.id, breakpoint=bp, columns=20) for bp in ("desktop", "mobile")])
    definition_count = await session.scalar(select(func.count()).select_from(GadgetDefinition).where(
        GadgetDefinition.workspace_id == scope.workspace_id, GadgetDefinition.owner_id == owner_id,
    )) or 0
    if definition_count + len(current["slots"]) > MAX_DEFINITIONS_PER_OWNER:
        raise DashboardConflict("definition_limit", "Definition limit reached", dashboard_revision)
    assert dashboard is not None
    group = DashboardGroup(dashboard_id=dashboard.id, name=current["name"], position=0)
    session.add(group)
    await session.flush()
    created_instances: list[tuple[GadgetInstance, GadgetDefinition, Any]] = []
    for index, slot in enumerate(current["slots"]):
        descriptor = gadgets.renderer_descriptor(slot["renderer"])
        definition = GadgetDefinition(workspace_id=scope.workspace_id, owner_id=owner_id, name=f"{current['name']} · {slot['slot_id']}", renderer=slot["renderer"],
            source_ids=[str(item) for item in slot["source_ids"]], scope={}, filters={"keywords": [], "exclude_keywords": [], "limit": 25}, highlight_rules=[])
        session.add(definition)
        await session.flush()
        assert dashboard is not None
        instance = GadgetInstance(dashboard_id=dashboard.id, group_id=group.id, definition_id=definition.id, title=None, position=index)
        session.add(instance)
        await session.flush()
        created_instances.append((instance, definition, descriptor))
    minimums = {instance.id: (descriptor.minimum_width, descriptor.minimum_height) for instance, _, descriptor in created_instances}
    desktop = layouts.default_desktop_layout(minimums)
    mobile = layouts.default_mobile_layout(minimums)
    for breakpoint, items in (("desktop", desktop), ("mobile", mobile)):
        assert dashboard is not None
        session.add_all([
            GadgetPlacement(dashboard_id=dashboard.id, breakpoint=breakpoint,
                            instance_id=item.instance_id, x=item.x, y=item.y, w=item.w, h=item.h)
            for item in items
        ])
    drafts = [make_dashboard_change("dashboard", dashboard.id, dashboard_revision, scope=scope)]
    drafts.extend(make_dashboard_change("definition", definition.id, definition.revision, scope=scope)
        for _, definition, _descriptor in created_instances)
    await commit_with_replay(session, drafts, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence)
    return await _dashboard_read(session, dashboard, scope=scope, multi_workspace_enabled=multi_workspace_enabled)


# Stable seams for P10 automation: daily context and briefs are consumed through this module only.
from modules.dashboard.briefs import (
    BriefEmpty,
    BriefEvidenceRevoked,
    BriefSlotOwned,
    BriefUnavailable,
    claim_brief_slot,
    clean_document_brief_evidence,
    generate_brief,
    latest_brief,
    legacy_brief_coverage,
    list_briefs,
    read_schedule,
    read_slot_owner,
    release_brief_slot,
)
from modules.dashboard.context import build_daily_context
from modules.dashboard.daily_schemas import BriefRead, DailyContext


async def count_source_gadgets(
    session: AsyncSession, source_id: UUID, *, scope: Scope, cap: int = 1000,
) -> tuple[int, int]:
    """Return workspace-scoped (definition, distinct instance) counts that select a source, saturating at ``cap``."""
    selects = GadgetDefinition.source_ids.contains([str(source_id)])
    definitions = (
        select(GadgetDefinition.id)
        .where(GadgetDefinition.workspace_id == scope.workspace_id, selects).limit(cap).subquery()
    )
    placements = (
        select(GadgetInstance.id)
        .join(GadgetDefinition, GadgetDefinition.id == GadgetInstance.definition_id)
        .where(GadgetDefinition.workspace_id == scope.workspace_id, selects).limit(cap).subquery()
    )
    return (
        int(await session.scalar(select(func.count()).select_from(definitions)) or 0),
        int(await session.scalar(select(func.count()).select_from(placements)) or 0),
    )


__all__ = [
    "BriefEmpty",
    "BriefEvidenceRevoked",
    "BriefRead",
    "BriefSlotOwned",
    "BriefUnavailable",
    "DailyContext",
    "build_daily_context",
    "claim_brief_slot",
    "clean_document_brief_evidence",
    "count_source_gadgets",
    "generate_brief",
    "latest_brief",
    "legacy_brief_coverage",
    "list_briefs",
    "read_schedule",
    "read_slot_owner",
    "release_brief_slot",
]

