"""GitHub webhook receiver and operator webhook-status routes."""

import asyncio
import re
from datetime import datetime

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from core.config import Settings
from modules.connectors.github._routes.common import (
    OperatorRead,
    Session,
    router,
)
from modules.connectors.github.webhooks import (
    MAX_GITHUB_WEBHOOK_BYTES,
    parse_github_delivery,
    verify_github_signature,
)
from modules.connectors.models import (
    GithubSourceHint,
    GithubWebhookCapacity,
    GithubWebhookDelivery,
    GithubWebhookOutbox,
)


class GitHubWebhookStatus(BaseModel):
    """Expose receiver readiness and bounded backlog counts without delivery or source identities."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    receiver_configured: bool
    receiver_revision: str
    digest_count: int = Field(ge=0, le=100_000)
    pending_count: int = Field(ge=0, le=100_000)
    pending_deliveries: int = Field(ge=0, le=100_000)
    pending_hints: int = Field(ge=0, le=100_000)
    needs_attention: int = Field(ge=0, le=200_000)
    oldest_pending_at: datetime | None = None


@router.post("/github/webhook")
async def receive_github_webhook(request: Request, session: Session) -> JSONResponse:
    """Authenticate exact GitHub request bytes, bound parsing, then durably admit a source-less delivery.

    The route intentionally has no owner-session dependency. It verifies HMAC before JSON parsing,
    checks persisted connector availability only after signature validation, and acknowledges only
    after the digest, extracted identifiers, and dispatch slot commit. Event content is a hint;
    current API reads and grants remain authoritative.
    """
    settings: Settings = request.app.state.settings
    app_id = settings.github_app_id
    secret = settings.github_app_webhook_secret.get_secret_value()
    if not app_id or not secret:
        raise HTTPException(status_code=503, detail="GitHub webhook receiver is unavailable")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="GitHub webhook requires application/json")
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        if not raw_length.isdecimal():
            raise HTTPException(status_code=400, detail="GitHub webhook length is invalid")
        if int(raw_length) > MAX_GITHUB_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="GitHub webhook body exceeds 256 KiB")
    delivery_id = request.headers.get("x-github-delivery", "")
    event = request.headers.get("x-github-event", "")
    signature = request.headers.get("x-hub-signature-256")
    if (
        not 1 <= len(delivery_id) <= 128
        or any(ord(char) < 33 or ord(char) > 126 for char in delivery_id)
        or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", event) is None
    ):
        raise HTTPException(status_code=400, detail="GitHub webhook headers are invalid")
    body = bytearray()
    try:
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                if len(body) + len(chunk) > MAX_GITHUB_WEBHOOK_BYTES:
                    raise HTTPException(
                        status_code=413, detail="GitHub webhook body exceeds 256 KiB"
                    )
                body.extend(chunk)
    except TimeoutError as exc:
        raise HTTPException(status_code=503, detail="GitHub webhook ingress timed out") from exc
    if not verify_github_signature(bytes(body), signature, secret):
        raise HTTPException(status_code=401, detail="GitHub webhook signature is invalid")
    try:
        delivery = parse_github_delivery(
            raw_body=bytes(body),
            receiver_revision=settings.github_webhook_receiver_revision,
            delivery_id=delivery_id,
            event=event,
            app_id=app_id,
        )
    except (ValueError, TypeError, RecursionError) as exc:
        raise HTTPException(status_code=400, detail="GitHub webhook payload is invalid") from exc
    from core.modules import register_modules
    from modules.connectors import public as connectors

    # The signed receiver is a source-less instance ingress: only build availability can be checked
    # here. Workspace enablement is checked per binding after that binding's own admission (worker).
    if not register_modules()["connectors"].enabled:
        raise HTTPException(status_code=404, detail="Webhook unavailable")

    receipt = await connectors.persist_verified_github_delivery(session, delivery)
    code = 202 if receipt.disposition == "received" else 200
    return JSONResponse(status_code=code, content=receipt.model_dump(mode="json"))


@router.get("/github/webhook-status", response_model=GitHubWebhookStatus)
async def get_github_webhook_status(
    request: Request,
    session: Session,
    _operator: OperatorRead,
) -> GitHubWebhookStatus:
    """Return owner-visible receiver readiness and global durable backlog without source inventory."""
    capacity = await session.get(GithubWebhookCapacity, 1)
    if capacity is None:
        raise HTTPException(status_code=503, detail="GitHub webhook capacity is unavailable")
    pending_deliveries = int(
        await session.scalar(
            select(func.count())
            .select_from(GithubWebhookOutbox)
            .where(
                GithubWebhookOutbox.state.in_(("pending", "dispatched", "needs_attention")),
            )
        )
        or 0
    )
    pending_hints = int(
        await session.scalar(
            select(func.count())
            .select_from(GithubSourceHint)
            .where(
                GithubSourceHint.state.in_(("pending", "dispatched", "needs_attention")),
            )
        )
        or 0
    )
    terminal_attention = int(
        await session.scalar(
            select(func.count())
            .select_from(GithubWebhookOutbox)
            .where(
                GithubWebhookOutbox.state == "needs_attention",
            )
        )
        or 0
    )
    terminal_attention += int(
        await session.scalar(
            select(func.count())
            .select_from(GithubSourceHint)
            .where(
                GithubSourceHint.state == "needs_attention",
            )
        )
        or 0
    )
    oldest_delivery = await session.scalar(
        select(func.min(GithubWebhookDelivery.received_at))
        .join(
            GithubWebhookOutbox,
            GithubWebhookOutbox.delivery_id == GithubWebhookDelivery.id,
        )
        .where(GithubWebhookOutbox.state.in_(("pending", "dispatched", "needs_attention")))
    )
    oldest_hint = await session.scalar(
        select(func.min(GithubSourceHint.updated_at)).where(
            GithubSourceHint.state.in_(("pending", "dispatched", "needs_attention")),
        )
    )
    oldest_values = [value for value in (oldest_delivery, oldest_hint) if value is not None]
    oldest = min(oldest_values) if oldest_values else None
    settings: Settings = request.app.state.settings
    return GitHubWebhookStatus(
        receiver_configured=bool(
            settings.github_app_id and settings.github_app_webhook_secret.get_secret_value()
        ),
        receiver_revision=settings.github_webhook_receiver_revision,
        digest_count=capacity.digest_count,
        pending_count=capacity.pending_count,
        pending_deliveries=pending_deliveries,
        pending_hints=pending_hints,
        needs_attention=terminal_attention,
        oldest_pending_at=oldest,
    )
