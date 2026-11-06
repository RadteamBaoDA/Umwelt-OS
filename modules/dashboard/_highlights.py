"""Gadget highlight evaluation, delivery, rule validation and preview (private to the dashboard package)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import (
    ConflictError,
    GatewayTimeoutError,
)
from core.realtime import commit_with_replay
from core.workspaces.admission import actor_id as _actor
from core.workspaces.schemas import AccessFence, Scope
from modules.dashboard._common import (
    HIGHLIGHT_MATCHES_PER_PAGE_MAX,
    HIGHLIGHT_SCAN_PAGE_LIMIT,
    PREVIEW_MATCH_TIMEOUT_SECONDS,
    PREVIEW_MAX_MATCHES,
    PREVIEW_MAX_PAGES,
    PREVIEW_PAGE_SIZE,
    DashboardMissing,
    _admit,
    _owner_tzinfo,
)
from modules.dashboard._reads import get_definition
from modules.dashboard.models import (
    Dashboard,
    GadgetDefinition,
    GadgetInstance,
)
from modules.dashboard.schemas import (
    MAX_DASHBOARDS_PER_OWNER,
    MAX_RULES_PER_DEFINITION,
    DashboardHighlightRead,
    GadgetDefinitionUsageRead,
    HighlightPreviewRead,
    HighlightPreviewRequest,
    HighlightPreviewRuleRead,
    HighlightRule,
)
from modules.settings import public as settings_public
from modules.sources import public as sources


@dataclass
class _HighlightScan:
    """Unlocked snapshot of one definition's scan inputs, re-verified after the locks are taken."""

    definition_revision: int
    source_ids: tuple[UUID, ...]
    selected_source_snapshot: dict[UUID, Any]
    scan_source_ids: tuple[UUID, ...]
    raw_scope: Any
    rules: list[HighlightRule]
    item_scope: set[str]
    compiled: Any
    definition_snapshot: str
    rules_fingerprint: str
    progress_snapshot: tuple[Any, Any, Any, Any] | None
    page: Any


def _highlight_read(item: Any, match: Any) -> DashboardHighlightRead:
    """Project one projection item and rule match onto the public highlight DTO."""
    return DashboardHighlightRead(
        document_id=item.document_id,
        document_version_id=item.document_version_id,
        source_id=item.source_id,
        title=item.title,
        observed_at=item.observed_at,
        rule_id=match.rule_id,
        matched_keywords=list(match.matched_keywords),
        severity=match.severity,
        notify=match.notify,
        reason=match.reason,
    )


async def _snapshot_highlight_scan(
    session: AsyncSession,
    definition_id: UUID,
    owner_id: int,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> _HighlightScan | None:
    """Snapshot the owner definition, scan position and next evidence page without any row lock.

    Sources and Documents must be settled before the Definition/Progress locks taken later. Returns
    ``None`` when no selected Source is active or there are no rules (nothing to scan). Raises
    ``DashboardMissing`` for an unknown definition, ``ValueError`` when the rule count exceeds the
    validated bound and ``RuntimeError`` when the page could exceed the notification coverage bound.
    """
    from modules.dashboard.highlights import compile_rules
    from modules.dashboard.models import GadgetHighlightProgress
    from modules.knowledge.documents import public as documents

    definition = await session.scalar(
        select(GadgetDefinition)
        .where(
            GadgetDefinition.id == definition_id,
            GadgetDefinition.workspace_id == scope.workspace_id,
            GadgetDefinition.owner_id == owner_id,
        )
        .execution_options(populate_existing=True)
    )
    if definition is None:
        raise DashboardMissing
    definition_revision = definition.revision
    source_ids = tuple(UUID(str(value)) for value in definition.source_ids[:32])
    selected_sources = (
        await sources.get_gadget_sources(
            session,
            source_ids,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if source_ids
        else []
    )
    selected_source_snapshot = {item.id: item for item in selected_sources}
    scan_source_ids = tuple(item.id for item in selected_sources if item.status == "active")
    raw_rules = json.loads(json.dumps(definition.highlight_rules, sort_keys=True))
    raw_scope = json.loads(json.dumps(definition.scope, sort_keys=True))
    rules = [HighlightRule.model_validate(rule) for rule in raw_rules]
    if len(rules) > MAX_RULES_PER_DEFINITION:
        raise ValueError("Highlight rule count exceeds the validated definition bound")
    raw_item_scope = raw_scope.get("source_item_ids", []) if isinstance(raw_scope, dict) else []
    item_scope = (
        {str(value) for value in raw_item_scope} if isinstance(raw_item_scope, list) else set()
    )
    if not scan_source_ids or not rules:
        return None
    # Read-only topic resolution happens before any definition/progress lock is taken.
    compiled = compile_rules(
        rules,
        await _rule_topic_terms(
            session,
            rules,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        ),
    )
    definition_snapshot = json.dumps(
        {
            "revision": definition_revision,
            "source_ids": [str(value) for value in source_ids],
            "scope": raw_scope,
            "rules": raw_rules,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    rules_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "source_ids": [str(value) for value in source_ids],
                "scope": sorted(item_scope),
                "rules": [rule.model_dump(mode="json", exclude_defaults=True) for rule in rules],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()

    progress_snapshot_row = await session.scalar(
        select(GadgetHighlightProgress)
        .where(
            GadgetHighlightProgress.definition_id == definition.id,
        )
        .execution_options(populate_existing=True)
    )
    progress_snapshot = (
        None
        if progress_snapshot_row is None
        else (
            progress_snapshot_row.definition_revision,
            progress_snapshot_row.rules_fingerprint,
            progress_snapshot_row.cursor_created_at,
            progress_snapshot_row.cursor_version_id,
        )
    )
    progress_is_current = progress_snapshot is not None and (
        progress_snapshot[0] == definition_revision and progress_snapshot[1] == rules_fingerprint
    )
    page = await documents.list_gadget_highlight_projection_page(
        session,
        source_ids=scan_source_ids,
        limit=HIGHLIGHT_SCAN_PAGE_LIMIT,
        cursor_created_at=progress_snapshot[2]
        if progress_snapshot and progress_is_current
        else None,
        cursor_version_id=progress_snapshot[3]
        if progress_snapshot and progress_is_current
        else None,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if len(page.items) * len(rules) > HIGHLIGHT_MATCHES_PER_PAGE_MAX:
        raise RuntimeError("Highlight scan page exceeds its notification coverage bound")
    return _HighlightScan(
        definition_revision=definition_revision,
        source_ids=source_ids,
        selected_source_snapshot=selected_source_snapshot,
        scan_source_ids=scan_source_ids,
        raw_scope=raw_scope,
        rules=rules,
        item_scope=item_scope,
        compiled=compiled,
        definition_snapshot=definition_snapshot,
        rules_fingerprint=rules_fingerprint,
        progress_snapshot=progress_snapshot,
        page=page,
    )


async def _lock_highlight_scan(
    session: AsyncSession,
    scan: _HighlightScan,
    definition_id: UUID,
    owner_id: int,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> tuple[GadgetDefinition, Any]:
    """Lock Sources, Documents, Definition then Progress (in that order) and re-verify the snapshot.

    Raises ``ConflictError`` (409) if the selected Sources, page evidence, definition or progress
    changed since ``_snapshot_highlight_scan``. A new or stale-fingerprint progress row is created
    or reset (cursor, cooldowns and suppressions cleared); the cursor is then advanced to the page's
    next position. Returns the locked ``(definition, progress)`` rows; commit is the caller's.
    """
    from modules.dashboard.models import GadgetHighlightProgress, GadgetHighlightSuppression
    from modules.knowledge.documents import public as documents

    page = scan.page
    # The page may contain only a subset of selected Sources. Lock all selected
    # Source identities first, then let Documents lock and verify selected versions.
    source_fences = {}
    for source_id in sorted(scan.scan_source_ids, key=str):
        source_fences[source_id] = await sources.lock_source(
            session,
            source_id,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            expected_access_fence=access_fence,
        )
    if any(
        fence is None
        or fence.status != "active"
        or fence.generation != scan.selected_source_snapshot[source_id].generation
        or fence.local_only != scan.selected_source_snapshot[source_id].local_only
        for source_id, fence in source_fences.items()
    ):
        raise ConflictError(detail="Highlight source selection changed")
    if page.selection_fences and not await documents.validate_gadget_document_selection_fences(
        session,
        tuple(page.selection_fences),
        lock_rows=True,
        max_documents=HIGHLIGHT_SCAN_PAGE_LIMIT,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    ):
        raise ConflictError(detail="Dashboard highlight evidence changed during evaluation")

    definition = await session.scalar(
        select(GadgetDefinition)
        .where(
            GadgetDefinition.id == definition_id,
            GadgetDefinition.workspace_id == scope.workspace_id,
            GadgetDefinition.owner_id == owner_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if definition is None:
        raise DashboardMissing
    current_snapshot = json.dumps(
        {
            "revision": definition.revision,
            "source_ids": definition.source_ids,
            "scope": definition.scope,
            "rules": definition.highlight_rules,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    if current_snapshot != scan.definition_snapshot:
        raise ConflictError(detail="Highlight definition changed during evaluation")

    progress = await session.scalar(
        select(GadgetHighlightProgress)
        .where(
            GadgetHighlightProgress.definition_id == definition.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    current_progress_snapshot = (
        None
        if progress is None
        else (
            progress.definition_revision,
            progress.rules_fingerprint,
            progress.cursor_created_at,
            progress.cursor_version_id,
        )
    )
    if current_progress_snapshot != scan.progress_snapshot:
        raise ConflictError(detail="Highlight scan progress changed during evaluation")
    if progress is None:
        progress = GadgetHighlightProgress(
            definition_id=definition.id,
            definition_revision=definition.revision,
            rules_fingerprint=scan.rules_fingerprint,
            rule_last_notified={},
        )
        session.add(progress)
        await session.flush()
    elif (
        progress.definition_revision != definition.revision
        or progress.rules_fingerprint != scan.rules_fingerprint
    ):
        progress.definition_revision = definition.revision
        progress.rules_fingerprint = scan.rules_fingerprint
        progress.cursor_created_at = None
        progress.cursor_version_id = None
        progress.rule_last_notified = {}
        await session.execute(
            delete(GadgetHighlightSuppression).where(
                GadgetHighlightSuppression.definition_id == definition.id,
            )
        )
    progress.cursor_created_at = page.cursor_created_at if page.has_more else None
    progress.cursor_version_id = page.cursor_version_id if page.has_more else None
    return definition, progress


async def _deliver_highlight_matches(
    session: AsyncSession,
    scan: _HighlightScan,
    definition: GadgetDefinition,
    progress: Any,
    *,
    owner_id: int,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[DashboardHighlightRead]:
    """Match the locked page, apply the delivery policy and emit notifications in the same transaction.

    Delivery policy (T6b) reads only the already-locked progress row plus an unlocked preferences
    SELECT, so the definition -> progress lock order is unchanged. BM-34: quiet hours, cooldown and
    expiry SUPPRESS; the decision is stored so a later rescan of the same version cannot deliver it
    (writes happen under the definition lock). Updates ``progress.rule_last_notified``.
    """
    from modules.dashboard.highlights import match_compiled, notification_allowed
    from modules.dashboard.models import GadgetHighlightSuppression
    from modules.notifications.public import NotificationEmit, NotificationEvidence, emit

    now = datetime.now(UTC)
    rules = scan.rules
    rules_by_id = {rule.id: rule for rule in rules}
    last_notified = {
        key: datetime.fromisoformat(value)
        for key, value in progress.rule_last_notified.items()
        if UUID(key) in rules_by_id
    }
    tz: tzinfo = UTC
    if any(rule.quiet_start for rule in rules):
        tz = _owner_tzinfo(
            (
                await settings_public.read_owner_preferences(
                    session,
                    actor_user_id=owner_id,
                    multi_workspace_enabled=multi_workspace_enabled,
                )
            ).timezone
        )
    found = [
        (item, match)
        for item in scan.page.items
        if not scan.item_scope or str(item.document_id) in scan.item_scope
        for match in match_compiled(item.excerpt, scan.compiled, source_id=item.source_id)
    ]

    def key_of(item: Any, match: Any) -> str:
        """Build the idempotent notification/suppression key for one (rule, version) match."""
        return (
            f"highlight:{definition.id}:{definition.revision}:"
            f"{scan.rules_fingerprint}:{match.rule_id}:{item.document_version_id}"
        )

    suppressed_keys: set[str] = set()
    if found:
        suppressed_keys = set(
            (
                await session.execute(
                    select(GadgetHighlightSuppression.dedupe_key).where(
                        GadgetHighlightSuppression.definition_id == definition.id,
                        GadgetHighlightSuppression.dedupe_key.in_([key_of(i, m) for i, m in found]),
                    )
                )
            ).scalars()
        )
    matches: list[DashboardHighlightRead] = []
    for item, match in found:
        dedupe_key = key_of(item, match)
        deliver = False
        if match.notify and dedupe_key not in suppressed_keys:
            deliver = notification_allowed(
                rules_by_id[match.rule_id],
                now,
                tz,
                last_notified.get(str(match.rule_id)),
            )
            expires = rules_by_id[match.rule_id].expires_at
            # ponytail: quiet/cooldown rows grow unbounded; upgrade is version-based pruning (R1 option 2).
            if not deliver and not (
                expires is not None and now >= expires
            ):  # expiry is permanent until edit
                # Keyed by the locked, scoped definition.id above; the table has no workspace column.
                await session.execute(
                    pg_insert(GadgetHighlightSuppression)
                    .values(
                        definition_id=definition.id,
                        dedupe_key=dedupe_key,
                    )
                    .on_conflict_do_nothing()
                )
        matches.append(_highlight_read(item, match))
        if deliver and await emit(
            session,
            NotificationEmit(
                dedupe_key=dedupe_key,
                kind="dashboard_highlight",
                title=item.title[:300],
                body=match.reason[:1000],
                params={
                    "severity": match.severity,
                    "definition_id": str(definition.id),
                    "definition_revision": definition.revision,
                },
                link="/dashboard",
            ),
            evidence=NotificationEvidence(
                document_id=item.document_id,
                document_version_id=item.document_version_id,
            ),
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        ):
            last_notified[str(match.rule_id)] = now
    progress.rule_last_notified = {key: value.isoformat() for key, value in last_notified.items()}
    return matches


async def _evaluate_highlights_read_only(
    session: AsyncSession,
    definition_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[DashboardHighlightRead]:
    """Evaluate a definition against the newest documents without locks, cursors or notifications.

    Raises ``DashboardMissing`` for an unknown definition and ``ValueError`` for a renderer that
    has no highlight evaluation. Regex matching runs in a worker thread (CPU-bound). At most 100
    matches are returned.
    """
    from modules.dashboard.highlights import compile_rules
    from modules.knowledge.documents import public as documents

    definition_read = await get_definition(
        session, definition_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if definition_read is None:
        raise DashboardMissing
    if definition_read.renderer not in {"highlights", "watch_rules"}:
        raise ValueError("Renderer does not support highlight evaluation")
    requested_source_ids = tuple(definition_read.source_ids[:32])
    if not requested_source_ids:
        return []
    selected_sources = await sources.get_gadget_sources(
        session,
        requested_source_ids,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    source_ids = tuple(item.id for item in selected_sources if item.status == "active")
    if not source_ids:
        return []
    # "Not relevant" scope: hides versions from dashboard feed gadgets and this highlights view only
    # (include_dismissed stays False). Notifications, brief, search and chat are unchanged.
    projection_page = await documents.list_gadget_document_projections(
        session,
        source_ids=source_ids,
        limit=100,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    rules = [HighlightRule.model_validate(rule) for rule in definition_read.highlight_rules]
    raw_item_scope = definition_read.scope.get("source_item_ids", [])
    item_scope = (
        {str(value) for value in raw_item_scope} if isinstance(raw_item_scope, list) else set()
    )
    compiled = compile_rules(
        rules,
        await _rule_topic_terms(
            session,
            rules,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        ),
    )
    matches = []
    scoped = [
        item
        for item in projection_page.items
        if not item_scope or str(item.document_id) in item_scope
    ]
    # Regex work is CPU-bound; keep it off the API event loop.
    per_item = await asyncio.to_thread(_match_items, scoped, compiled)
    for item, item_matches in zip(scoped, per_item, strict=True):
        for match in item_matches:
            matches.append(_highlight_read(item, match))
    return matches[:100]


async def evaluate_gadget_highlights(
    session: AsyncSession,
    definition_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    emit_notifications: bool = False,
) -> list[DashboardHighlightRead]:
    """Evaluate current evidence and durably page scans with at most 96 rule/version matches.

    Scheduled pages hold at most three documents and the schema caps each definition at 32 rules.
    The resulting 3-by-32 ceiling keeps every matching notification in the same cursor transaction.
    Highlight notification titles carry exact private Document/version provenance; Notifications
    rechecks the selected current version and keeps that provenance out of its public DTO.

    With ``emit_notifications`` the scan is durable: snapshot (no locks) -> ordered locks
    (Sources, Documents, Definition, Progress) with re-verification -> delivery, and progress plus
    notifications commit together under the original access fence. Without it the call is a
    read-only preview-style evaluation.
    """
    access_fence = await _admit(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        lock=emit_notifications,
    )
    if not emit_notifications:
        return await _evaluate_highlights_read_only(
            session,
            definition_id,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    owner_id = _actor(scope)
    scan = await _snapshot_highlight_scan(
        session,
        definition_id,
        owner_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if scan is None:
        return []
    definition, progress = await _lock_highlight_scan(
        session,
        scan,
        definition_id,
        owner_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    matches = await _deliver_highlight_matches(
        session,
        scan,
        definition,
        progress,
        owner_id=owner_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    # Progress and notifications commit together under the original access fence.
    await commit_with_replay(
        session,
        [],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    return matches[:100]


async def _rule_topic_terms(
    session: AsyncSession,
    rules: Sequence[HighlightRule],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> dict[UUID, list[str]]:
    """Resolve the actor's live, active topics referenced by rules in this workspace (read-only)."""
    from modules.news import public as news

    topic_ids = list(dict.fromkeys(topic for rule in rules for topic in rule.topic_ids))
    return await news.resolve_topic_terms(
        session,
        topic_ids,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )


class HighlightRuleError(ValueError):
    """A rule breaks a scope or ownership invariant; ``code`` lets the web client localize it."""

    def __init__(self, code: str, message: str) -> None:
        """Keep the stable machine code beside the English message."""
        super().__init__(message)
        self.code = code


async def validate_highlight_rules(
    session: AsyncSession,
    source_ids: Sequence[UUID],
    rules: Sequence[HighlightRule],
    *,
    scope: Scope,
    known_topic_ids: Collection[UUID] | None = (),
) -> None:
    """Trust-boundary check: rule sources stay inside the definition scope; new topics must be the owner's.

    Topic ids in ``known_topic_ids`` (already stored on the definition) are not re-checked, so a topic
    deleted later never blocks edits; ``None`` skips the topic check (preview reports them unresolved).
    """
    from modules.news import public as news

    allowed = set(source_ids)
    for rule in rules:
        if not set(rule.source_ids) <= allowed or not set(rule.exclude_source_ids) <= allowed:
            raise HighlightRuleError(
                "rule_sources_not_subset", "Rule sources must be a subset of the definition sources"
            )
        if allowed and allowed <= set(rule.exclude_source_ids):
            raise HighlightRuleError(
                "rule_excludes_all_sources", "A rule cannot exclude every definition source"
            )
    if known_topic_ids is None:
        return
    topic_ids = list(
        dict.fromkeys(
            topic for rule in rules for topic in rule.topic_ids if topic not in known_topic_ids
        )
    )
    if topic_ids and set(topic_ids) - await news.live_topic_ids(session, topic_ids, scope=scope):
        raise HighlightRuleError(
            "rule_topic_unknown", "Rule topic_ids must reference your existing topics"
        )


def _match_items(items: Sequence[Any], compiled: Sequence[Any]) -> list[list[Any]]:
    """Match every item against pre-compiled rules (pure CPU; safe to run in a thread)."""
    from modules.dashboard.highlights import match_compiled

    return [match_compiled(item.excerpt, compiled, source_id=item.source_id) for item in items]


async def preview_highlights(
    session: AsyncSession,
    payload: HighlightPreviewRequest,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> HighlightPreviewRead:
    """Dry-run draft rules over recent current evidence: read-only, bounded, no notifications, no egress.

    Uses the same projection reads as the display path, so purged, paused or superseded content is
    never scanned. Nothing is written or committed and ``emit`` is never imported.
    """
    from modules.dashboard.highlights import compile_rules
    from modules.knowledge.documents import public as documents

    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    await validate_highlight_rules(
        session, payload.source_ids, payload.rules, scope=scope, known_topic_ids=None
    )
    since = datetime.now(UTC) - timedelta(days=payload.days)
    # Only active sources visible in this workspace are scanned; foreign or paused ids drop out.
    selected = (
        await sources.get_gadget_sources(
            session,
            tuple(payload.source_ids),
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if payload.source_ids
        else []
    )
    source_ids = tuple(item.id for item in selected if item.status == "active")
    topic_terms = await _rule_topic_terms(
        session,
        payload.rules,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
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
            session,
            source_ids=source_ids,
            limit=PREVIEW_PAGE_SIZE,
            cursor=cursor,
            since=since,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        items = [
            item for item in page.items if not item_scope or str(item.document_id) in item_scope
        ]
        scanned += len(items)
        # Regex work is CPU-bound; keep it off the event loop.
        try:
            per_item = await asyncio.wait_for(
                asyncio.to_thread(_match_items, items, compiled),
                PREVIEW_MATCH_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            raise GatewayTimeoutError(
                detail={
                    "code": "preview_timeout",
                    "message": "Preview took too long",
                    "details": {},
                },
            ) from exc
        for item, item_matches in zip(items, per_item, strict=True):
            for match in item_matches:
                counts[match.rule_id] += 1
                if len(matches) < PREVIEW_MAX_MATCHES:
                    matches.append(
                        DashboardHighlightRead(
                            document_id=item.document_id,
                            document_version_id=item.document_version_id,
                            source_id=item.source_id,
                            title=item.title,
                            observed_at=item.observed_at,
                            rule_id=match.rule_id,
                            matched_keywords=list(match.matched_keywords),
                            severity=match.severity,
                            notify=match.notify,
                            reason=match.reason,
                        )
                    )
        cursor = page.next_cursor
        if cursor is None:
            break
    else:
        truncated = True
    return HighlightPreviewRead(
        window_days=payload.days,
        scanned=scanned,
        truncated=truncated,
        matches=matches,
        rules=[
            HighlightPreviewRuleRead(
                rule_id=rule.id,
                match_count=counts[rule.id],
                unresolved_topic_ids=[
                    topic for topic in rule.topic_ids if topic not in topic_terms
                ],
            )
            for rule in payload.rules
        ],
    )


async def definition_usage(
    session: AsyncSession,
    definition_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[GadgetDefinitionUsageRead] | None:
    """List the actor's dashboards that place this definition (and so evaluate its rules)."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if (
        await session.scalar(
            select(GadgetDefinition.id).where(
                GadgetDefinition.id == definition_id,
                GadgetDefinition.workspace_id == scope.workspace_id,
                GadgetDefinition.owner_id == _actor(scope),
            )
        )
        is None
    ):
        return None
    rows = (
        await session.execute(
            select(Dashboard.id, Dashboard.name, func.count(GadgetInstance.id))
            .join(GadgetInstance, GadgetInstance.dashboard_id == Dashboard.id)
            .where(
                GadgetInstance.definition_id == definition_id,
                Dashboard.workspace_id == scope.workspace_id,
                Dashboard.owner_id == _actor(scope),
            )
            .group_by(Dashboard.id, Dashboard.name)
            .order_by(Dashboard.name, Dashboard.id)
            .limit(MAX_DASHBOARDS_PER_OWNER)
        )
    ).all()
    return [
        GadgetDefinitionUsageRead(dashboard_id=r[0], name=r[1], instance_count=r[2]) for r in rows
    ]
