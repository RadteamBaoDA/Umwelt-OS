"""Member-visible brief lineage: share eligibility, access projection and shared reads.

A brief is shareable only when every fact is a story with document-backed support, the full
captured prompt is still current, and the recipient holds an active grant on every dependency
document. Members see a brief only while its own grant (bound to the saved revision) and every
dependency grant stay active. Persistence is read through ``briefs``/``models``; nothing here
writes or edits the P14 three-phase generation path.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces import public as workspaces
from core.workspaces.schemas import GrantRef, ResourceAccessProjection, Scope, WorkspaceContext
from modules.dashboard import briefs
from modules.dashboard.daily_schemas import BriefRead
from modules.dashboard.models import DailyBrief, DailyBriefEvidence
from modules.knowledge.documents import public as documents
from modules.translations.schemas import TranslationInput

_PAGE = 50


@dataclass(frozen=True, slots=True)
class SharedBrief:
    """One member-visible brief and the grants its publication must hold."""

    brief: BriefRead
    grants: tuple[GrantRef, ...]


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Brief not found")


async def _story_documents(session: AsyncSession, brief_id: UUID) -> frozenset[UUID] | None:
    """Return dependency document IDs, or None when any fact is not a document-backed story."""
    rows = (await session.execute(select(
        DailyBriefEvidence.fact_ref, DailyBriefEvidence.fact_kind, DailyBriefEvidence.document_id,
    ).where(DailyBriefEvidence.brief_id == brief_id).limit(4_041))).all()
    if not rows or any(kind != "stories" or doc is None for _, kind, doc in rows):
        return None
    return frozenset(doc for _, _, doc in rows if doc is not None)


async def _is_current(
    session: AsyncSession, row: DailyBrief, *, multi_workspace_enabled: bool,
) -> bool:
    """Revalidate the captured manifest without locks, under the workspace owner's own scope."""
    owner = await workspaces.resolve_workspace_context(session, row.owner_id, row.workspace_id)
    if owner is None or owner.role != "owner":
        return False
    try:
        return await briefs._captured_inputs_match(
            session, row, scope=owner, multi_workspace_enabled=multi_workspace_enabled, lock=False,
        )
    except (briefs.BriefUnavailable, ValueError, TypeError, KeyError):
        return False


async def _row(session: AsyncSession, brief_id: UUID, workspace_id: UUID) -> DailyBrief | None:
    return await session.scalar(select(DailyBrief).where(
        DailyBrief.id == brief_id, DailyBrief.workspace_id == workspace_id,
    ).execution_options(populate_existing=True))


async def read_brief_access_projection(
    session: AsyncSession, resource_id: UUID, *, scope: Scope, multi_workspace_enabled: bool = False,
) -> ResourceAccessProjection | None:
    """Return the brief's revision and whether it is currently shareable lineage, else None."""
    row = await _row(session, resource_id, scope.workspace_id)
    if row is None:
        return None
    available = (
        await _story_documents(session, row.id) is not None
        and await _is_current(session, row, multi_workspace_enabled=multi_workspace_enabled)
    )
    return ResourceAccessProjection(
        workspace_id=row.workspace_id, resource_id=row.id, resource_revision=row.revision,
        available=available,
    )


async def check_brief_shareable(
    session: AsyncSession, brief_id: UUID, *, scope: WorkspaceContext, member_user_id: int,
    multi_workspace_enabled: bool = False,
) -> None:
    """404 invisible; 409 brief_not_shareable; 409 brief_evidence_not_shared (owner-visible IDs)."""
    if scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")
    row = await _row(session, brief_id, scope.workspace_id)
    if row is None or row.owner_id != scope.user_id:
        raise _not_found()
    docs = await _story_documents(session, row.id)
    if (docs is None or len(docs) > briefs.MAX_BRIEF_EVIDENCE
            or not await _is_current(session, row, multi_workspace_enabled=multi_workspace_enabled)):
        raise HTTPException(status_code=409, detail="brief_not_shareable")
    granted = await workspaces.active_grant_ids(
        session, workspace_id=scope.workspace_id, member_user_id=member_user_id,
        kind="document", resource_ids=tuple(sorted(docs, key=str)),
    )
    missing = sorted(docs - granted, key=str)
    if missing:
        visible = await documents.existing_document_ids(
            session, missing, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        raise HTTPException(status_code=409, detail={
            "code": "brief_evidence_not_shared", "document_ids": [str(v) for v in visible],
        })


async def _member_grants(
    session: AsyncSession, row: DailyBrief, *, scope: WorkspaceContext, multi_workspace_enabled: bool,
) -> tuple[GrantRef, ...] | None:
    """Return the brief grant plus every dependency grant, or None if any link is missing/stale."""
    own = await workspaces.read_resource_grants(
        session, scope=scope, kind="brief", resource_ids=(row.id,),
    )
    if len(own) != 1 or own[0].resource_revision != row.revision:
        return None
    docs = await _story_documents(session, row.id)
    if docs is None or len(docs) > briefs.MAX_BRIEF_EVIDENCE:
        return None
    deps = await workspaces.read_resource_grants(
        session, scope=scope, kind="document", resource_ids=tuple(sorted(docs, key=str)),
    )
    if {g.resource_id for g in deps} != docs or len(deps) != len(docs):
        return None
    if not await _is_current(session, row, multi_workspace_enabled=multi_workspace_enabled):
        return None
    return (*own, *deps)


def _read(row: DailyBrief) -> BriefRead:
    return BriefRead.model_validate(row).model_copy(
        update={"status": "current", "lineage_status": "captured"},
    )


async def list_shared_briefs(
    session: AsyncSession, day: date, timezone: str, *, scope: WorkspaceContext,
    multi_workspace_enabled: bool,
) -> list[SharedBrief]:
    """List a member's currently admissible shared revisions for the day, newest first."""
    await workspaces.read_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    rows = (await session.scalars(select(DailyBrief).where(
        DailyBrief.workspace_id == scope.workspace_id, DailyBrief.brief_date == day,
        DailyBrief.timezone == timezone,
        DailyBrief.id.in_(workspaces.granted_resource_ids(scope=scope, kind="brief")),
    ).order_by(DailyBrief.revision.desc()).limit(_PAGE))).all()
    result: list[SharedBrief] = []
    for row in rows:
        grants = await _member_grants(
            session, row, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        if grants is not None:
            result.append(SharedBrief(_read(row), grants))
    return result


async def read_shared_brief(
    session: AsyncSession, brief_id: UUID, *, scope: WorkspaceContext, multi_workspace_enabled: bool,
) -> SharedBrief | None:
    """Read one brief for a member (all grants required) or an owner; None means identical 404."""
    await workspaces.read_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    row = await _row(session, brief_id, scope.workspace_id)
    if row is None:
        return None
    if scope.role == "owner":
        if row.owner_id != scope.user_id:
            return None
        (item,) = await briefs._with_live_status(
            session, [row], scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        return SharedBrief(item, ())
    grants = await _member_grants(
        session, row, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    return None if grants is None else SharedBrief(_read(row), grants)


async def read_brief_translation_input(
    session: AsyncSession, *, scope: WorkspaceContext, brief_id: UUID, multi_workspace_enabled: bool,
) -> TranslationInput | None:
    """Return the brief text and a dependency-bound visibility hash, or None when not visible."""
    shared = await read_shared_brief(
        session, brief_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if shared is None or shared.brief.lineage_status != "captured" or not shared.brief.content:
        return None
    evidence = (await session.execute(select(
        DailyBriefEvidence.document_id, DailyBriefEvidence.document_version_id,
        DailyBriefEvidence.chunk_id, DailyBriefEvidence.source_id,
    ).where(DailyBriefEvidence.brief_id == brief_id, DailyBriefEvidence.document_id.is_not(None))
    .order_by(DailyBriefEvidence.fact_ref, DailyBriefEvidence.support_index))).all()
    sources = tuple(sorted({row[3] for row in evidence if row[3] is not None}, key=str))
    tuples = sorted(
        [str(brief_id), str(shared.brief.revision), "evidence", *map(str, row)] for row in evidence
    ) + sorted(
        ["grant", g.resource_type, str(g.resource_id), str(g.share_revision), str(g.resource_revision)]
        for g in shared.grants
    )
    digest = hashlib.sha256(json.dumps(tuples, separators=(",", ":")).encode()).hexdigest()
    return TranslationInput(
        workspace_id=scope.workspace_id, actor_user_id=scope.user_id, resource_type="daily_brief",
        resource_id=brief_id, resource_revision=str(shared.brief.revision),
        fields={"content": shared.brief.content}, visibility_hash=digest,
        source_ids=sources, local_only=False,
    )
