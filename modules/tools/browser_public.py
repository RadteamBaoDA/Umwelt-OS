"""Owner contracts for durable, source-fenced static browser observations."""

import asyncio
import hashlib
import hmac
import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import DomainError, ForbiddenError, error_status
from core.ids import uuid7
from core.realtime import commit_with_replay
from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope, WorkspaceContext
from modules.agents.public import BrowserRunAuthorization
from modules.connectors.public import AgentBrowserScope
from modules.tools.models import BrowserPageEvidence, BrowserReadJob


def _actor(scope: Scope) -> int:
    """Return the acting owner ID: the stamped actor of a job scope or the request user."""
    return scope.actor_user_id if isinstance(scope, InternalJobScope) else scope.user_id


def _require_owner(scope: Scope) -> None:
    """Require an explicit workspace scope and, for requests, the workspace owner role.

    Raises ``TypeError`` for an untyped scope and ``ForbiddenError`` (403) for a member; job scopes
    were admitted against the stamped owner already. Pure; runs before any query.
    """
    if not isinstance(scope, (WorkspaceContext, InternalJobScope)):
        raise TypeError("An explicit workspace scope is required")
    if isinstance(scope, WorkspaceContext) and scope.role != "owner":
        raise ForbiddenError("Workspace owner required")


async def _admit(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool,
    lock: bool = False, expected: AccessFence | None = None,
) -> AccessFence:
    """Owner admission; a member is denied before any session await."""
    _require_owner(scope)
    if type(multi_workspace_enabled) is not bool:
        raise TypeError("The configured multi-workspace feature flag must be a boolean")
    if lock:
        return await workspaces.lock_access_fence(
            session, scope=scope, expected=expected, multi_workspace_enabled=multi_workspace_enabled,
        )
    return await workspaces.read_access_fence(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)


async def _job_authority(
    session: AsyncSession, job: BrowserReadJob, *, multi_workspace_enabled: bool,
) -> tuple[InternalJobScope, AccessFence] | None:
    """Recipe J: lock the job's ORIGINAL access epoch; None means authority revoked.

    Legacy rows without a captured epoch are quarantined (never rebased onto the current epoch),
    and an admission denial or any fence that differs from the original also returns None. The
    caller rolls back and marks the job failed ``authority_revoked`` in a fresh transaction.
    """
    if type(multi_workspace_enabled) is not bool:
        raise TypeError("The configured multi-workspace feature flag must be a boolean")
    if job.membership_revision is None or job.configuration_revision is None:
        return None
    scope = InternalJobScope(
        workspace_id=job.workspace_id, actor_user_id=job.owner_id, membership_revision=job.membership_revision,
    )
    original = AccessFence(job.workspace_id, job.owner_id, job.membership_revision, job.configuration_revision)
    try:
        fence = await workspaces.lock_access_fence(
            session, scope=scope, expected=original, multi_workspace_enabled=multi_workspace_enabled,
        )
    except (HTTPException, DomainError) as exc:
        if error_status(exc) in {401, 403, 404, 409}:
            return None
        raise
    return (scope, fence) if fence == original else None


def browser_capability_verified() -> bool:
    """Report whether network isolation and the gateway browser/tool probe are accepted.

    Single gate for registration, the specialist tool list and the control callbacks.
    Stays False until S4 records that proof; never derive it from config or health.
    """
    return False


async def _erase_evidence_in_uow(session: AsyncSession, job_ids: list[UUID] | tuple[UUID, ...]) -> int:
    """Delete private page rows (bytes, text and URLs) for the given jobs; caller commits."""
    from sqlalchemy import delete

    result = await session.execute(
        delete(BrowserPageEvidence).where(BrowserPageEvidence.job_id.in_(tuple(job_ids)))
    )
    return int(cast("CursorResult[Any]", result).rowcount or 0)


async def _fail_job(
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    job_id: UUID, code: str,
) -> "BrowserReadResult":
    """Write a terminal failed status for a rejected service result; terminal rows are untouched."""
    async with session_factory() as session:
        row = await session.scalar(select(BrowserReadJob).where(
            BrowserReadJob.id == job_id,
        ).with_for_update())
        if row is None:
            raise LookupError("Browser job not found")
        if row.status in {"queued", "running"}:
            row.status, row.error_code = "failed", code
        await session.commit()
        return BrowserReadResult(job=_read(row), pages=())


class BrowserReadArgs(BaseModel):
    """Accept only one granted source identifier and a strict bounded page count."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    source_id: UUID
    max_pages: StrictInt = Field(ge=1, le=3)


@dataclass(frozen=True)
class BrowserReadBudget:
    """Carry one current Agents authority object and immutable remaining ceilings."""

    authorization: BrowserRunAuthorization
    operation_id: UUID
    max_bytes: int
    max_active_seconds: int
    service_token_hash: str
    expires_at: datetime


def derive_browser_job_token(secret: str, operation_id: UUID, claim_generation: int) -> str:
    """Derive a per-operation 256-bit bearer from the server secret and exact claim identity."""
    if not secret or type(claim_generation) is not int or claim_generation < 1:
        raise ValueError("Browser operation token inputs are invalid")
    message = f"bbd-browser-job:{operation_id}:{claim_generation}".encode("ascii")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


class BrowserReadJobRead(BaseModel):
    """Expose durable job state without exposing service tokens, raw bytes, or local paths."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    operation_id: UUID
    run_id: UUID
    tool_slot: int
    source_id: UUID
    status: Literal[
        "queued", "running", "succeeded", "cancel_requested", "cancelled",
        "failed", "uncertain", "expired",
    ]
    actual_pages: int
    actual_bytes: int
    result_hash: str | None
    error_code: str | None
    expires_at: datetime


class BrowserPageRead(BaseModel):
    """Expose exact persisted observation identity and bounded extracted text, never raw bytes."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    requested_url: str
    final_url: str
    observed_at: datetime
    content_digest: str
    extracted_text: str = Field(max_length=20_000)


class BrowserReadResult(BaseModel):
    """Return a current, bounded browser result with exact page evidence references."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    job: BrowserReadJobRead
    pages: tuple[BrowserPageRead, ...] = Field(max_length=3)


def _read(row: BrowserReadJob) -> BrowserReadJobRead:
    """Map one private durable job to its credential-free public view."""
    return BrowserReadJobRead(
        id=row.id, operation_id=row.operation_id, run_id=row.run_id,
        tool_slot=row.tool_slot, source_id=row.source_id, status=row.status,
        actual_pages=row.actual_pages, actual_bytes=row.actual_bytes,
        result_hash=row.result_hash, error_code=row.error_code, expires_at=row.expires_at,
    )


async def submit_browser_read_in_uow(
    session: AsyncSession,
    run_id: UUID,
    tool_slot: int,
    auth_session_hash: str,
    grant: AgentBrowserScope,
    args: BrowserReadArgs,
    budget: BrowserReadBudget,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> BrowserReadJobRead:
    """Flush one unique durable job per run slot after Agents and Connectors authorization.

    The caller owns the transaction and must commit before dispatch. Exact retry
    returns the existing record; changed input conflicts, so observations never
    rerun under a completed slot.
    """
    _require_owner(scope)
    if type(multi_workspace_enabled) is not bool:
        raise TypeError("The configured multi-workspace feature flag must be a boolean")
    authorization = budget.authorization
    if (
        authorization.owner_id != _actor(scope) or access_fence.workspace_id != scope.workspace_id
        or grant.workspace_id != scope.workspace_id
        or not grant.enabled or grant.source_id != args.source_id
        or authorization.run_id != run_id
        or authorization.tool_slot != tool_slot
        or args.source_id not in authorization.source_ids
        or authorization.auth_session_hash != auth_session_hash
        or authorization.arguments_hash == ""
        or type(budget.max_bytes) is not int or not 1 <= budget.max_bytes <= 5 * 1024 * 1024
        or not 1 <= budget.max_active_seconds <= 45
        or args.max_pages > authorization.remaining_pages
        or budget.max_bytes > authorization.remaining_bytes
        or len(budget.service_token_hash) != 64
        or not isinstance(budget.operation_id, UUID)
        or budget.expires_at.tzinfo is None
    ):
        raise PermissionError("Browser job authorization is invalid")
    existing = await session.scalar(select(BrowserReadJob).where(
        BrowserReadJob.workspace_id == scope.workspace_id,
        BrowserReadJob.owner_id == _actor(scope),
        BrowserReadJob.run_id == run_id,
        BrowserReadJob.tool_slot == tool_slot,
    ).with_for_update())
    if existing is not None:
        if (
            existing.arguments_hash != authorization.arguments_hash
            or existing.source_id != grant.source_id
            or existing.scope_hash != grant.scope_hash
            or existing.auth_session_hash != auth_session_hash
        ):
            raise ValueError("Browser tool slot was already used with different arguments")
        return _read(existing)
    row = BrowserReadJob(
        id=uuid7(), operation_id=budget.operation_id, workspace_id=scope.workspace_id,
        owner_id=_actor(scope), membership_revision=access_fence.membership_revision,
        configuration_revision=access_fence.configuration_revision, run_id=run_id,
        tool_slot=tool_slot, auth_session_hash=auth_session_hash,
        conversation_id=authorization.conversation_id,
        profile_id=authorization.profile_id,
        authorized_source_ids=sorted(str(item) for item in authorization.source_ids),
        profile_revision_hash=authorization.profile_revision_hash,
        claim_generation=authorization.claim_generation, source_id=grant.source_id,
        source_generation=grant.source_generation, connector_revision=grant.connector_revision,
        grant_revision=grant.grant_revision, scope_hash=grant.scope_hash,
        arguments_hash=authorization.arguments_hash, max_pages=args.max_pages,
        max_bytes=budget.max_bytes, max_active_seconds=budget.max_active_seconds,
        service_token_hash=budget.service_token_hash, expires_at=budget.expires_at,
        status="queued",
    )
    session.add(row)
    await session.flush()
    return _read(row)


async def cancel_browser_job_in_uow(
    session: AsyncSession, job_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> BrowserReadJobRead:
    """Record cancellation intent under the job lock; caller commits before remote cleanup."""
    _require_owner(scope)
    if type(multi_workspace_enabled) is not bool:
        raise TypeError("The configured multi-workspace feature flag must be a boolean")
    row = await session.scalar(select(BrowserReadJob).where(
        BrowserReadJob.id == job_id, BrowserReadJob.workspace_id == scope.workspace_id,
        BrowserReadJob.owner_id == _actor(scope),
    ).with_for_update())
    if row is None:
        raise LookupError("Browser job not found")
    if row.status in {"queued", "running", "uncertain"}:
        row.cancel_requested = True
        row.status = "cancel_requested"
    elif row.status == "succeeded":
        # Evidence is erased below, so the job must not stay a valid success.
        row.status = "cancelled"
    await _erase_evidence_in_uow(session, [row.id])
    await session.flush()
    return _read(row)


@dataclass
class _JobAuthority:
    """Fresh authority evidence gathered for one browser job under the Sources lock."""

    scope: InternalJobScope
    fence: AccessFence
    source: Any
    source_view: Any
    grant: AgentBrowserScope | None
    session_valid: bool
    run_valid: bool


@dataclass
class _DispatchPlan:
    """Everything needed to call the isolated browser service after the authority transaction ends."""

    payload: dict[str, Any]
    service_url: str
    shared_token: str
    job_token: str
    operation_id: UUID
    grant: AgentBrowserScope


@dataclass
class _ValidatedPages:
    """Service pages that passed identity, scope, size and digest checks, ready to persist."""

    reads: list[BrowserPageRead]
    rows: list[BrowserPageEvidence]
    raw_total: int
    result_hash: str
    service_result_hash: str


def _job_identity(job: BrowserReadJob) -> tuple[Any, ...]:
    """Return the fields that must not change between claim, dispatch and publication."""
    return (
        job.operation_id, job.source_id, job.run_id, job.tool_slot,
        job.claim_generation, job.auth_session_hash, job.profile_id,
    )


async def _collect_job_authority(
    session: AsyncSession, candidate: BrowserReadJob, *, multi_workspace_enabled: bool,
) -> _JobAuthority | None:
    """Lock the Source, then read grant, session and run authority for ``candidate``.

    Returns ``None`` when the job's original access epoch can no longer be locked (the caller rolls
    back and fails the job). Lock order: access fence, Source, then read-only revalidations; the
    Tools job row is locked afterwards by the caller so Sources and Agents precede Tools.
    """
    from core.auth.public import revalidate_account_session
    from modules.agents.public import revalidate_browser_run_authority
    from modules.connectors import public as connectors
    from modules.sources import public as sources

    authority = await _job_authority(session, candidate, multi_workspace_enabled=multi_workspace_enabled)
    if authority is None:
        return None
    job_scope, fence = authority
    source = await sources.lock_source(
        session, candidate.source_id, scope=job_scope, multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=fence,
    )
    source_view = await sources.get_connector_source(
        session, candidate.source_id, scope=job_scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    grant = await connectors.resolve_agent_browser_scope(
        session, candidate.owner_id, candidate.source_id,
        scope=job_scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    auth = BrowserRunAuthorization(
        candidate.owner_id, candidate.run_id, candidate.tool_slot, candidate.arguments_hash,
        candidate.auth_session_hash, candidate.conversation_id, candidate.profile_id,
        candidate.profile_revision_hash,
        frozenset(UUID(item) for item in candidate.authorized_source_ids),
        candidate.claim_generation, 0, 0, 0, candidate.max_active_seconds,
    )
    session_valid = await revalidate_account_session(
        session, candidate.auth_session_hash, candidate.owner_id,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    run_valid = await revalidate_browser_run_authority(
        session, auth, scope=job_scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    return _JobAuthority(job_scope, fence, source, source_view, grant, session_valid, run_valid)


def _grant_matches_job(authority: _JobAuthority, row: BrowserReadJob) -> bool:
    """Return whether the live Source, grant and session still equal what the job was admitted under."""
    grant, source = authority.grant, authority.source
    return bool(
        source is not None and source.status == "active"
        and source.generation == row.source_generation
        and authority.source_view is not None
        and grant is not None and grant.enabled
        and grant.scope_hash == row.scope_hash
        and grant.connector_revision == row.connector_revision
        and grant.grant_revision == row.grant_revision
        and authority.session_valid and authority.run_valid
    )


async def _fail_locked_job(
    session: AsyncSession, row: BrowserReadJob, code: str, authority: _JobAuthority,
    *, multi_workspace_enabled: bool,
) -> BrowserReadResult:
    """Mark the locked job failed with ``code``, commit under its original fence and report it."""
    row.status = "failed"
    row.error_code = code
    await commit_with_replay(
        session, [], scope=authority.scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=authority.fence,
    )
    return BrowserReadResult(job=_read(row), pages=())


async def _prepare_dispatch(
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    job_id: UUID, claim_generation: int, settings: Any,
    *, deadline: float, multi_workspace_enabled: bool,
) -> _DispatchPlan | BrowserReadResult:
    """Revalidate authority under locks and build the service request, or fail the job.

    Raises ``LookupError``/``PermissionError`` when the job vanished or its claim is stale. Lock
    order: access fence, Source, then the Tools job row. A revoked authority or a token mismatch
    commits a failed job and returns its result without any network call.
    """
    import time

    async with session_factory() as session:
        candidate = await session.scalar(select(BrowserReadJob).where(BrowserReadJob.id == job_id))
        if candidate is None:
            raise LookupError("Browser job not found")
        if candidate.claim_generation != claim_generation or candidate.status != "queued":
            raise PermissionError("Browser job claim is no longer dispatchable")
        identity = _job_identity(candidate)
        authority = await _collect_job_authority(
            session, candidate, multi_workspace_enabled=multi_workspace_enabled,
        )
        if authority is None:
            await session.rollback()
            return await _fail_job(session_factory, job_id, "authority_revoked")
        # Lock Tools only after Sources and Agents; an owner cancellation may
        # have changed the durable job while authority was being revalidated.
        row = await session.scalar(select(BrowserReadJob).where(
            BrowserReadJob.id == job_id,
        ).with_for_update().execution_options(populate_existing=True))
        if row is None or identity != _job_identity(row) or row.status != "queued" or row.cancel_requested:
            raise PermissionError("Browser job claim is no longer dispatchable")
        grant = authority.grant
        if not _grant_matches_job(authority, row) or grant is None:
            return await _fail_locked_job(
                session, row, "authority_revoked", authority, multi_workspace_enabled=multi_workspace_enabled,
            )
        service_secret = settings.browser_shared_token.get_secret_value()
        job_token = derive_browser_job_token(service_secret, row.operation_id, claim_generation)
        if not hmac.compare_digest(hashlib.sha256(job_token.encode("ascii")).hexdigest(), row.service_token_hash):
            return await _fail_locked_job(
                session, row, "token_mismatch", authority, multi_workspace_enabled=multi_workspace_enabled,
            )
        target_url = grant.origin + ("" if grant.path_prefix == "/" else grant.path_prefix)
        payload: dict[str, Any] = {
            "job_id": str(row.id), "operation_id": str(row.operation_id),
            "claim_generation": row.claim_generation,
            "service_instance_id": "", "job_token": job_token,
            "target_url": target_url, "origin": grant.origin,
            "path_prefix": grant.path_prefix, "max_pages": row.max_pages,
            "timeout_seconds": min(row.max_active_seconds, max(1, int(deadline - time.monotonic()))),
        }
        return _DispatchPlan(
            payload, str(settings.browser_service_url).rstrip("/"), service_secret, job_token,
            row.operation_id, grant,
        )


async def _call_browser_service(
    plan: _DispatchPlan, *, deadline: float,
) -> tuple[dict[str, object] | None, int, bool]:
    """POST the read to the isolated service; return ``(response_data, http_status, interrupted)``.

    Fetches the service instance identity first, then streams the bounded (8 MiB) envelope under
    the remaining deadline. Transport failures yield ``(None, status_or_0, False)``; a cancellation
    sets ``interrupted`` so the caller still runs shielded cleanup before re-raising.
    """
    import time

    import httpx

    payload, shared_token = plan.payload, plan.shared_token
    response_data: dict[str, object] | None = None
    http_status = 0
    interrupted = False
    remaining = max(0.1, deadline - time.monotonic())
    try:
        async with httpx.AsyncClient(
            timeout=min(remaining, 47), trust_env=False, follow_redirects=False,
        ) as client:
            instance_response = await client.get(
                f"{plan.service_url}/agent-reads/instance",
                headers={"Authorization": f"Bearer {shared_token}"},
            )
            instance_response.raise_for_status()
            instance_data = instance_response.json()
            service_instance_id = instance_data.get("service_instance_id")
            if not isinstance(service_instance_id, str) or not service_instance_id or len(service_instance_id) > 128:
                raise ValueError("Browser service identity is invalid")
            payload["service_instance_id"] = service_instance_id
            payload["timeout_seconds"] = min(
                int(payload["timeout_seconds"]), max(1, int(deadline - time.monotonic()))
            )
            async with asyncio.timeout(max(0.1, deadline - time.monotonic())):
                async with client.stream(
                    "POST", f"{plan.service_url}/agent-reads", json=payload,
                    headers={
                        "Authorization": f"Bearer {shared_token}",
                        "X-Browser-Job-Token": plan.job_token,
                    },
                ) as response:
                    http_status = response.status_code
                    response_data = await _read_service_envelope(response)
    except httpx.HTTPStatusError as exc:
        http_status = exc.response.status_code
    except asyncio.CancelledError:
        # Cancelled mid-dispatch: still run cleanup below, then propagate.
        interrupted = True
    except (httpx.HTTPError, TimeoutError, ValueError, TypeError, json.JSONDecodeError):
        http_status = 0
    return response_data, http_status, interrupted


async def _read_service_envelope(response: Any) -> dict[str, object]:
    """Read a streamed service response into a JSON object, enforcing the 8 MiB envelope bound.

    Raises ``ValueError`` for an oversized or non-object body and ``httpx.HTTPStatusError`` for a
    4xx/5xx status (after the body is drained so the connection is reusable).
    """
    import httpx

    length = response.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > 8 * 1024 * 1024:
        raise ValueError("Browser service envelope exceeds its limit")
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > 8 * 1024 * 1024:
            raise ValueError("Browser service envelope exceeds its limit")
        body.extend(chunk)
    if response.status_code >= 400:
        raise httpx.HTTPStatusError(
            "Browser service rejected operation", request=response.request, response=response
        )
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("Browser service response is invalid")  # noqa: TRY004  # ValueError is part of the contract; TypeError would change behavior
    return value


async def _recover_failed_dispatch(
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    plan: _DispatchPlan, job_id: UUID, claim_generation: int, http_status: int,
) -> BrowserReadResult:
    """Resolve a failed dispatch: prove cleanup or mark the guard uncertain.

    Called under ``asyncio.shield`` so a timeout or cancellation cannot leave the job running with
    an active guard; the guard's expiry is the last-resort recovery. Busy (429) is returned before
    task start; other outcomes need a service cleanup acknowledgement or proof that no guard was
    registered. Opens separate short sessions for the guard read and the locked job update.
    """
    import httpx

    from core.heavy_work.remote import get_remote_heavy_guard, mark_remote_heavy_uncertain_in_uow

    payload, operation_id = plan.payload, plan.operation_id
    cleaned = http_status == 429
    if not cleaned and isinstance(payload.get("service_instance_id"), str):
        try:
            async with httpx.AsyncClient(
                timeout=2, trust_env=False, follow_redirects=False,
            ) as client:
                cancel_response = await client.post(
                    f"{plan.service_url}/agent-reads/{job_id}/cancel",
                    json={
                        "job_id": str(job_id), "operation_id": str(operation_id),
                        "claim_generation": claim_generation,
                        "service_instance_id": payload["service_instance_id"],
                        "job_token": plan.job_token,
                    },
                    headers={
                        "Authorization": f"Bearer {plan.shared_token}",
                        "X-Browser-Job-Token": plan.job_token,
                    },
                )
                cleaned = cancel_response.status_code == 200 and cancel_response.json().get("cleaned") is True
        except (httpx.HTTPError, ValueError, TypeError):
            cleaned = False
    async with session_factory() as session:
        guard = await get_remote_heavy_guard(session, operation_id)
        if guard is None or guard.state == "cleared":
            cleaned = True
    async with session_factory() as session:
        row = await session.scalar(select(BrowserReadJob).where(
            BrowserReadJob.id == job_id,
        ).with_for_update())
        if row is None:
            raise LookupError("Browser job not found")
        if cleaned:
            row.status = "failed"
            row.error_code = "service_busy" if http_status == 429 else (
                "capability_unverified"
                if http_status == 403 and row.service_instance_id is None
                else "browser_denied"
            )
        else:
            row.status = "uncertain"
            row.error_code = "remote_uncertain"
            try:
                await mark_remote_heavy_uncertain_in_uow(session, row.operation_id)
            except LookupError:
                # Registration may not have committed before the callback response was lost.
                pass
        await session.commit()
        return BrowserReadResult(job=_read(row), pages=())


def _validate_service_pages(
    response_data: dict[str, object], plan: _DispatchPlan, job_id: UUID, claim_generation: int,
) -> _ValidatedPages:
    """Verify the service result's identity, bounds, target scope and digests, building page rows.

    Raises ``PermissionError`` when a page URL leaves the source grant, and ``ValueError``/
    ``TypeError``/``KeyError`` (including base64 and timestamp errors) for any malformed or oversized
    evidence. Pure; performs no I/O.
    """
    import base64

    from modules.connectors.public import agent_browser_target_in_scope

    payload = plan.payload
    pages_wire = response_data.get("pages")
    if (
        response_data.get("job_id") != str(job_id)
        or response_data.get("operation_id") != str(plan.operation_id)
        or response_data.get("claim_generation") != claim_generation
        or response_data.get("service_instance_id") != payload["service_instance_id"]
        or not isinstance(pages_wire, list) or not 1 <= len(pages_wire) <= int(payload["max_pages"])
    ):
        raise ValueError("Browser service result identity or bounds are invalid")
    if not all(isinstance(page, dict) for page in pages_wire):
        raise ValueError("Browser page evidence is invalid")
    service_result_hash = hashlib.sha256(json.dumps(
        [{key: value for key, value in page.items() if key != "raw_content"} for page in pages_wire],
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    persisted: list[BrowserPageRead] = []
    raw_total = 0
    persisted_rows: list[BrowserPageEvidence] = []
    for index, item in enumerate(pages_wire, start=1):
        if not isinstance(item, dict):
            raise ValueError("Browser page evidence is invalid")  # noqa: TRY004  # ValueError is part of the contract; TypeError would change behavior
        raw_value, text_value = item.get("raw_content"), item.get("extracted_text")
        if (
            not agent_browser_target_in_scope(plan.grant, str(item.get("requested_url", "")))
            or not agent_browser_target_in_scope(plan.grant, str(item.get("final_url", "")))
        ):
            raise PermissionError("Browser page target is outside the source grant")
        if not isinstance(raw_value, str) or not isinstance(text_value, str) or len(text_value.encode("utf-8")) > 20_000:
            raise ValueError("Browser page evidence exceeds its bound")
        raw = base64.b64decode(raw_value, validate=True)
        raw_total += len(raw)
        if raw_total > 5 * 1024 * 1024:
            raise ValueError("Browser raw result exceeds its bound")
        observed = datetime.fromisoformat(str(item.get("observed_at")))
        if observed.tzinfo is None:
            raise ValueError("Browser observation time must be timezone aware")
        digest = hashlib.sha256(raw).hexdigest()
        if item.get("content_digest") != digest:
            raise ValueError("Browser page digest does not match its raw bytes")
        page = BrowserPageEvidence(
            id=uuid7(), job_id=job_id, page_number=index,
            requested_url=str(item.get("requested_url", "")),
            final_url=str(item.get("final_url", "")), observed_at=observed,
            content_digest=digest, raw_content=raw, extracted_text=text_value,
        )
        persisted_rows.append(page)
        persisted.append(BrowserPageRead(
            id=page.id, requested_url=page.requested_url, final_url=page.final_url,
            observed_at=page.observed_at, content_digest=page.content_digest,
            extracted_text=page.extracted_text,
        ))
    result_hash = hashlib.sha256(json.dumps(
        [{"id": str(page.id), "digest": page.content_digest} for page in persisted],
        sort_keys=True, separators=(",", ":"),
    ).encode("ascii")).hexdigest()
    return _ValidatedPages(persisted, persisted_rows, raw_total, result_hash, service_result_hash)


async def _publish_browser_pages(
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    job_id: UUID, claim_generation: int, response_data: dict[str, object], pages: _ValidatedPages,
    *, multi_workspace_enabled: bool,
) -> BrowserReadResult:
    """Publish validated evidence only after a second full authority and guard check under locks.

    Lock order: access fence, Source, then the Tools job row; the remote heavy guard is read after.
    Raises ``PermissionError`` when the claim, identity or running state changed (a cancelled job is
    committed as ``cancelled`` first). Stale authority, guard or totals commit a failed job instead.
    """
    from core.heavy_work.remote import get_remote_heavy_guard

    async with session_factory() as session:
        candidate = await session.scalar(select(BrowserReadJob).where(BrowserReadJob.id == job_id))
        if candidate is None or candidate.claim_generation != claim_generation:
            raise PermissionError("Browser result claim is no longer current")
        identity = _job_identity(candidate)
        authority = await _collect_job_authority(
            session, candidate, multi_workspace_enabled=multi_workspace_enabled,
        )
        if authority is None:
            await session.rollback()
            return await _fail_job(session_factory, job_id, "authority_revoked")
        # Source and Agent locks fence against revoke/cancel before the job row
        # and its private page evidence are published.
        row = await session.scalar(select(BrowserReadJob).where(
            BrowserReadJob.id == job_id,
        ).with_for_update().execution_options(populate_existing=True))
        if row is None or identity != _job_identity(row):
            raise PermissionError("Browser result identity changed before publication")
        if row.cancel_requested or row.status != "running":
            if row.status in {"queued", "running", "cancel_requested", "uncertain"}:
                row.status = "cancelled"
            await commit_with_replay(
                session, [], scope=authority.scope, multi_workspace_enabled=multi_workspace_enabled,
                access_fence=authority.fence,
            )
            raise PermissionError("Browser result was cancelled before publication")
        guard = await get_remote_heavy_guard(session, row.operation_id)
        if (
            not _grant_matches_job(authority, row)
            or response_data.get("service_instance_id") != row.service_instance_id
            or guard is None or guard.state != "cleared"
            or guard.service_instance_id != row.service_instance_id
            or guard.remote_job_id != row.id
            or not hmac.compare_digest(guard.nonce_hash, row.service_token_hash)
            or row.actual_pages != len(pages.rows)
            or row.actual_bytes != pages.raw_total
            or row.result_hash != pages.service_result_hash
        ):
            return await _fail_locked_job(
                session, row, "authority_revoked", authority, multi_workspace_enabled=multi_workspace_enabled,
            )
        session.add_all(pages.rows)
        row.actual_pages = len(pages.reads)
        row.actual_bytes = pages.raw_total
        row.result_hash = pages.result_hash
        row.status = "succeeded"
        row.error_code = None
        row.service_instance_id = str(response_data["service_instance_id"])
        await session.flush()
        await commit_with_replay(
            session, [], scope=authority.scope, multi_workspace_enabled=multi_workspace_enabled,
            access_fence=authority.fence,
        )
        return BrowserReadResult(job=_read(row), pages=tuple(pages.reads))


async def execute_browser_read(
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    job_id: UUID,
    claim_generation: int,
    *,
    deadline: float,
    multi_workspace_enabled: bool,
) -> BrowserReadResult:
    """Await one isolated service job directly, then persist only after fresh local authority.

    The caller already holds global heavy admission. The service registers its
    durable remote guard before external requests, authorizes each pinned target
    through the API, and acknowledges cleanup before returning. Unknown transport
    outcomes remain uncertain and are never reexecuted for this slot.
    """
    from core.config import Settings

    if type(multi_workspace_enabled) is not bool:
        raise TypeError("The configured multi-workspace feature flag must be a boolean")
    settings = Settings()
    plan = await _prepare_dispatch(
        session_factory, job_id, claim_generation, settings,
        deadline=deadline, multi_workspace_enabled=multi_workspace_enabled,
    )
    if isinstance(plan, BrowserReadResult):
        return plan
    response_data, http_status, interrupted = await _call_browser_service(plan, deadline=deadline)
    if response_data is None or interrupted:
        outcome = await asyncio.shield(
            _recover_failed_dispatch(session_factory, plan, job_id, claim_generation, http_status),
        )
        if interrupted:
            raise asyncio.CancelledError
        return outcome
    try:
        pages = _validate_service_pages(response_data, plan, job_id, claim_generation)
    except PermissionError:
        return await _fail_job(session_factory, job_id, "scope_violation")
    except (ValueError, TypeError, KeyError):
        return await _fail_job(session_factory, job_id, "invalid_result")
    return await _publish_browser_pages(
        session_factory, job_id, claim_generation, response_data, pages,
        multi_workspace_enabled=multi_workspace_enabled,
    )


async def read_browser_result(
    session: AsyncSession,
    auth_session_hash: str,
    job_id: UUID,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> BrowserReadResult | None:
    """Return successful observations only while the original session/run/Chat/profile/source grant stays current."""
    from sqlalchemy import select

    from core.auth.public import revalidate_account_session
    from modules.agents.public import BrowserRunAuthorization, revalidate_browser_run_authority
    from modules.connectors import public as connectors
    from modules.tools.models import BrowserPageEvidence

    _require_owner(scope)
    if type(multi_workspace_enabled) is not bool:
        raise TypeError("The configured multi-workspace feature flag must be a boolean")
    owner_id = _actor(scope)
    row = await session.scalar(select(BrowserReadJob).where(
        BrowserReadJob.id == job_id, BrowserReadJob.workspace_id == scope.workspace_id,
        BrowserReadJob.owner_id == owner_id,
    ))
    if row is None or row.status != "succeeded" or row.expires_at <= datetime.now(row.expires_at.tzinfo):
        return None
    authorization = BrowserRunAuthorization(
        owner_id, row.run_id, row.tool_slot, row.arguments_hash, row.auth_session_hash,
        row.conversation_id, row.profile_id, row.profile_revision_hash,
        frozenset(UUID(item) for item in row.authorized_source_ids),
        row.claim_generation, 0, 0, 0, 0,
    )
    if row.auth_session_hash != auth_session_hash:
        return None
    async with session_factory() as fresh:
        if (
            not await revalidate_account_session(
                fresh, auth_session_hash, owner_id, multi_workspace_enabled=multi_workspace_enabled,
            )
            or not await revalidate_browser_run_authority(
                fresh, authorization, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
        ):
            return None
        grant = await connectors.resolve_agent_browser_scope(
            fresh, owner_id, row.source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
    if (
        grant is None or not grant.enabled or grant.scope_hash != row.scope_hash
        or grant.source_generation != row.source_generation
        or grant.connector_revision != row.connector_revision
        or grant.grant_revision != row.grant_revision
    ):
        return None
    pages = list((await session.scalars(select(BrowserPageEvidence).where(
        BrowserPageEvidence.job_id == row.id,
    ).order_by(BrowserPageEvidence.page_number))).all())
    if (
        len(pages) != row.actual_pages or len(pages) > 3
        or any(not page.raw_content or hashlib.sha256(page.raw_content).hexdigest() != page.content_digest
               for page in pages)
    ):
        return None
    result = BrowserReadResult(
        job=_read(row),
        pages=tuple(BrowserPageRead(
            id=page.id, requested_url=page.requested_url, final_url=page.final_url,
            observed_at=page.observed_at, content_digest=page.content_digest,
            extracted_text=page.extracted_text,
        ) for page in pages),
    )
    if len(result.model_dump_json().encode("utf-8")) > 64_000:
        return None
    return result


__all__ = [
    "BrowserPageRead",
    "BrowserReadArgs",
    "BrowserReadBudget",
    "BrowserReadJobRead",
    "BrowserReadResult",
    "browser_capability_verified",
    "cancel_browser_job_in_uow",
    "read_browser_result",
    "submit_browser_read_in_uow",
]
