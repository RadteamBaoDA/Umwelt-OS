"""Publication gate for member reads of shared content: every protected ASGI send is re-fenced."""

import asyncio
from typing import Any, Final

import anyio
from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from core.workspaces import public as workspaces
from core.workspaces.schemas import PublicationFence

SEND_TIMEOUT_SECONDS: Final = 2.0
DB_TIMEOUT_SECONDS: Final = 3.0
CLEANUP_TIMEOUT_SECONDS: Final = 2.0
_PRIVATE_HEADERS: Final = {"Cache-Control": "private, no-store", "Vary": "Cookie, X-Workspace-ID"}


def require_publication_gate(request: Request, fence: PublicationFence) -> None:
    """Arm the gate: the middleware re-locks this fence before every protected send."""
    if not isinstance(fence, PublicationFence):
        raise TypeError("A PublicationFence is required")
    request.state.publication_fence = fence


async def _cleanup(session: AsyncSession) -> None:
    """Roll back and close, shielded from cancellation; invalidate if that fails."""
    with anyio.CancelScope(shield=True):
        try:
            with anyio.fail_after(CLEANUP_TIMEOUT_SECONDS):
                await session.rollback()
                await session.close()
        except BaseException:
            with anyio.fail_after(CLEANUP_TIMEOUT_SECONDS):
                await session.invalidate()
            raise


class PublicationGateMiddleware:
    """Pure ASGI middleware gating http.response.start and non-empty bodies when a fence is armed."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False
        denied: HTTPException | None = None

        async def gated(message: Message) -> None:
            nonlocal started, denied
            if denied is not None:
                return  # response start was denied: drop the rest, 404/409 is sent afterwards
            fence: PublicationFence | None = (scope.get("state") or {}).get("publication_fence")
            kind = message["type"]
            protected = fence is not None and (
                kind == "http.response.start" or (kind == "http.response.body" and bool(message.get("body", b""))))
            if not protected:
                if kind == "http.response.start":
                    started = True
                await send(message)
                return
            assert fence is not None
            app: Any = scope["app"]
            session: AsyncSession = app.state.session_factory()
            try:
                try:
                    async with asyncio.timeout(DB_TIMEOUT_SECONDS):
                        await workspaces.lock_access_fence(
                            session, scope=fence.scope, expected=fence.access_fence,
                            multi_workspace_enabled=app.state.settings.multi_workspace_enabled,
                            auth_sessions=(fence.auth_session,))
                        await workspaces.lock_resource_grants(session, scope=fence.scope, grants=fence.grants)
                except HTTPException as exc:
                    denied = exc
                    if kind == "http.response.start":
                        return  # swallowed; the caller path below sends the 404/409
                    raise RuntimeError("publication_revoked") from exc  # mid-body: abort connection
                if kind == "http.response.start":
                    started = True
                async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
                    await send(message)
            finally:
                await _cleanup(session)

        await self.app(scope, receive, gated)
        if denied is not None and not started:
            status = 409 if denied.status_code == 409 else 404
            detail = "Workspace access fence changed" if status == 409 else "Not found"
            await JSONResponse({"detail": detail}, status_code=status, headers=_PRIVATE_HEADERS)(
                scope, receive, send)
