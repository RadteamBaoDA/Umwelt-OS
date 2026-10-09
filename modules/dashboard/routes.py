"""Protected dashboard configuration REST routes with explicit owner and write dependencies."""

import time
from collections.abc import Awaitable
from datetime import date
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_owner, require_owner_write
from core.auth.models import AuthSession
from core.database import get_session
from core.workspaces.dependencies import require_workspace_read, require_workspace_write
from core.workspaces.schemas import WorkspaceContext
from modules.dashboard import briefs, context, public
from modules.dashboard.daily_schemas import (
    BriefGenerateRequest,
    BriefRead,
    BriefSchedule,
    DailyContext,
    validate_timezone,
)
from modules.dashboard.public import DashboardConflict, DashboardMissing
from modules.dashboard.schemas import (
    DashboardCreate,
    DashboardDetail,
    DashboardGroupRead,
    DashboardHighlightRead,
    DashboardPatch,
    DashboardPresetRead,
    DashboardSummary,
    GadgetDefinitionCreate,
    GadgetDefinitionPatch,
    GadgetDefinitionRead,
    GadgetDefinitionUsageRead,
    GroupCreate,
    GroupPatch,
    HighlightPreviewRead,
    HighlightPreviewRequest,
    InstanceCreate,
    InstancePatch,
    LayoutReplace,
    PresetApplyRequest,
    PresetPreviewRead,
    PresetPreviewRequest,
    RendererRead,
)
from modules.settings.public import module_dependency
from modules.sources.schemas import GadgetSourceSelectionPage

router = APIRouter(prefix="/api/v1", tags=["dashboard"], dependencies=[Depends(module_dependency("dashboard"))])
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[AuthSession, Depends(require_owner)]
OwnerWrite = Annotated[AuthSession, Depends(require_owner_write)]
WorkspaceRead = Annotated[WorkspaceContext, Depends(require_workspace_read)]
WorkspaceWrite = Annotated[WorkspaceContext, Depends(require_workspace_write)]


def _no_store(response: Response) -> None:
    """Prevent private configuration and lifecycle metadata from entering shared caches."""
    response.headers["Cache-Control"] = "private, no-store"


async def _call[T](operation: Awaitable[T]) -> T:
    """Map public-owner errors to stable HTTP status and machine codes without exposing ORM details."""
    try:
        return await operation
    except DashboardMissing as exc:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Resource not found", "details": {}}) from exc
    except DashboardConflict as exc:
        details = {"current_revision": exc.current_revision} if exc.current_revision is not None else {}
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc), "details": details}) from exc
    except (ValueError, KeyError) as exc:
        message = "Unknown preset or renderer" if isinstance(exc, KeyError) else str(exc)
        code = getattr(exc, "code", "invalid_dashboard_configuration")
        raise HTTPException(status_code=422, detail={"code": code, "message": message, "details": {}}) from exc


@router.get("/dashboards", response_model=list[DashboardSummary])
async def list_dashboards(session: Session, owner: OwnerRead, scope: WorkspaceRead, request: Request, response: Response) -> list[DashboardSummary]:
    """List the authenticated owner's dashboard summaries without accepting browser ownership."""
    _no_store(response)
    return await public.list_dashboards(session, scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)


@router.post("/dashboards", status_code=201, response_model=DashboardDetail)
async def create_dashboard(payload: DashboardCreate, session: Session, owner: OwnerWrite, scope: WorkspaceWrite, request: Request, response: Response) -> DashboardDetail:
    """Create an owner dashboard only after Origin, session, and CSRF write checks pass."""
    _no_store(response)
    return await _call(public.create_dashboard(session, payload, scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled))


@router.get("/dashboards/{dashboard_id}", response_model=DashboardDetail)
async def get_dashboard(dashboard_id: UUID, session: Session, owner: OwnerRead, scope: WorkspaceRead, request: Request, response: Response) -> DashboardDetail:
    """Read saved configuration and both layouts, returning 404 for absent or foreign dashboards."""
    _no_store(response)
    result = await public.get_dashboard(session, dashboard_id, scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    if result is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Dashboard not found", "details": {}})
    return result


@router.patch("/dashboards/{dashboard_id}", response_model=DashboardDetail)
async def patch_dashboard(dashboard_id: UUID, payload: DashboardPatch, session: Session, owner: OwnerWrite, scope: WorkspaceWrite, request: Request, response: Response) -> DashboardDetail:
    """Rename a dashboard under CSRF protection and its shared expected revision."""
    _no_store(response)
    return await _call(public.patch_dashboard(session, dashboard_id, payload, scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled))


@router.delete("/dashboards/{dashboard_id}", status_code=204)
async def delete_dashboard(dashboard_id: UUID, expected_revision: Annotated[int, Query(ge=1, le=9_007_199_254_740_991)], session: Session, owner: OwnerWrite, scope: WorkspaceWrite, request: Request, response: Response) -> Response:
    """Delete one dashboard tree using an explicit revision and return no cached body."""
    _no_store(response)
    await _call(public.delete_dashboard(session, dashboard_id, expected_revision, scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled))
    return Response(status_code=204, headers={"Cache-Control": "private, no-store"})


@router.get("/dashboards/{dashboard_id}/groups", response_model=list[DashboardGroupRead])
async def list_groups(dashboard_id: UUID, session: Session, owner: OwnerRead, scope: WorkspaceRead, request: Request, response: Response) -> list[DashboardGroupRead]:
    """List groups belonging to an authenticated owner's dashboard."""
    _no_store(response)
    rows = await public.list_groups(session, dashboard_id, scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    if rows is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Dashboard not found", "details": {}})
    return rows


@router.post("/dashboards/{dashboard_id}/groups", status_code=201, response_model=DashboardGroupRead)
async def create_group(dashboard_id: UUID, payload: GroupCreate, session: Session, owner: OwnerWrite, scope: WorkspaceWrite, request: Request, response: Response) -> DashboardGroupRead:
    """Create a group only after owner-write authorization and expected-revision validation."""
    _no_store(response)
    return await _call(public.create_group(session, dashboard_id, payload, scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled))


@router.patch("/dashboards/{dashboard_id}/groups/{group_id}", response_model=DashboardGroupRead)
async def patch_group(dashboard_id: UUID, group_id: UUID, payload: GroupPatch, session: Session, owner: OwnerWrite, scope: WorkspaceWrite, request: Request, response: Response) -> DashboardGroupRead:
    """Patch group name or order while rejecting explicit null for required values."""
    _no_store(response)
    return await _call(public.patch_group(session, dashboard_id, group_id, payload, scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled))


@router.delete("/dashboards/{dashboard_id}/groups/{group_id}", status_code=204)
async def delete_group(dashboard_id: UUID, group_id: UUID, expected_revision: Annotated[int, Query(ge=1, le=9_007_199_254_740_991)], session: Session, owner: OwnerWrite, scope: WorkspaceWrite, request: Request, response: Response) -> Response:
    """Delete only an empty group and preserve its revision conflict details."""
    _no_store(response)
    await _call(public.delete_group(session, dashboard_id, group_id, expected_revision, scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled))
    return Response(status_code=204, headers={"Cache-Control": "private, no-store"})


@router.post("/dashboards/{dashboard_id}/instances", status_code=201, response_model=DashboardDetail)
async def create_instance(dashboard_id: UUID, payload: InstanceCreate, session: Session, owner: OwnerWrite, scope: WorkspaceWrite, request: Request, response: Response) -> DashboardDetail:
    """Place a reusable definition into an owned group and atomically set both breakpoint rectangles."""
    _no_store(response)
    return await _call(public.create_instance(session, dashboard_id, payload, scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled))


@router.patch("/dashboards/{dashboard_id}/instances/{instance_id}", response_model=DashboardDetail)
async def patch_instance(dashboard_id: UUID, instance_id: UUID, payload: InstancePatch, session: Session, owner: OwnerWrite, scope: WorkspaceWrite, request: Request, response: Response) -> DashboardDetail:
    """Change instance title, group, or order without changing either saved layout."""
    _no_store(response)
    return await _call(public.patch_instance(session, dashboard_id, instance_id, payload, scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled))


@router.delete("/dashboards/{dashboard_id}/instances/{instance_id}", response_model=DashboardDetail)
async def delete_instance(dashboard_id: UUID, instance_id: UUID, expected_revision: Annotated[int, Query(ge=1, le=9_007_199_254_740_991)], session: Session, owner: OwnerWrite, scope: WorkspaceWrite, request: Request, response: Response) -> DashboardDetail:
    """Remove an instance and its two placements while retaining its shared definition."""
    _no_store(response)
    return await _call(public.delete_instance(session, dashboard_id, instance_id, expected_revision, scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled))


@router.put("/dashboards/{dashboard_id}/layout", response_model=DashboardDetail)
async def replace_layout(dashboard_id: UUID, payload: LayoutReplace, session: Session, owner: OwnerWrite, scope: WorkspaceWrite, request: Request, response: Response) -> DashboardDetail:
    """Replace one complete breakpoint layout under its dashboard revision and owner authorization."""
    _no_store(response)
    return await _call(public.replace_layout(session, dashboard_id, payload, scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled))


PREVIEW_LIMIT_PER_MINUTE = 10


async def _allow_preview(request: Request, scope: WorkspaceContext) -> None:
    """Workspace and user scoped fixed-window limit (preview fans out into topic and projection queries); Redis outage fails closed."""
    key = f"dashboard:preview:{scope.workspace_id}:{scope.user_id}:{int(time.time() // 60)}"
    try:
        pipeline = request.app.state.redis.pipeline(transaction=True)
        pipeline.incr(key)
        pipeline.expire(key, 120, nx=True)
        count = (await pipeline.execute())[0]
    except RedisError as exc:
        raise HTTPException(status_code=503, detail={"code": "preview_unavailable", "message": "Preview is temporarily unavailable", "details": {}}) from exc
    if count > PREVIEW_LIMIT_PER_MINUTE:
        raise HTTPException(
            status_code=429, detail={"code": "preview_rate_limited", "message": "Too many previews", "details": {}},
            headers={"Retry-After": str(60 - int(time.time()) % 60)},
        )


@router.get("/gadget-definitions", response_model=list[GadgetDefinitionRead])
async def list_definitions(session: Session, owner: OwnerRead, scope: WorkspaceRead, request: Request, response: Response, limit: Annotated[int, Query(ge=1, le=200)] = 200) -> list[GadgetDefinitionRead]:
    """List a bounded page from the authenticated owner's reusable definition library."""
    _no_store(response)
    return await public.list_definitions(session, limit, scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)


@router.post("/gadget-definitions", status_code=201, response_model=GadgetDefinitionRead)
async def create_definition(payload: GadgetDefinitionCreate, session: Session, owner: OwnerWrite, scope: WorkspaceWrite, request: Request, response: Response) -> GadgetDefinitionRead:
    """Save validated renderer configuration after source lifecycle checks and quota enforcement."""
    _no_store(response)
    return await _call(public.create_definition(session, payload, scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled))


@router.post("/gadget-definitions/highlight-preview", response_model=HighlightPreviewRead)
async def preview_highlights(payload: HighlightPreviewRequest, session: Session, owner: OwnerWrite, scope: WorkspaceWrite, response: Response, request: Request) -> HighlightPreviewRead:
    """Dry-run draft rules over the last days of current evidence; never persists or notifies."""
    _no_store(response)
    await _allow_preview(request, scope)
    return await _call(public.preview_highlights(
        session, payload, scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled))


@router.get("/gadget-definitions/{definition_id}/usage", response_model=list[GadgetDefinitionUsageRead])
async def definition_usage(definition_id: UUID, session: Session, owner: OwnerRead, scope: WorkspaceRead, request: Request, response: Response) -> list[GadgetDefinitionUsageRead]:
    """List the owner dashboards using one definition, for edit and delete warnings."""
    _no_store(response)
    result = await public.definition_usage(
        session, definition_id, scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    if result is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Definition not found", "details": {}})
    return result


@router.get("/gadget-definitions/{definition_id}", response_model=GadgetDefinitionRead)
async def get_definition(definition_id: UUID, session: Session, owner: OwnerRead, scope: WorkspaceRead, request: Request, response: Response) -> GadgetDefinitionRead:
    """Read one owner definition or conceal missing and foreign identifiers with 404."""
    _no_store(response)
    result = await public.get_definition(session, definition_id, scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    if result is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Definition not found", "details": {}})
    return result


@router.get("/gadget-definitions/{definition_id}/highlights", response_model=list[DashboardHighlightRead])
async def evaluate_highlights(
    definition_id: UUID, session: Session, owner: OwnerRead, scope: WorkspaceRead, request: Request, response: Response,
) -> list[DashboardHighlightRead]:
    """Read one owner's bounded highlight matches without persisting alerts."""
    _no_store(response)
    return await _call(public.evaluate_gadget_highlights(session, definition_id, scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled))


@router.patch("/gadget-definitions/{definition_id}", response_model=GadgetDefinitionRead)
async def patch_definition(definition_id: UUID, payload: GadgetDefinitionPatch, session: Session, owner: OwnerWrite, scope: WorkspaceWrite, request: Request, response: Response) -> GadgetDefinitionRead:
    """Update reusable configuration under its independent revision and owner write authorization."""
    _no_store(response)
    return await _call(public.patch_definition(session, definition_id, payload, scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled))


@router.delete("/gadget-definitions/{definition_id}", status_code=204)
async def delete_definition(definition_id: UUID, expected_revision: Annotated[int, Query(ge=1, le=9_007_199_254_740_991)], session: Session, owner: OwnerWrite, scope: WorkspaceWrite, request: Request, response: Response) -> Response:
    """Delete only unused reusable configuration and emit its final revision event."""
    _no_store(response)
    await _call(public.delete_definition(session, definition_id, expected_revision, scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled))
    return Response(status_code=204, headers={"Cache-Control": "private, no-store"})


@router.get("/gadget-renderers", response_model=list[RendererRead])
async def list_renderers(owner: OwnerRead, response: Response) -> list[RendererRead]:
    """Return renderer adapter metadata and minimum geometry without runtime acceptance claims."""
    _no_store(response)
    return public.renderer_reads()


@router.get("/gadget-sources", response_model=GadgetSourceSelectionPage)
async def list_gadget_sources(session: Session, owner: OwnerRead, scope: WorkspaceRead, request: Request, response: Response, limit: Annotated[int, Query(ge=1, le=100)] = 50, cursor: str | None = Query(default=None, max_length=512)) -> dict[str, Any]:
    """Return owner source selection metadata without exposing connector configuration or content."""
    _no_store(response)
    page = await public.list_gadget_sources(session, limit, cursor, scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    return page.model_dump(mode="json")


@router.get("/dashboard-presets", response_model=list[DashboardPresetRead])
async def list_presets(owner: OwnerRead, response: Response) -> list[DashboardPresetRead]:
    """Return stable static preset templates without creating sources or collecting data."""
    _no_store(response)
    return public.preset_catalog()


@router.post("/dashboard-presets/{preset_id}/preview", response_model=PresetPreviewRead)
async def preview_preset(preset_id: str, payload: PresetPreviewRequest, session: Session, owner: OwnerWrite, scope: WorkspaceWrite, request: Request, response: Response) -> PresetPreviewRead:
    """Preview explicit source selectors through owner-write authorization without persistence."""
    _no_store(response)
    return await _call(public.preview_preset(session, preset_id, payload, scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled))


@router.post("/dashboard-presets/{preset_id}/apply", status_code=201, response_model=DashboardDetail)
async def apply_preset(preset_id: str, payload: PresetApplyRequest, session: Session, owner: OwnerWrite, scope: WorkspaceWrite, request: Request, response: Response) -> DashboardDetail:
    """Apply a fresh fingerprinted preset atomically after explicit replacement confirmation when needed."""
    _no_store(response)
    if payload.mode == "replace":
        response.status_code = 200
    return await _call(public.apply_preset(session, preset_id, payload, scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled))


def _timezone(value: str) -> str:
    """Validate a query timezone, mapping failure to the stable 422 envelope."""
    try:
        return validate_timezone(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_timezone", "message": str(exc), "details": {}}) from exc


@router.get("/context/current", response_model=DailyContext)
async def current_context(
    session: Session, owner: OwnerRead, scope: WorkspaceRead, request: Request, response: Response, timezone: str = "Asia/Ho_Chi_Minh",
) -> DailyContext:
    """Return the selected-day context for the current local date in the requested timezone."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    _no_store(response)
    zone = _timezone(timezone)
    result = await context.build_daily_context(session, datetime.now(ZoneInfo(zone)).date(), zone, scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    return result


@router.get("/context/daily", response_model=DailyContext)
async def daily_context(
    session: Session, owner: OwnerRead, scope: WorkspaceRead, request: Request, response: Response, date: date, timezone: str = "Asia/Ho_Chi_Minh",
) -> DailyContext:
    """Return the saved brief plus current-record widgets for one local date."""
    _no_store(response)
    zone = _timezone(timezone)
    try:
        result = await context.build_daily_context(session, date, zone, scope=scope,
            multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    except ValueError as exc:  # nonexistent local midnight in that zone
        raise HTTPException(status_code=422, detail={"code": "invalid_date", "message": str(exc), "details": {}}) from exc
    return result


@router.get("/briefs", response_model=list[BriefRead])
async def list_brief_revisions(
    session: Session, owner: OwnerRead, scope: WorkspaceRead, request: Request, response: Response, date: date, timezone: str = "Asia/Ho_Chi_Minh",
) -> list[BriefRead]:
    """List every saved revision of a day's brief, newest first."""
    _no_store(response)
    return await briefs.list_briefs(session, date, _timezone(timezone), scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)


@router.post("/briefs/generate", response_model=BriefRead, status_code=201)
async def generate_brief(
    payload: BriefGenerateRequest, request: Request, session: Session, owner: OwnerWrite, scope: WorkspaceWrite, response: Response,
) -> BriefRead:
    """Generate a new brief revision; a model outage, or inputs changed or purged mid-call, returns 503 and keeps the earlier revisions."""
    _no_store(response)
    try:
        return await briefs.generate_brief(
            session, payload.brief_date, payload.timezone,
            scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
            settings=request.app.state.settings, redis=request.app.state.redis, force=payload.force,
        )
    except briefs.BriefEmpty as exc:
        raise HTTPException(status_code=409, detail={"code": "no_inputs", "message": "Nothing to summarize for this day", "details": {}}) from exc
    except briefs.BriefUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "model_unavailable", "message": "Brief generation is unavailable", "details": {}}) from exc


@router.get("/briefs/schedule", response_model=BriefSchedule)
async def read_brief_schedule(session: Session, owner: OwnerRead, scope: WorkspaceRead, request: Request, response: Response) -> BriefSchedule:
    """Read the editable daily brief schedule (default 07:00 Asia/Ho_Chi_Minh)."""
    _no_store(response)
    return await briefs.read_schedule(session, scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)


@router.get("/briefs/schedule/ownership")
async def read_brief_schedule_ownership(session: Session, owner: OwnerRead, scope: WorkspaceRead, request: Request, response: Response) -> dict[str, object]:
    """Read which scheduler (internal cron or one automation) owns the daily brief slot."""
    _no_store(response)
    return await briefs.read_slot_owner(session, scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)


@router.put("/briefs/schedule", response_model=BriefSchedule)
async def save_brief_schedule(payload: BriefSchedule, session: Session, owner: OwnerWrite, scope: WorkspaceWrite, request: Request, response: Response) -> BriefSchedule:
    """Save the daily brief schedule consumed by the ARQ cron."""
    _no_store(response)
    return await briefs.save_schedule(session, payload, scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
