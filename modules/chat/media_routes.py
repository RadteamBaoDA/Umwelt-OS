"""Authenticated same-origin media proxy routes for opt-in Generative UI remote media."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_session
from core.rate_limit import require_budget
from core.workspaces.dependencies import require_workspace_read
from core.workspaces.schemas import WorkspaceContext
from modules.chat import media
from modules.settings import public as settings_public
from modules.settings.public import module_dependency

router = APIRouter(
    prefix="/api/v1/media", tags=["chat"],
    # Authenticated mode: the per-account budget bounds upstream fetches a logged-in client can trigger.
    dependencies=[Depends(module_dependency("chat")), Depends(require_budget("media_proxy", 300, 60))],
)
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[WorkspaceContext, Depends(require_workspace_read)]
_HEADERS = {
    "Cache-Control": "private, max-age=3600",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",  # a direct navigation can never run active content
    "Cross-Origin-Resource-Policy": "same-origin",
}
_ERRORS = {"not_configured": 503, "bad_token": 403, "expired": 403, "host_denied": 403, "bad_tile": 404}


async def _serve(url: str, *, request: Request, session: AsyncSession, scope: WorkspaceContext,
                 extra: dict[str, str] | None = None) -> Response:
    """Check the owner flag, fetch the already-validated ``url`` and return it with hardened cache headers.

    Raises:
        HTTPException: 403 when the privacy flag is off; 502 when the upstream is rejected or fails.
    """
    config = await settings_public.get_ai_execution_config(
        session, request.app.state.settings, request.app.state.redis, scope=scope)
    if not config.privacy.allow_widget_remote_media:
        raise HTTPException(status_code=403, detail="Remote widget media is disabled")
    try:
        body, content_type = await media.fetch_media(url)
    except media.MediaError:
        raise HTTPException(status_code=502, detail="Media unavailable") from None
    return Response(body, media_type=content_type, headers={**_HEADERS, **(extra or {})})


@router.get("/proxy")
async def proxy_media(
    request: Request, session: Session, owner: OwnerRead, token: Annotated[str, Query(max_length=2048)],
) -> Response:
    """Serve one image bound to a signed, unexpired token; allowlisted host, image/* only, 5 MB cap, no redirects."""
    secret = request.app.state.settings.csrf_signing_secret.get_secret_value()
    try:
        url = media.verify_media_token(secret, token, subject=f"{owner.workspace_id}:{owner.user_id}")
    except media.MediaError as exc:
        raise HTTPException(status_code=_ERRORS.get(exc.code, 403), detail="Invalid media token") from None
    return await _serve(url, request=request, session=session, scope=owner)


@router.get("/tile/{z}/{x}/{y}")
async def proxy_tile(z: int, x: int, y: int, request: Request, session: Session, owner: OwnerRead) -> Response:
    """Serve a USGS Topo tile; the response carries ``X-Attribution`` which map UIs must display."""
    try:
        url = media.tile_url(z, x, y)
    except media.MediaError:
        raise HTTPException(status_code=404, detail="Tile out of range") from None
    return await _serve(url, request=request, session=session, scope=owner,
                        extra={"X-Attribution": media.TILE_ATTRIBUTION})
