"""Phone prompt picker routes. Disabled (404) unless PICK_ENABLED=true.

Desktop routes need an authenticated session (writes also CSRF). Phone routes are anonymous by design but
accept only the 128-bit session id, are rate limited per client IP, and take an option index, never text.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, StrictInt

from core.auth.dependencies import require_account, require_account_write
from core.auth.models import AuthSession
from core.rate_limit import require_budget
from modules.pick import public


async def _enabled(request: Request) -> None:
    """Hide the whole feature (404) while the PICK_ENABLED flag is off, before any auth or state work."""
    if not request.app.state.settings.pick_enabled:
        raise HTTPException(status_code=404, detail="Not found")


router = APIRouter(prefix="/api/v1/pick", tags=["pick"], dependencies=[Depends(_enabled)])
OwnerRead = Annotated[AuthSession, Depends(require_account)]
OwnerWrite = Annotated[AuthSession, Depends(require_account_write)]
phone_budget = Depends(require_budget("pick_phone", 30, 60))
create_budget = Depends(require_budget("pick_create", 20, 60))


class PickChoice(BaseModel):
    """Phone submission: only a pool index; extra fields are rejected."""

    model_config = ConfigDict(extra="forbid")
    index: StrictInt


def _gone() -> HTTPException:
    """Uniform 404 so callers cannot tell unknown, expired, consumed and wrong-state sessions apart."""
    return HTTPException(status_code=404, detail="Not found")


@router.get("/status")
async def pick_status(owner: OwnerRead) -> dict[str, bool]:
    """Tell the signed-in desktop that the feature is on (the router already 404s when it is off)."""
    return {"enabled": True}


@router.post("/sessions", dependencies=[create_budget])
async def create_pick_session(request: Request, response: Response, owner: OwnerWrite) -> dict[str, object]:
    """Create a QR session; returns its id, lifetime and the host LAN IPv4 (only ever to this authenticated caller)."""
    response.headers["Cache-Control"] = "private, no-store"
    session_id = await public.create_session(request.app.state.redis)
    return {"session_id": session_id, "expires_in": public.PICK_TTL_SECONDS, "lan_ip": public.lan_ipv4()}


@router.get("/sessions/{session_id}")
async def poll_pick_session(session_id: str, request: Request, response: Response, owner: OwnerRead) -> dict[str, object]:
    """Desktop poll; a picked prompt is delivered exactly once."""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return await public.poll(request.app.state.redis, session_id)
    except public.PickUnavailable as exc:
        raise _gone() from exc


@router.put("/phone/{session_id}", dependencies=[phone_budget])
async def scan_pick_session(session_id: str, request: Request, response: Response) -> dict[str, object]:
    """Phone opened the link: mark scanned and return the 3 offered options."""
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"options": await public.scan(request.app.state.redis, session_id)}
    except public.PickUnavailable as exc:
        raise _gone() from exc


@router.post("/phone/{session_id}", status_code=204, dependencies=[phone_budget])
async def submit_pick(session_id: str, payload: PickChoice, request: Request) -> Response:
    """Phone submits its chosen option index."""
    try:
        await public.submit(request.app.state.redis, session_id, payload.index)
    except public.PickUnavailable as exc:
        raise _gone() from exc
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
