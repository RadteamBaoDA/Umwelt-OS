"""Authenticated internal callbacks from the isolated browser service."""

import hmac
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from core.realtime import commit_with_replay
from core.remote_heavy import (
    cleanup_proof_after_ack,
    clear_remote_heavy_in_uow,
    get_remote_heavy_guard,
    register_remote_heavy_in_uow,
)
from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.agents.public import BrowserRunAuthorization, revalidate_browser_run_authority
from modules.connectors import public as connectors
from modules.tools.browser_public import _job_authority, browser_capability_verified
from modules.tools.models import BrowserReadJob

# Guard lifetime: longer than any service job (bounded at 47 s or less) but inside the
# ARQ retry window of deferred heavy work (max_tries 5 x defer 30 s ~= 150 s), so a
# deferred job outlives an orphaned guard instead of exhausting its retries first.
REMOTE_GUARD_SECONDS = 90

router = APIRouter(prefix="/api/v1/browser-control", tags=["internal-browser-control"])


class BrowserControlEvent(BaseModel):
    """Carry one exact service identity and a bounded lifecycle/request authorization event."""

    model_config = ConfigDict(extra="forbid", strict=True)
    event: Literal["register", "authorize", "complete", "cancelled"]
    operation_id: UUID
    claim_generation: StrictInt = Field(ge=1)
    service_instance_id: str = Field(min_length=1, max_length=128)
    request_ordinal: StrictInt = Field(ge=0, le=6)
    target_url: str | None = Field(default=None, max_length=2048)
    actual_pages: StrictInt = Field(default=0, ge=0, le=3)
    actual_bytes: StrictInt = Field(default=0, ge=0, le=5 * 1024 * 1024)
    result_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


def _service_authorized(authorization: str | None, expected: str) -> bool:
    """Compare the shared service identity without leaking token equality timing."""
    scheme, _, token = (authorization or "").partition(" ")
    return bool(expected and scheme.lower() == "bearer" and hmac.compare_digest(token, expected))


def _browser_network_verified() -> bool:
    """Keep egress disabled until S4 records accepted isolation and model capability proof."""
    return browser_capability_verified()


def _target_in_scope(value: str | None, origin: str, path_prefix: str) -> bool:
    """Require exact HTTPS origin and segment-bounded source path for actual socket targets."""
    from urllib.parse import unquote, urlsplit

    if not value:
        return False
    try:
        target = urlsplit(value)
        port = target.port
        host = (target.hostname or "").encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return False
    path = unquote(target.path or "/")
    prefix = unquote(path_prefix or "/")
    return bool(
        target.scheme == "https" and target.username is None and target.password is None
        and not target.fragment and target.query == ""
        and f"https://{host}" == origin
        and (port is None or port == 443)
        and not any(segment in {".", ".."} for segment in path.split("/"))
        and "%2f" not in target.path.lower() and "%5c" not in target.path.lower()
        and "%25" not in target.path.lower() and "\\" not in path
        and (path == prefix or prefix == "/" or path.startswith(prefix.rstrip("/") + "/"))
    )


async def _current_authority(
    session: AsyncSession, job: BrowserReadJob, *, multi_workspace_enabled: bool,
) -> tuple[bool, connectors.AgentBrowserScope | None, InternalJobScope | None, AccessFence | None]:
    """Check original run/session/profile/Chat and source grant in this short callback transaction.

    Also returns the job scope and the locked original access fence (both None when authority is
    already revoked and no fence is held) so the caller can commit with replay.
    """
    from core.auth.public import revalidate_owner_session
    from modules.sources import public as sources

    authority = await _job_authority(session, job, multi_workspace_enabled=multi_workspace_enabled)
    if authority is None:
        return False, None, None, None
    job_scope, fence = authority
    source = await sources.lock_source(
        session, job.source_id, scope=job_scope, multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=fence,
    )
    source_view = await sources.get_connector_source(
        session, job.source_id, scope=job_scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    grant = await connectors.resolve_agent_browser_scope(
        session, job.owner_id, job.source_id, scope=job_scope, multi_workspace_enabled=multi_workspace_enabled,
    )

    authorization = BrowserRunAuthorization(
        job.owner_id, job.run_id, job.tool_slot, job.arguments_hash,
        job.auth_session_hash, job.conversation_id, job.profile_id,
        job.profile_revision_hash, frozenset(UUID(item) for item in job.authorized_source_ids),
        job.claim_generation, 0, 0, 0, 1,
    )
    session_valid = await revalidate_owner_session(session, job.auth_session_hash, job.owner_id)
    run_valid = await revalidate_browser_run_authority(
        session, authorization, scope=job_scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if (
        not session_valid or not run_valid or job.source_id not in authorization.source_ids
        or source is None or source.status != "active"
        or source.generation != job.source_generation
        or source_view is None
        or grant is None or not grant.enabled or grant.scope_hash != job.scope_hash
        or grant.source_generation != job.source_generation
        or grant.connector_revision != job.connector_revision
        or grant.grant_revision != job.grant_revision
    ):
        return False, None, job_scope, fence
    return True, grant, job_scope, fence


@router.post("/jobs/{job_id}/event")
async def browser_control_event(
    job_id: UUID,
    payload: BrowserControlEvent,
    request: Request,
    authorization: str | None = Header(default=None),
    job_token: str | None = Header(default=None, alias="X-Browser-Job-Token"),
) -> dict[str, object]:
    """Authorize service lifecycle callbacks and one next-request permit using current owner state."""
    settings = request.app.state.settings
    if not isinstance(settings, Settings) or not _service_authorized(
        authorization, settings.browser_shared_token.get_secret_value()
    ):
        raise HTTPException(status_code=401, detail="Browser service authentication required")
    if not job_token or len(job_token) != 64 or not all(c in "0123456789abcdef" for c in job_token):
        raise HTTPException(status_code=401, detail="Browser job authentication required")
    factory = request.app.state.session_factory
    async with factory() as session:
        from modules.settings.public import register_request_activity

        # The isolated browser service callback updates durable owner job state; admission is
        # committed before its Sources -> Agents -> Tools row-lock sequence begins.
        await register_request_activity(request, session, "browser_control_callback", str(job_id))
        candidate = await session.scalar(select(BrowserReadJob).where(
            BrowserReadJob.id == job_id,
        ))
        if candidate is None:
            raise HTTPException(status_code=409, detail="Browser job authority is stale")
        expected_identity = (
            candidate.operation_id, candidate.claim_generation, candidate.service_token_hash,
            candidate.source_id, candidate.run_id,
        )
        # Cross-module row locks follow Sources -> Agents -> Tools. The job is
        # reread under lock only after both current authorities have been checked.
        flag = settings.multi_workspace_enabled
        valid, grant, job_scope, fence = await _current_authority(
            session, candidate, multi_workspace_enabled=flag,
        )

        async def commit() -> None:
            if fence is None or job_scope is None:
                await session.commit()  # authority already revoked: no fence is held
            else:
                await commit_with_replay(
                    session, [], scope=job_scope, multi_workspace_enabled=flag, access_fence=fence,
                )

        def revoke() -> None:
            if job.status in {"queued", "running"}:
                job.status, job.error_code = "failed", "authority_revoked"

        job = await session.scalar(select(BrowserReadJob).where(
            BrowserReadJob.id == job_id,
        ).with_for_update().execution_options(populate_existing=True))
        if (
            job is None or expected_identity != (
                job.operation_id, job.claim_generation, job.service_token_hash,
                job.source_id, job.run_id,
            )
            or payload.operation_id != job.operation_id
            or payload.claim_generation != job.claim_generation
            or not hmac.compare_digest(sha256(job_token.encode("ascii")).hexdigest(), job.service_token_hash)
            or (job.expires_at <= datetime.now(UTC) and payload.event in {"register", "authorize"})
        ):
            raise HTTPException(status_code=409, detail="Browser job authority is stale")
        if payload.event == "register":
            if (
                job.cancel_requested or job.status != "queued" or job.service_instance_id is not None
                or payload.request_ordinal != 0 or payload.target_url is not None
                or payload.actual_pages != 0 or payload.actual_bytes != 0
                or payload.result_hash is not None
            ):
                raise HTTPException(status_code=409, detail="Browser job was already claimed")
            if not _browser_network_verified():
                raise HTTPException(status_code=503, detail="Browser network capability is unverified")
            if not valid:
                revoke()
                await commit()
                raise HTTPException(status_code=403, detail="Browser job authority was revoked")
            await register_remote_heavy_in_uow(
                session, job.operation_id, payload.service_instance_id,
                job.id, job.service_token_hash,
                # Just past the 45 s hard job bound: a guard orphaned by a dead service
                # stops blocking heavy work instead of wedging it until job expiry.
                min(job.expires_at, datetime.now(UTC) + timedelta(seconds=REMOTE_GUARD_SECONDS)),
            )
            job.service_instance_id = payload.service_instance_id
            job.status = "running"
            await commit()
            return {"allowed": True}

        guard = await get_remote_heavy_guard(session, job.operation_id)
        if (
            guard is not None and guard.state == "cleared"
            and guard.service_instance_id == payload.service_instance_id
            and guard.remote_job_id == job.id
            and job.service_instance_id == payload.service_instance_id
            and payload.event in {"complete", "cancelled"}
        ):
            return {"allowed": True, "cleaned": True}
        # An uncertain guard may still be cleared by the exact instance/nonce on
        # complete or cancelled; new request permits require it to be active.
        if (
            guard is None
            or guard.state != ("active" if payload.event == "authorize" else guard.state)
            or guard.state not in {"active", "uncertain"}
            or guard.service_instance_id != payload.service_instance_id
            or guard.remote_job_id != job.id or guard.nonce_hash != job.service_token_hash
            or job.service_instance_id != payload.service_instance_id
        ):
            raise HTTPException(status_code=409, detail="Browser remote guard is not active")
        if payload.event == "authorize":
            if not _browser_network_verified():
                raise HTTPException(status_code=503, detail="Browser network capability is unverified")
            if not valid:
                revoke()
                await commit()
                raise HTTPException(status_code=403, detail="Browser request is outside current authority")
            if (
                # The API enforces the guard bound: no new permits after expiry.
                guard.expires_at <= datetime.now(UTC)
                or not valid or grant is None
                or payload.request_ordinal != job.request_ordinal + 1
                or payload.request_ordinal > 6
                or payload.actual_pages != 0 or payload.actual_bytes != 0
                or payload.result_hash is not None
                or not _target_in_scope(payload.target_url, grant.origin, grant.path_prefix)
            ):
                raise HTTPException(status_code=403, detail="Browser request is outside current authority")
            job.request_ordinal = payload.request_ordinal
            await commit()
            return {"allowed": True, "request_ordinal": payload.request_ordinal}
        if payload.event == "complete":
            if job.cancel_requested or job.status != "running":
                # A cancelled or purged job is never resurrected: clear the guard and
                # settle any non-terminal row as cancelled, never as running.
                if job.status in {"queued", "running", "cancel_requested", "uncertain"}:
                    job.status = "cancelled"
                proof = cleanup_proof_after_ack(job.operation_id, guard.service_instance_id, job.id)
                await clear_remote_heavy_in_uow(session, job.operation_id, proof)
                await commit()
                return {"allowed": True, "cleaned": True}
            if (
                payload.request_ordinal != job.request_ordinal
                or not 1 <= payload.actual_pages <= job.max_pages
                or payload.actual_bytes > job.max_bytes
                or payload.result_hash is None
            ):
                raise HTTPException(status_code=409, detail="Browser completion ordinal is stale")
            job.actual_pages = payload.actual_pages
            job.actual_bytes = payload.actual_bytes
            job.result_hash = payload.result_hash
            job.status = "running"
            proof = cleanup_proof_after_ack(job.operation_id, guard.service_instance_id, job.id)
            await clear_remote_heavy_in_uow(session, job.operation_id, proof)
            await commit()
            return {"allowed": True}
        if job.status not in {"succeeded", "failed", "expired"}:
            job.status = "cancelled"
        job.cancel_requested = True
        proof = cleanup_proof_after_ack(job.operation_id, guard.service_instance_id, job.id)
        await clear_remote_heavy_in_uow(session, job.operation_id, proof)
        await commit()
        return {"allowed": True}
