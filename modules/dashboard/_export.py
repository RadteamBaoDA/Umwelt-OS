"""Dashboard, definition and brief export pages, cursors and fence validation (private to the dashboard package)."""

from __future__ import annotations

import binascii
import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.expression import Exists

from core.errors import (
    ConflictError,
    ValidationFailedError,
)
from core.export_pages import serialize_bounded_page
from core.pagination import b64url_decode, b64url_encode
from core.workspaces.admission import actor_id as _actor
from core.workspaces.schemas import AccessFence, Scope
from modules.dashboard import briefs
from modules.dashboard._common import _admit
from modules.dashboard._reads import _dashboard_read
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
    GadgetDefinition,
    GadgetInstance,
)
from modules.dashboard.schemas import (
    DashboardExportFence,
    DashboardExportPage,
    DashboardExportValidation,
    GadgetDefinitionExport,
    GadgetDefinitionExportFence,
    GadgetDefinitionExportPage,
    GadgetDefinitionExportValidation,
    GadgetFilters,
    GadgetScope,
    HighlightRule,
)


def _encode_dashboard_export_cursor(
    snapshot_at: datetime,
    created_at: datetime,
    identifier: UUID,
    access_fence: AccessFence,
) -> str:
    """Bind dashboard export position to owner scope, dataset, sort and cutoff."""
    raw = json.dumps(
        [
            2,
            "dashboards",
            str(access_fence.workspace_id),
            access_fence.user_id,
            access_fence.membership_revision,
            access_fence.configuration_revision,
            {},
            "created_at_asc_id_asc",
            snapshot_at.isoformat(),
            created_at.isoformat(),
            str(identifier),
        ],
        separators=(",", ":"),
    ).encode()
    return b64url_encode(raw)


def _decode_dashboard_export_cursor(
    cursor: str,
    access_fence: AccessFence,
) -> tuple[datetime, datetime, UUID]:
    """Reject legacy, cross-workspace, stale-admission, or altered dashboard cursors."""
    try:
        if len(cursor) > 1024 or "=" in cursor:
            raise ValueError
        raw = b64url_decode(cursor)
        if b64url_encode(raw) != cursor:
            raise ValueError
        value = json.loads(raw)
        expected_scope = [
            2,
            "dashboards",
            str(access_fence.workspace_id),
            access_fence.user_id,
            access_fence.membership_revision,
            access_fence.configuration_revision,
            {},
            "created_at_asc_id_asc",
        ]
        if not isinstance(value, list) or len(value) != 11 or value[:8] != expected_scope:
            raise ValueError
        snapshot_at, created_at = datetime.fromisoformat(value[8]), datetime.fromisoformat(value[9])
        identifier = UUID(value[10])
        if (
            any(
                item.tzinfo is None or item.utcoffset() is None
                for item in (snapshot_at, created_at)
            )
            or snapshot_at.isoformat() != value[8]
            or created_at.isoformat() != value[9]
            or created_at > snapshot_at
            or snapshot_at > datetime.now(UTC)
            or str(identifier) != value[10]
            or _encode_dashboard_export_cursor(snapshot_at, created_at, identifier, access_fence)
            != cursor
        ):
            raise ValueError
        return snapshot_at, created_at, identifier
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValidationFailedError(detail="Dashboard export cursor is invalid") from exc


def _encode_owner_export_cursor(
    record_kind: str,
    snapshot_at: datetime,
    position_at: datetime,
    identifier: UUID,
    access_fence: AccessFence,
) -> str:
    """Bind a portable owner keyset position to its dataset and admitted scope."""
    sort = "generated_at_asc_id_asc" if record_kind == "daily_briefs" else "created_at_asc_id_asc"
    raw = json.dumps(
        [
            2,
            record_kind,
            str(access_fence.workspace_id),
            access_fence.user_id,
            access_fence.membership_revision,
            access_fence.configuration_revision,
            {},
            sort,
            snapshot_at.isoformat(),
            position_at.isoformat(),
            str(identifier),
        ],
        separators=(",", ":"),
    ).encode("utf-8")
    return b64url_encode(raw)


def _decode_owner_export_cursor(
    cursor: str,
    record_kind: str,
    access_fence: AccessFence,
) -> tuple[datetime, datetime, UUID]:
    """Reject legacy, cross-workspace, stale-admission, or altered owner cursors."""
    try:
        if len(cursor) > 1024 or "=" in cursor:
            raise ValueError
        raw = b64url_decode(cursor)
        if b64url_encode(raw) != cursor:
            raise ValueError
        sort = (
            "generated_at_asc_id_asc" if record_kind == "daily_briefs" else "created_at_asc_id_asc"
        )
        expected_scope = [
            2,
            record_kind,
            str(access_fence.workspace_id),
            access_fence.user_id,
            access_fence.membership_revision,
            access_fence.configuration_revision,
            {},
            sort,
        ]
        value = json.loads(raw)
        if not isinstance(value, list) or len(value) != 11 or value[:8] != expected_scope:
            raise ValueError
        snapshot_at, position_at = (
            datetime.fromisoformat(value[8]),
            datetime.fromisoformat(value[9]),
        )
        identifier = UUID(value[10])
        if (
            any(
                item.tzinfo is None or item.utcoffset() is None
                for item in (snapshot_at, position_at)
            )
            or snapshot_at.isoformat() != value[8]
            or position_at.isoformat() != value[9]
            or position_at > snapshot_at
            or snapshot_at > datetime.now(UTC)
            or str(identifier) != value[10]
            or _encode_owner_export_cursor(
                record_kind, snapshot_at, position_at, identifier, access_fence
            )
            != cursor
        ):
            raise ValueError
        return snapshot_at, position_at, identifier
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValidationFailedError(detail="Owner export cursor is invalid") from exc


def _dashboard_export_scope(
    owner_id: int, snapshot_at: datetime, workspace_id: UUID
) -> tuple[ColumnElement[bool], ...]:
    """Select workspace-local parent dashboard revisions that existed unchanged at the cutoff."""
    return (
        Dashboard.workspace_id == workspace_id,
        Dashboard.owner_id == owner_id,
        Dashboard.created_at <= snapshot_at,
        Dashboard.updated_at <= snapshot_at,
    )


def _definition_export_scope(
    owner_id: int, snapshot_at: datetime, workspace_id: UUID
) -> tuple[ColumnElement[bool], ...]:
    """Select the complete set of saved owner definitions created by the cutoff."""
    return (
        GadgetDefinition.workspace_id == workspace_id,
        GadgetDefinition.owner_id == owner_id,
        GadgetDefinition.created_at <= snapshot_at,
    )


def _definition_export_read(row: GadgetDefinition) -> GadgetDefinitionExport:
    """Project persisted selectors only, excluding placements, live warnings and renderer output."""
    return GadgetDefinitionExport(
        id=row.id,
        name=row.name,
        revision=row.revision,
        renderer=row.renderer,
        source_ids=[UUID(str(item)) for item in row.source_ids],
        scope=GadgetScope.model_validate(row.scope),
        filters=GadgetFilters.model_validate(row.filters),
        highlight_rules=[HighlightRule.model_validate(item) for item in row.highlight_rules],
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _definition_export_row_digest(row: GadgetDefinition) -> str:
    """Hash the exact persisted selector fields and both revision timestamps for final fencing."""
    value = {
        "id": str(row.id),
        "name": row.name,
        "revision": row.revision,
        "renderer": row.renderer,
        "source_ids": row.source_ids,
        "scope": row.scope,
        "filters": row.filters,
        "highlight_rules": row.highlight_rules,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


async def _definition_export_page(
    session: AsyncSession,
    *,
    owner_id: int,
    limit: int,
    cursor: str | None,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> GadgetDefinitionExportPage:
    """Page all definitions, including unplaced; omit changed-after-cutoff configs and fence them."""
    access_fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if owner_id != _actor(scope) or not 1 <= limit <= 100:
        raise ValueError("Gadget definition export owner or page limit is invalid")
    if cursor is None:
        snapshot_at, position = datetime.now(UTC), None
    else:
        snapshot_at, position_at, position_id = _decode_owner_export_cursor(
            cursor, "gadget_definitions", access_fence
        )
        position = (position_at, position_id)
    predicates = _definition_export_scope(owner_id, snapshot_at, scope.workspace_id)
    snapshot_count = int(
        await session.scalar(select(func.count()).select_from(GadgetDefinition).where(*predicates))
        or 0
    )
    statement = select(GadgetDefinition).where(*predicates)
    if position is not None:
        from sqlalchemy import tuple_

        statement = statement.where(
            tuple_(GadgetDefinition.created_at, GadgetDefinition.id) > position
        )
    rows = list(
        (
            await session.scalars(
                statement.order_by(GadgetDefinition.created_at, GadgetDefinition.id)
                .limit(limit + 1)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    has_more, rows = len(rows) > limit, rows[:limit]
    eligible_rows = [row for row in rows if row.updated_at <= snapshot_at]
    items = [_definition_export_read(row) for row in eligible_rows]
    _encoded, payload_bytes = serialize_bounded_page(items, label="Gadget definition")
    fences = [
        GadgetDefinitionExportFence(
            id=row.id,
            created_at=row.created_at,
            updated_at=row.updated_at,
            revision=row.revision,
            content_digest=_definition_export_row_digest(row),
            eligible=row.updated_at <= snapshot_at,
        )
        for row in rows
    ]
    return GadgetDefinitionExportPage(
        owner_id=owner_id,
        record_kind="gadget_definitions",
        snapshot_at=snapshot_at,
        snapshot_count=snapshot_count,
        omitted_count=len(rows) - len(eligible_rows),
        items=items,
        fences=fences,
        payload_bytes=payload_bytes,
        available=True,
        next_cursor=_encode_owner_export_cursor(
            "gadget_definitions", snapshot_at, rows[-1].created_at, rows[-1].id, access_fence
        )
        if has_more and rows
        else None,
        omission_reason="definition_changed_after_snapshot"
        if len(rows) != len(eligible_rows)
        else None,
    )


async def _definition_export_validation(
    session: AsyncSession,
    *,
    owner_id: int,
    snapshot_at: datetime,
    expected_snapshot_count: int,
    fences: list[GadgetDefinitionExportFence],
    scope: Scope,
    multi_workspace_enabled: bool,
) -> GadgetDefinitionExportValidation:
    """Recheck the definition cutoff inventory and exact saved-selector digest before publication."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if owner_id != _actor(scope) or len(fences) > 100:
        raise ValueError("Gadget definition export validation input is invalid")
    predicates = _definition_export_scope(owner_id, snapshot_at, scope.workspace_id)
    observed = int(
        await session.scalar(select(func.count()).select_from(GadgetDefinition).where(*predicates))
        or 0
    )
    if observed != expected_snapshot_count:
        return GadgetDefinitionExportValidation(
            valid=False,
            reason="snapshot_count_changed",
            observed_snapshot_count=observed,
        )
    for fence in fences:
        row = await session.scalar(
            select(GadgetDefinition)
            .where(
                GadgetDefinition.id == fence.id,
                *predicates,
            )
            .execution_options(populate_existing=True)
        )
        if row is None:
            return GadgetDefinitionExportValidation(
                valid=False,
                reason="record_changed",
                observed_snapshot_count=observed,
            )
        eligible = row.updated_at <= snapshot_at
        if (
            row.created_at != fence.created_at
            or row.updated_at != fence.updated_at
            or row.revision != fence.revision
            or eligible != fence.eligible
            or _definition_export_row_digest(row) != fence.content_digest
        ):
            return GadgetDefinitionExportValidation(
                valid=False,
                reason="record_changed",
                observed_snapshot_count=observed,
            )
    return GadgetDefinitionExportValidation(
        valid=True, reason="valid", observed_snapshot_count=observed
    )


def _newer_export_definition_exists(
    owner_id: int, snapshot_at: datetime, workspace_id: UUID
) -> Exists:
    """Find child definitions updated after the cutoff but used by a retained dashboard."""
    return (
        select(GadgetInstance.id)
        .join(
            GadgetDefinition,
            GadgetDefinition.id == GadgetInstance.definition_id,
        )
        .where(
            GadgetInstance.dashboard_id == Dashboard.id,
            GadgetDefinition.workspace_id == workspace_id,
            GadgetDefinition.owner_id == owner_id,
            GadgetDefinition.updated_at > snapshot_at,
        )
        .correlate(Dashboard)
        .exists()
    )


async def _brief_export_eligibility(
    session: AsyncSession,
    row: DailyBrief,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> bool:
    """Allow export only when captured prompt lineage remains structurally and currently valid.

    Historical independent facts remain exportable; current story/event support is
    revalidated by the manifest checker without comparing unrelated dashboard facts.
    """
    citations = _brief_export_citations(row)
    return bool(
        citations is not None
        and row.status == "current"
        and await briefs._captured_inputs_match(
            session, row, lock=False, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
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
        if (
            type(reference) is not int
            or not 1 <= reference <= 40
            or reference in seen_references
            or not isinstance(kind, str)
            or not isinstance(identifier, str)
            or not isinstance(title, str)
            or not isinstance(source_ids, list)
            or any(not isinstance(value, str) for value in source_ids)
        ):
            return None
        seen_references.add(reference)
        citations.append(
            {
                "ref": reference,
                "kind": kind,
                "id": identifier,
                "title": title,
                "source_ids": list(source_ids),
            }
        )
    return citations


def _brief_export_row_digest(row: DailyBrief) -> str:
    """Hash only persisted brief fields intentionally eligible for portable projection."""
    value = {
        "id": str(row.id),
        "brief_date": row.brief_date.isoformat(),
        "timezone": row.timezone,
        "revision": row.revision,
        "status": row.status,
        "content": row.content,
        "citations": row.citations,
        "model_alias": row.model_alias,
        "generated_at": row.generated_at.isoformat(),
        "evidence_capture_version": row.evidence_capture_version,
        "evidence_capture_status": row.evidence_capture_status,
        "evidence_fact_count": row.evidence_fact_count,
    }
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


async def _brief_export_page(
    session: AsyncSession,
    *,
    owner_id: int,
    limit: int,
    cursor: str | None,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> BriefExportPage:
    """Page every saved revision at one cutoff and omit text whose exact live citation set fails."""
    access_fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if owner_id != _actor(scope) or not 1 <= limit <= 100:
        raise ValueError("Daily brief export owner or page limit is invalid")
    if cursor is None:
        snapshot_at, position = datetime.now(UTC), None
    else:
        snapshot_at, position_at, position_id = _decode_owner_export_cursor(
            cursor, "daily_briefs", access_fence
        )
        position = (position_at, position_id)
    predicates = (
        DailyBrief.workspace_id == scope.workspace_id,
        DailyBrief.owner_id == owner_id,
        DailyBrief.generated_at <= snapshot_at,
    )
    snapshot_count = int(
        await session.scalar(select(func.count()).select_from(DailyBrief).where(*predicates)) or 0
    )
    statement = select(DailyBrief).where(*predicates)
    if position is not None:
        from sqlalchemy import tuple_

        statement = statement.where(tuple_(DailyBrief.generated_at, DailyBrief.id) > position)
    rows = list(
        (
            await session.scalars(
                statement.order_by(DailyBrief.generated_at, DailyBrief.id)
                .limit(limit + 1)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    has_more, rows = len(rows) > limit, rows[:limit]
    items: list[DailyBriefExport] = []
    fences: list[BriefExportFence] = []
    omitted_count = 0
    # Lock-free eligibility: export spans many pages in one transaction, so it must not accumulate
    # locks; `_brief_export_validation` rejects anything that changed after this scan.
    for row in rows:
        eligible = await _brief_export_eligibility(
            session, row, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        fences.append(
            BriefExportFence(
                id=row.id,
                generated_at=row.generated_at,
                revision=row.revision,
                content_digest=_brief_export_row_digest(row),
                eligible=eligible,
            )
        )
        if not eligible:
            omitted_count += 1
            continue
        citations = _brief_export_citations(row)
        if citations is None:
            raise ConflictError(detail="Daily brief changed during export; retry the download")
        items.append(
            DailyBriefExport(
                id=row.id,
                brief_date=row.brief_date,
                timezone=row.timezone,
                revision=row.revision,
                status="current",
                content=row.content,
                citations=citations,
                model_alias=row.model_alias,
                generated_at=row.generated_at,
            )
        )
    _encoded, payload_bytes = serialize_bounded_page(items, label="Daily brief")
    return BriefExportPage(
        owner_id=owner_id,
        record_kind="daily_briefs",
        snapshot_at=snapshot_at,
        snapshot_count=snapshot_count,
        omitted_count=omitted_count,
        items=items,
        fences=fences,
        payload_bytes=payload_bytes,
        next_cursor=_encode_owner_export_cursor(
            "daily_briefs", snapshot_at, rows[-1].generated_at, rows[-1].id, access_fence
        )
        if has_more and rows
        else None,
        omission_reason="unsupported_or_deleted_citation" if omitted_count else None,
    )


async def _brief_export_validation(
    session: AsyncSession,
    *,
    owner_id: int,
    snapshot_at: datetime,
    expected_snapshot_count: int,
    fences: list[BriefExportFence],
    scope: Scope,
    multi_workspace_enabled: bool,
) -> BriefExportValidation:
    """Recheck all scanned revisions, including omitted rows, so deletion or support changes abort."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if owner_id != _actor(scope) or len(fences) > 100:
        raise ValueError("Daily brief export validation input is invalid")
    predicates = (
        DailyBrief.workspace_id == scope.workspace_id,
        DailyBrief.owner_id == owner_id,
        DailyBrief.generated_at <= snapshot_at,
    )
    observed = int(
        await session.scalar(select(func.count()).select_from(DailyBrief).where(*predicates)) or 0
    )
    if observed != expected_snapshot_count:
        return BriefExportValidation(
            valid=False, reason="snapshot_count_changed", observed_snapshot_count=observed
        )
    for fence in fences:
        row = await session.scalar(
            select(DailyBrief)
            .where(
                DailyBrief.id == fence.id,
                *predicates,
            )
            .execution_options(populate_existing=True)
        )
        if row is None:
            return BriefExportValidation(
                valid=False, reason="record_changed", observed_snapshot_count=observed
            )
        eligible = await _brief_export_eligibility(
            session, row, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        if (
            row.generated_at != fence.generated_at
            or row.revision != fence.revision
            or eligible != fence.eligible
            or _brief_export_row_digest(row) != fence.content_digest
        ):
            return BriefExportValidation(
                valid=False, reason="record_changed", observed_snapshot_count=observed
            )
    return BriefExportValidation(valid=True, reason="valid", observed_snapshot_count=observed)


def _brief_schedule_export_value(row: BriefScheduleRow | None) -> BriefScheduleExport:
    """Project the saved/default editable time without its private scheduler ownership fields."""
    schedule = BriefSchedule.model_validate(row) if row is not None else BriefSchedule()
    return BriefScheduleExport.model_validate(schedule.model_dump())


def _brief_schedule_export_fence(row: BriefScheduleRow | None) -> BriefScheduleExportFence:
    """Digest the safe schedule projection together with its persisted-row identity and timestamp."""
    value = _brief_schedule_export_value(row)
    updated_at = row.updated_at if row is not None else None
    data = {
        "persisted": row is not None,
        "updated_at": updated_at.isoformat() if updated_at else None,
        "schedule": value.model_dump(mode="json"),
    }
    return BriefScheduleExportFence(
        persisted=row is not None,
        updated_at=updated_at,
        content_digest=hashlib.sha256(
            json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )


async def _brief_schedule_export_page(
    session: AsyncSession,
    *,
    owner_id: int,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> BriefScheduleExportPage:
    """Return the default or persisted owner schedule and reject updates racing the cutoff."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if owner_id != _actor(scope):
        raise ValueError("Brief schedule export owner is invalid")
    snapshot_at = datetime.now(UTC)
    row = await session.scalar(
        select(BriefScheduleRow).where(
            BriefScheduleRow.workspace_id == scope.workspace_id,
            BriefScheduleRow.owner_id == owner_id,
        )
    )
    if row is not None and row.updated_at > snapshot_at:
        raise ConflictError(detail="Brief schedule changed during export; retry the download")
    item = _brief_schedule_export_value(row)
    payload_bytes = 2 + len(item.model_dump_json().encode("utf-8"))
    return BriefScheduleExportPage(
        owner_id=owner_id,
        record_kind="brief_schedule",
        snapshot_at=snapshot_at,
        snapshot_count=1,
        items=[item],
        fences=[_brief_schedule_export_fence(row)],
        payload_bytes=payload_bytes,
        next_cursor=None,
        available=True,
        omission_reason=None,
    )


async def _brief_schedule_export_validation(
    session: AsyncSession,
    *,
    owner_id: int,
    snapshot_at: datetime,
    expected_snapshot_count: int,
    fences: list[BriefScheduleExportFence],
    scope: Scope,
    multi_workspace_enabled: bool,
) -> BriefScheduleExportValidation:
    """Compare persisted/default schedule state immediately before the aggregate download is returned."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if owner_id != _actor(scope) or expected_snapshot_count != 1 or len(fences) != 1:
        raise ValueError("Brief schedule export validation input is invalid")
    row = await session.scalar(
        select(BriefScheduleRow)
        .where(
            BriefScheduleRow.workspace_id == scope.workspace_id,
            BriefScheduleRow.owner_id == owner_id,
        )
        .execution_options(populate_existing=True)
    )
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
        valid=valid,
        reason="valid" if valid else "record_changed",
        observed_snapshot_count=observed,
    )


async def export_page(
    session: AsyncSession,
    *,
    owner_id: int,
    record_kind: str,
    limit: int = 50,
    cursor: str | None = None,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> DashboardExportPage | GadgetDefinitionExportPage | BriefExportPage | BriefScheduleExportPage:
    """Return one cutoff-bound dashboard, definition, retained-brief or schedule page.

    Each dataset is owner-scoped and size-bounded. Dashboard placements exclude newer unretained
    definition revisions; definitions include unplaced saved selectors; brief prose is emitted only
    when every actual fact kind/ID/title/source citation remains currently eligible; schedule output
    excludes automation ownership identifiers. The aggregate caller must revalidate returned fences.
    """
    access_fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if owner_id != _actor(scope):
        raise ValueError("Dashboard export actor does not match the admitted workspace")
    if record_kind == "gadget_definitions":
        return await _definition_export_page(
            session,
            owner_id=owner_id,
            limit=limit,
            cursor=cursor,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    if record_kind == "daily_briefs":
        return await _brief_export_page(
            session,
            owner_id=owner_id,
            limit=limit,
            cursor=cursor,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    if record_kind == "brief_schedule":
        if cursor is not None:
            raise ValueError("Brief schedule export does not accept a cursor")
        return await _brief_schedule_export_page(
            session, owner_id=owner_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
    if record_kind != "dashboards" or not 1 <= limit <= 100:
        raise ValueError("Dashboard export owner, kind or page limit is invalid")
    if cursor is None:
        snapshot_at, position = datetime.now(UTC), None
    else:
        snapshot_at, position_at, position_id = _decode_dashboard_export_cursor(
            cursor, access_fence
        )
        position = (position_at, position_id)
    predicates = _dashboard_export_scope(owner_id, snapshot_at, scope.workspace_id)
    snapshot_count = int(
        await session.scalar(select(func.count()).select_from(Dashboard).where(*predicates)) or 0
    )
    # Gadget definitions have independent revisions. If one changed after the owner cutoff,
    # omit the whole dataset because the prior definition version is not retained here.
    if (
        await session.scalar(
            select(Dashboard.id)
            .where(
                *predicates,
                _newer_export_definition_exists(owner_id, snapshot_at, scope.workspace_id),
            )
            .limit(1)
        )
        is not None
    ):
        return DashboardExportPage(
            owner_id=owner_id,
            record_kind="dashboards",
            snapshot_at=snapshot_at,
            snapshot_count=snapshot_count,
            items=[],
            fences=[],
            payload_bytes=2,
            available=False,
            omission_reason="definition_changed_after_snapshot",
        )
    statement = select(Dashboard).where(*predicates)
    if position is not None:
        from sqlalchemy import tuple_

        statement = statement.where(tuple_(Dashboard.created_at, Dashboard.id) > position)
    rows = list(
        (
            await session.scalars(
                statement.order_by(Dashboard.created_at, Dashboard.id)
                .limit(limit + 1)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    has_more, rows = len(rows) > limit, rows[:limit]
    items = [
        await _dashboard_read(
            session, row, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        for row in rows
    ]
    encoded, payload_bytes = serialize_bounded_page(items, label="Dashboard")
    fences = [
        DashboardExportFence(
            id=row.id,
            created_at=row.created_at,
            updated_at=row.updated_at,
            revision=row.revision,
            content_digest=hashlib.sha256(raw).hexdigest(),
        )
        for row, raw in zip(rows, encoded, strict=True)
    ]
    return DashboardExportPage(
        owner_id=owner_id,
        record_kind="dashboards",
        snapshot_at=snapshot_at,
        snapshot_count=snapshot_count,
        items=items,
        fences=fences,
        payload_bytes=payload_bytes,
        next_cursor=_encode_dashboard_export_cursor(
            snapshot_at, rows[-1].created_at, rows[-1].id, access_fence
        )
        if has_more and rows
        else None,
    )


async def validate_export_fences(
    session: AsyncSession,
    *,
    owner_id: int,
    record_kind: str,
    snapshot_at: datetime,
    expected_snapshot_count: int,
    fences: list[DashboardExportFence],
    scope: Scope,
    multi_workspace_enabled: bool,
) -> (
    DashboardExportValidation
    | GadgetDefinitionExportValidation
    | BriefExportValidation
    | BriefScheduleExportValidation
):
    """Recheck the cutoff inventory and exact public projection/evidence fences for one dataset.

    The export aggregator calls this only after rendering, immediately before constructing the
    response. Bounds cap each fence batch; invalid owner, dataset or changed evidence fails closed.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if owner_id != _actor(scope):
        raise ValueError("Dashboard export actor does not match the admitted workspace")
    if record_kind == "gadget_definitions":
        return await _definition_export_validation(
            session,
            owner_id=owner_id,
            snapshot_at=snapshot_at,
            expected_snapshot_count=expected_snapshot_count,
            fences=fences,  # type: ignore[arg-type]
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    if record_kind == "daily_briefs":
        return await _brief_export_validation(
            session,
            owner_id=owner_id,
            snapshot_at=snapshot_at,
            expected_snapshot_count=expected_snapshot_count,
            fences=fences,  # type: ignore[arg-type]
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    if record_kind == "brief_schedule":
        return await _brief_schedule_export_validation(
            session,
            owner_id=owner_id,
            snapshot_at=snapshot_at,
            expected_snapshot_count=expected_snapshot_count,
            fences=fences,  # type: ignore[arg-type]
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    if record_kind != "dashboards" or len(fences) > 100:
        raise ValueError("Dashboard export validation input is invalid")
    observed = int(
        await session.scalar(
            select(func.count())
            .select_from(Dashboard)
            .where(*_dashboard_export_scope(owner_id, snapshot_at, scope.workspace_id))
        )
        or 0
    )
    if observed != expected_snapshot_count:
        return DashboardExportValidation(
            valid=False, reason="snapshot_count_changed", observed_snapshot_count=observed
        )
    if (
        fences
        and await session.scalar(
            select(Dashboard.id)
            .where(
                *_dashboard_export_scope(owner_id, snapshot_at, scope.workspace_id),
                _newer_export_definition_exists(owner_id, snapshot_at, scope.workspace_id),
            )
            .limit(1)
        )
        is not None
    ):
        return DashboardExportValidation(
            valid=False, reason="record_changed", observed_snapshot_count=observed
        )
    for fence in fences:
        row = await session.scalar(
            select(Dashboard)
            .where(
                Dashboard.id == fence.id,
                *_dashboard_export_scope(owner_id, snapshot_at, scope.workspace_id),
            )
            .execution_options(populate_existing=True)
        )
        if row is None:
            return DashboardExportValidation(
                valid=False, reason="record_changed", observed_snapshot_count=observed
            )
        item = await _dashboard_read(
            session, row, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        if (
            item.created_at != fence.created_at
            or item.updated_at != fence.updated_at
            or item.revision != fence.revision
            or hashlib.sha256(item.model_dump_json().encode("utf-8")).hexdigest()
            != fence.content_digest
        ):
            return DashboardExportValidation(
                valid=False, reason="record_changed", observed_snapshot_count=observed
            )
    return DashboardExportValidation(valid=True, reason="valid", observed_snapshot_count=observed)
