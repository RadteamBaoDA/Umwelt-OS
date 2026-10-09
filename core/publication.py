"""Publication gate seam for member reads of shared content (M0 contract; W4-pub implements)."""

from typing import Final

from fastapi import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from core.workspaces.schemas import PublicationFence

SEND_TIMEOUT_SECONDS: Final = 2.0


def require_publication_gate(request: Request, fence: PublicationFence) -> None:
    """Set request.state.publication_fence (W4-pub)."""
    raise NotImplementedError("W4-pub")


class PublicationGateMiddleware:
    """Pure ASGI middleware; W4-pub registers it in apps/api/main.py."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        raise NotImplementedError("W4-pub")
