"""Registered browser read contract with an explicit fail-closed capability gate."""

import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from core.realtime import commit_with_replay
from core.tools import ToolDefinition, ToolRegistry, ToolResult, ToolRisk
from core.workspaces import public as workspaces
from core.workspaces.schemas import InternalJobScope, Scope


def _actor(scope: Scope) -> int:
    return scope.actor_user_id if isinstance(scope, InternalJobScope) else scope.user_id


async def _handle_browser_read(arguments: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Read bounded static pages through the isolated browser service.

    Refuses before any reservation or service call while the capability gate is false
    (defence in depth: the tool is not registered then). Manifest, health and service
    URL never establish isolation; only the accepted capability proof does.
    """
    from modules.tools.browser_public import browser_capability_verified

    if not browser_capability_verified():
        return ToolResult(success=False, error="Browser read is unavailable", error_code="tool_unavailable")
    source_id = arguments.get("source_id")
    pages = arguments.get("max_pages")
    if not isinstance(source_id, str) or type(pages) is not int or not 1 <= pages <= 3:
        return ToolResult(
            success=False, error="Browser request arguments are invalid",
            error_code="invalid_arguments",
        )
    try:
        source_uuid = UUID(source_id)
    except (TypeError, ValueError):
        return ToolResult(
            success=False, error="Browser request arguments are invalid",
            error_code="invalid_arguments",
        )
    from core.heavy_work import HeavyWorkBusy, RemoteHeavyWorkBlocked, heavy_job_slot
    from core.tools.schemas import compute_argument_hash
    from modules.agents.public import reserve_browser_run_budget_in_uow
    from modules.connectors import public as connectors
    from modules.sources import public as sources
    from modules.tools.browser_public import (
        BrowserReadArgs,
        BrowserReadBudget,
        derive_browser_job_token,
        execute_browser_read,
        read_browser_result,
        submit_browser_read_in_uow,
    )

    principal = context.get("principal")
    factory = context.get("session_factory")
    run_value = context.get("run_id")
    slot = context.get("tool_ordinal")
    claim = context.get("claim_generation")
    remaining_active = context.get("remaining_active_seconds")
    settings = context.get("settings")
    scope = getattr(principal, "scope", None)
    flag = getattr(settings, "multi_workspace_enabled", None)
    if (
        principal is None or not getattr(principal, "is_owner", False)
        or scope is None or type(flag) is not bool
        or getattr(principal, "actor_id", None) != f"owner:{_actor(scope)}"
        or not callable(factory)
        or not isinstance(run_value, str) or type(slot) is not int
        or type(claim) is not int or type(remaining_active) not in {int, float}
        or context.get("workflow_version") != "specialist-approved-v1"
    ):
        return ToolResult(success=False, error="Browser run authority is unavailable", error_code="forbidden")
    try:
        run_id = UUID(run_value)
    except ValueError:
        return ToolResult(success=False, error="Browser run authority is invalid", error_code="forbidden")
    if not settings:
        return ToolResult(success=False, error="Browser service is unavailable", error_code="tool_unavailable")
    remaining = min(45.0, float(cast("float", remaining_active)))  # type(...) in {int, float} checked above
    if remaining <= 0:
        return ToolResult(success=False, error="Browser run budget is exhausted", error_code="tool_unavailable")
    try:
        args = BrowserReadArgs(source_id=source_uuid, max_pages=pages)
        args_digest = compute_argument_hash({"source_id": source_id, "max_pages": pages})
        token_secret = settings.browser_shared_token.get_secret_value()
        # Operation identity is fresh for this proposed slot; an existing row wins on exact retry.
        from uuid import uuid4

        operation_id = uuid4()
        service_token = derive_browser_job_token(token_secret, operation_id, claim)
        async with heavy_job_slot(factory, timeout_seconds=remaining) as lease:
            async with factory() as session:
                # Source scope is locked before the AgentRun budget row, then
                # the durable Tools job is inserted in that same order.
                fence = await workspaces.lock_access_fence(
                    session, scope=scope, multi_workspace_enabled=flag,
                )
                source = await sources.lock_source(
                    session, source_uuid, scope=scope, multi_workspace_enabled=flag,
                    expected_access_fence=fence,
                )
                if source is None or source.status != "active":
                    return ToolResult(
                        success=False, error="Browser source is not enabled", error_code="forbidden",
                    )
                grant = await connectors.resolve_agent_browser_scope(
                    session, _actor(scope), source_uuid, scope=scope, multi_workspace_enabled=flag,
                )
                if grant is None or not grant.enabled:
                    return ToolResult(
                        success=False, error="Browser source is not enabled", error_code="forbidden",
                    )
                authorization = await reserve_browser_run_budget_in_uow(
                    session, run_id, claim, slot, args_digest, pages,
                    scope=scope, multi_workspace_enabled=flag,
                )
                if source_uuid not in authorization.source_ids:
                    return ToolResult(
                        success=False, error="Browser source is outside the saved profile scope",
                        error_code="forbidden",
                    )
                expires_at = datetime.now(UTC) + timedelta(hours=24)
                budget = BrowserReadBudget(
                    authorization=authorization, operation_id=operation_id,
                    max_bytes=min(5 * 1024 * 1024, authorization.remaining_bytes),
                    max_active_seconds=min(45, authorization.remaining_active_seconds),
                    service_token_hash=hashlib.sha256(service_token.encode("ascii")).hexdigest(),
                    expires_at=expires_at,
                )
                if budget.max_bytes <= 0:
                    return ToolResult(
                        success=False, error="Browser run byte budget is exhausted",
                        error_code="tool_unavailable",
                    )
                job = await submit_browser_read_in_uow(
                    session, run_id, slot, authorization.auth_session_hash,
                    grant, args, budget, scope=scope, multi_workspace_enabled=flag, access_fence=fence,
                )
                await commit_with_replay(
                    session, [], scope=scope, multi_workspace_enabled=flag, access_fence=fence,
                )
            if job.status == "succeeded":
                async with factory() as session:
                    result = await read_browser_result(
                        session, authorization.auth_session_hash, job.id, factory,
                        scope=scope, multi_workspace_enabled=flag,
                    )
                if result is None:
                    return ToolResult(success=False, error="Browser result is no longer current", error_code="authority_revoked")
            elif job.status != "queued":
                return ToolResult(success=False, error="Browser job cannot be repeated", error_code=job.error_code or "tool_unavailable")
            else:
                result = await execute_browser_read(
                    factory, job.id, claim, deadline=min(lease.deadline, time.monotonic() + remaining),
                    multi_workspace_enabled=flag,
                )
            if result.job.status != "succeeded":
                return ToolResult(
                    success=False, error="Browser capability or authority is unavailable",
                    error_code=result.job.error_code or "tool_unavailable",
                )
            data = {
                "job_id": str(result.job.id),
                "pages": [{
                    "evidence_id": str(page.id),
                    "requested_url": page.requested_url,
                    "final_url": page.final_url,
                    "observed_at": page.observed_at.isoformat(),
                    "content_digest": page.content_digest,
                    "text": page.extracted_text,
                } for page in result.pages],
            }
            if len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 64_000:
                return ToolResult(success=False, error="Browser result exceeded its output bound", error_code="invalid_result")
            return ToolResult(
                success=True, data=data,
                evidence_refs=tuple(str(page.id) for page in result.pages),
            )
    except (HeavyWorkBusy, RemoteHeavyWorkBlocked):
        return ToolResult(success=False, error="Browser capacity is unavailable", error_code="tool_unavailable")
    except (PermissionError, LookupError, ValueError):
        return ToolResult(success=False, error="Browser authority is unavailable", error_code="forbidden")
    except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
        return ToolResult(success=False, error="Browser operation did not complete", error_code="tool_unavailable")


def register_browser_tool(registry: ToolRegistry) -> None:
    """Register the exact static-read v1 schema, but only once the capability gate passes.

    While the gate is false the tool is absent, so specialists report browser reading
    as unavailable instead of offering a call that burns run budget and cannot succeed.
    """
    from modules.tools.browser_public import browser_capability_verified

    if not browser_capability_verified():
        return
    definition = ToolDefinition(
        name="browser.read",
        version="1.0.0",
        description="Read bounded static pages from an owner-enabled web source.",
        input_schema={
            "type": "object",
            "properties": {
                "source_id": {"type": "string", "format": "uuid"},
                "max_pages": {"type": "integer", "minimum": 1, "maximum": 3},
            },
            "required": ["source_id", "max_pages"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        risk=ToolRisk.READ_ONLY,
        confirmation_required=False,
        timeout_seconds=45,
        max_arguments_bytes=1024,
        max_result_bytes=64_000,
        permissions=("browser.read",),
        module="tools",
    )
    registry.register_tool(definition, _handle_browser_read)
