"""Durable ARQ dispatch, exclusive PostgreSQL claims, bounded LangGraph segments, and recovery."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import hashlib
import logging
import math
from typing import Any, AsyncIterator, cast
from uuid import UUID

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from redis.asyncio import Redis
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from core.config import Settings
from core.modules import register_modules
from core.tools import ToolRegistry
from modules.agents.harness import (
    MAX_ACTIVE_SECONDS, HarnessContext, RunCancelled, RunIncompatible, RunLimitReached, StrictJsonSerializer,
    build_workflow,
)
from modules.agents.models import AgentApproval, AgentEffect, AgentRun, AgentToolCall
from modules.agents.public import (
    APPROVAL_PROMPT_VERSION, APPROVAL_WORKFLOW_VERSION, CHECKPOINT_SCHEMA_VERSION,
    PROMPT_VERSION, WORKFLOW_VERSION, SPECIALIST_CHECKPOINT_SCHEMA_VERSION,
    SPECIALIST_PROMPT_VERSION, SPECIALIST_WORKFLOW_VERSION,
    publish_agent_activity_safely,
)
from modules.agents.approvals import expire_pending_approvals
from modules.tools.builtins import register_builtin_tools
from modules.tools.webhook import register_webhook_tool
from modules.tools.browser import register_browser_tool
from modules.agents.handoff import register_handoff_tool
from modules.tools.public import McpAdmission, McpRuntime

logger = logging.getLogger(__name__)
SEGMENT_TIMEOUT_SECONDS = 145
RECOVERY_AFTER_SECONDS = 150
RECOVERY_ACCOUNTING_SECONDS = 150
MAX_RECONCILE_ROWS = 25
_activity_reconcile_cursor: UUID | None = None


def _advisory_key(run_id: UUID) -> int:
    """Derive a signed PostgreSQL advisory-lock key scoped to this run identifier."""
    raw = int.from_bytes(hashlib.blake2b(run_id.bytes, digest_size=8, person=b"bbd-agent").digest(), "big")
    return raw if raw < 2**63 else raw - 2**64


@asynccontextmanager
async def _run_lease(engine: AsyncEngine, run_id: UUID) -> AsyncIterator[tuple[AsyncConnection, int] | None]:
    """Hold one session-level PostgreSQL advisory lock for the whole execution segment.

    A second worker never TTL-steals this lease while its original backend can still issue
    requests. Connection loss releases the database lock; every call boundary then fails closed.
    """
    key = _advisory_key(run_id)
    connection = await engine.connect()
    acquired = False
    try:
        acquired = bool(await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}))
        await connection.commit()
        if not acquired:
            yield None
            return
        yield connection, key
    finally:
        if acquired:
            try:
                await connection.scalar(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                await connection.commit()
            except Exception:
                # Closing a lost connection releases its PostgreSQL session lock.
                pass
        await connection.close()


async def _claim_run(
    session_factory: async_sessionmaker[AsyncSession], run_id: UUID, dispatch_generation: int,
) -> AgentRun | None:
    """Claim the matching queued generation and attempt bounded linked-chat status delivery."""
    async with session_factory() as session:
        row = await session.scalar(select(AgentRun).where(AgentRun.id == run_id).with_for_update())
        if (
            row is None or row.status != "queued" or row.cancel_requested
            or row.dispatch_generation != dispatch_generation
        ):
            return None
        row.status = "running"
        row.claim_generation += 1
        row.claim_started_at = datetime.now(UTC)
        row.updated_at = datetime.now(UTC)
        row.activities = [*row.activities[-63:], {
            "kind": "status", "status": "running", "created_at": datetime.now(UTC).isoformat(),
        }]
        await session.commit()
        await session.refresh(row)
        session.expunge(row)
        claimed = row
    await publish_agent_activity_safely(
        session_factory, run_id=claimed.id, owner_id=claimed.owner_id,
        auth_session_hash=claimed.auth_session_hash, status="running",
    )
    return claimed


async def _account_segment(
    session_factory: async_sessionmaker[AsyncSession], context: HarnessContext,
    *, state: dict[str, Any] | None, outcome: str,
) -> None:
    """Persist bounded segment accounting and continuation state after rechecking authorization at approval pauses."""
    elapsed = max(0, math.ceil(context.elapsed()))
    async with session_factory() as session:
        row = await session.scalar(select(AgentRun).where(AgentRun.id == context.run_id).with_for_update())
        if row is None or row.claim_generation != context.claim_generation or row.status != "running":
            return
        row.active_seconds = min(MAX_ACTIVE_SECONDS, row.active_seconds + elapsed)
        row.claim_started_at = None
        row.updated_at = datetime.now(UTC)
        if state is not None:
            if state.get("token_usage") is not None:
                row.token_usage = state["token_usage"]
            row.token_usage_unknown = (
                row.token_usage_unknown or bool(state.get("token_usage_unknown", False))
                or context.unobservable_model_usage
            )
            fences = state.get("source_fences")
            if isinstance(fences, dict):
                row.source_fences = fences
        row.token_usage_unknown = row.token_usage_unknown or context.unobservable_model_usage
        approvals = list((await session.scalars(select(AgentApproval).where(
            AgentApproval.run_id == context.run_id,
        ).order_by(AgentApproval.id).with_for_update())).all())
        approvals_by_action = {approval.action_id: approval for approval in approvals}
        uncertain = list((await session.scalars(select(AgentEffect).where(
            AgentEffect.run_id == context.run_id, AgentEffect.state == "in_flight",
        ).order_by(AgentEffect.action_id).with_for_update())).all())
        for effect in uncertain:
            effect.state = "requires_review"
            effect.payload = None
            approval = approvals_by_action.get(effect.action_id)
            if approval is not None:
                approval.status = "requires_review"
                approval.resolved_at = datetime.now(UTC)
        await session.execute(
            update(AgentToolCall)
            .where(AgentToolCall.run_id == context.run_id, AgentToolCall.status == "started")
            .values(status="failed", error_code="segment_interrupted", completed_at=datetime.now(UTC))
        )
        if row.cancel_requested:
            row.status = "cancelled"
            row.completed_at = datetime.now(UTC)
            row.activities = [*row.activities[-63:], {
                "kind": "status", "status": "cancelled", "created_at": datetime.now(UTC).isoformat(),
            }]
            await _delete_checkpoints(session, row.checkpoint_thread_id)
        elif state is not None and state.get("waiting_approval"):
            # The run and approval rows are locked; repeat ephemeral authorization at the pause boundary.
            from core.auth.public import revalidate_owner_session
            from modules.chat.public import has_live_agent_run_link

            if (await revalidate_owner_session(session, row.auth_session_hash, row.owner_id)
                    and await has_live_agent_run_link(
                        session, row.id, row.owner_id, row.auth_session_hash,
                    )):
                row.status = "waiting_approval"
                row.activities = [*row.activities[-63:], {
                    "kind": "status", "status": "waiting_approval", "created_at": datetime.now(UTC).isoformat(),
                }]
            else:
                row.cancel_requested = True
                row.status, row.completed_at = "cancelled", datetime.now(UTC)
                row.activities = [*row.activities[-63:], {
                    "kind": "status", "status": "cancelled", "created_at": datetime.now(UTC).isoformat(),
                }]
                for approval in approvals:
                    if approval.status == "pending":
                        approval.status, approval.resolved_at = "cancelled", datetime.now(UTC)
                        approval.arguments = None
                        approval.source_fences = {}
                await _delete_checkpoints(session, row.checkpoint_thread_id)
        elif outcome == "continue" and row.active_seconds < MAX_ACTIVE_SECONDS:
            row.status = "queued"
            row.dispatch_generation += 1
            row.activities = [*row.activities[-63:], {
                "kind": "status", "status": "queued", "created_at": datetime.now(UTC).isoformat(),
            }]
        else:
            row.status = "failed"
            row.error_code = (
                "active_time_limit" if row.active_seconds >= MAX_ACTIVE_SECONDS else
                outcome if outcome == "token_budget_unavailable" else outcome
            )
            row.completed_at = datetime.now(UTC)
            row.token_usage_unknown = row.token_usage_unknown or outcome == "segment_timeout"
            await _delete_checkpoints(session, row.checkpoint_thread_id)
            row.activities = [*row.activities[-63:], {
                "kind": "status", "status": "failed", "created_at": datetime.now(UTC).isoformat(),
            }]
        await session.commit()
        published = (row.owner_id, row.auth_session_hash, row.status)
    await publish_agent_activity_safely(
        session_factory, run_id=context.run_id, owner_id=published[0],
        auth_session_hash=published[1], status=published[2],
    )


async def _finish_run(
    session_factory: async_sessionmaker[AsyncSession], context: HarnessContext,
    *, state: dict[str, Any] | None, status: str, error_code: str | None,
) -> None:
    """Persist the fenced terminal result and publish its safe linked chat status."""
    if status == "succeeded":
        if state is None or not isinstance(state.get("answer"), str) or len(state["answer"].encode()) > 32_000:
            status, error_code = "failed", "invalid_output"
        else:
            try:
                await context.authorize_remote_send(state)
            except Exception:
                status, error_code = "failed", "source_or_policy_fence_changed"
    async with session_factory() as session:
        row = await session.scalar(select(AgentRun).where(AgentRun.id == context.run_id).with_for_update())
        if row is None or row.claim_generation != context.claim_generation:
            return
        now = datetime.now(UTC)
        row.active_seconds = min(MAX_ACTIVE_SECONDS, row.active_seconds + max(0, math.ceil(context.elapsed())))
        if state is not None:
            if state.get("token_usage") is not None:
                row.token_usage = state["token_usage"]
            row.token_usage_unknown = row.token_usage_unknown or bool(state.get("token_usage_unknown", False))
        row.token_usage_unknown = row.token_usage_unknown or context.unobservable_model_usage
        if row.cancel_requested or status == "cancelled":
            row.status, row.error_code = "cancelled", None
        else:
            row.status, row.error_code = status, error_code
            if status == "succeeded" and state is not None:
                row.answer = state["answer"]
                row.source_fences = state.get("source_fences", {})
        row.claim_started_at = None
        row.completed_at = now
        row.updated_at = now
        row.activities = [*row.activities[-63:], {
            "kind": "status", "status": row.status, "created_at": now.isoformat(),
        }]
        await _delete_checkpoints(session, row.checkpoint_thread_id)
        publication = (row.owner_id, row.auth_session_hash, row.status)
        await session.commit()
        final_status = row.status
    await publish_agent_activity_safely(
        session_factory, run_id=context.run_id, owner_id=publication[0],
        auth_session_hash=publication[1], status=final_status,
    )


async def _delete_checkpoints(session: AsyncSession, thread_id: str) -> None:
    """Delete every pinned saver row for a terminal run before its private evidence can linger."""
    for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
        await session.execute(text(f"DELETE FROM {table} WHERE thread_id = :thread_id"), {"thread_id": thread_id})


async def _enqueue_generation(redis: Redis, run_id: UUID, generation: int) -> bool:
    """Enqueue one deterministic ARQ job ID while PostgreSQL remains the dispatch source of truth."""
    result = await redis.enqueue_job(
        "process_agent_run", str(run_id), generation,
        _job_id=f"agent-run:{run_id}:{generation}",
    )
    return result is not None


async def _recover_abandoned(
    session_factory: async_sessionmaker[AsyncSession], engine: AsyncEngine, row_id: UUID,
) -> bool:
    """Recover stale rows only after lease release, then attempt bounded linked-status delivery."""
    async with _run_lease(engine, row_id) as lease:
        if lease is None:
            return False
        _, key = lease
        async with session_factory() as session:
            row = await session.scalar(
                select(AgentRun).where(
                    AgentRun.id == row_id,
                    AgentRun.status == "running",
                    AgentRun.claim_started_at < func.now() - text("interval '150 seconds'"),
                ).with_for_update()
            )
            if row is None:
                return False
            row.active_seconds = min(
                MAX_ACTIVE_SECONDS,
                row.active_seconds + min(
                    RECOVERY_ACCOUNTING_SECONDS,
                    max(0, math.ceil((datetime.now(UTC) - row.claim_started_at).total_seconds()))
                    if row.claim_started_at else RECOVERY_ACCOUNTING_SECONDS,
                ),
            )
            row.token_usage_unknown = True
            row.claim_started_at = None
            approvals = list((await session.scalars(select(AgentApproval).where(
                AgentApproval.run_id == row_id,
            ).order_by(AgentApproval.id).with_for_update())).all())
            approvals_by_action = {approval.action_id: approval for approval in approvals}
            uncertain = list((await session.scalars(select(AgentEffect).where(
                AgentEffect.run_id == row_id, AgentEffect.state == "in_flight",
            ).order_by(AgentEffect.action_id).with_for_update())).all())
            for effect in uncertain:
                effect.state = "requires_review"
                effect.payload = None
                approval = approvals_by_action.get(effect.action_id)
                if approval is not None:
                    approval.status = "requires_review"
                    approval.resolved_at = datetime.now(UTC)
            if row.cancel_requested:
                row.status, row.completed_at = "cancelled", datetime.now(UTC)
                await _delete_checkpoints(session, row.checkpoint_thread_id)
            elif row.active_seconds >= MAX_ACTIVE_SECONDS:
                row.status, row.error_code, row.completed_at = "failed", "active_time_limit", datetime.now(UTC)
                await _delete_checkpoints(session, row.checkpoint_thread_id)
            else:
                row.status = "queued"
                row.dispatch_generation += 1
            row.updated_at = datetime.now(UTC)
            await session.execute(
                update(AgentToolCall)
                .where(AgentToolCall.run_id == row_id, AgentToolCall.status == "started")
                .values(status="failed", error_code="worker_interrupted", completed_at=datetime.now(UTC))
            )
            await session.commit()
            publication = (row.owner_id, row.auth_session_hash, row.status)
        await publish_agent_activity_safely(
            session_factory, run_id=row_id, owner_id=publication[0],
            auth_session_hash=publication[1], status=publication[2],
        )
        try:
            await lease[0].scalar(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
            await lease[0].commit()
        except Exception:
            pass
        # The context manager retries this unlock; closing the backend also releases its session lock.
        return True


async def _refresh_stale_mcp_tools(ctx: dict[str, object], registry: ToolRegistry, row: Any) -> None:
    """Re-project only this run's MCP connections whose worker registration is missing or drifted.

    The API registry changes on every grant edit while the worker hydrates once at startup, so a
    newly granted capability would otherwise fail revalidation. Refresh reads PostgreSQL only and
    never contacts the provider; it is deliberately not a full re-hydrate, which could unregister
    a connection another run is dispatching. Failures are swallowed: the unchanged registry keeps
    revalidation fail-closed.
    """
    runtime = ctx.get("agent_mcp_runtime")
    if runtime is None:
        return
    stale: set[str] = set()
    for name in row.allowed_tools:
        if not name.startswith("mcp."):
            continue
        expected = (row.tool_contracts or {}).get(name, {})
        current = registry.get_tool(name)
        if (current is None or current.version != expected.get("version")
                or current.schema_fingerprint != expected.get("fingerprint")):
            parts = name.split(".")
            if len(parts) == 3:
                stale.add(parts[1])
    for connection_hex in stale:
        try:
            await runtime.refresh_connection(row.owner_id, UUID(hex=connection_hex))
        except Exception:
            continue


async def process_agent_run(ctx: dict[str, object], run_id: str, dispatch_generation: int) -> None:
    """Execute one durable run segment under an exclusive lease and checkpointed graph state.

    Redis delivery is at-least-once and never owns lifecycle state. The worker validates immutable
    versions before deserializing, holds no ORM transaction during model/tool I/O, and releases its
    ARQ slot after at most 145 active seconds. All model/tool nodes fence auth, cancellation,
    module state, source generations, exact tool contracts and PostgreSQL lease ownership.
    """
    settings = cast(Settings, ctx["settings"])
    session_factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    engine = cast(AsyncEngine, ctx["db_engine"])
    redis = cast(Redis, ctx["redis"])
    registry = cast(ToolRegistry, ctx["agent_tool_registry"])
    try:
        parsed_id = UUID(run_id)
    except ValueError:
        return
    async with _run_lease(engine, parsed_id) as lease:
        if lease is None:
            return
        connection, key = lease
        row = await _claim_run(session_factory, parsed_id, dispatch_generation)
        if row is None:
            return
        supported_versions = {
            (WORKFLOW_VERSION, PROMPT_VERSION),
            (APPROVAL_WORKFLOW_VERSION, APPROVAL_PROMPT_VERSION),
        }
        legacy_compatible = (
            row.agent_id == "assistant" and (row.workflow_version, row.prompt_version) in supported_versions
            and row.checkpoint_schema_version == CHECKPOINT_SCHEMA_VERSION and row.profile_snapshot is None
        )
        profile_snapshot = row.profile_snapshot if isinstance(row.profile_snapshot, dict) else None
        specialist_compatible = (
            profile_snapshot is not None and profile_snapshot.get("id") == row.agent_id
            and (row.workflow_version, row.prompt_version) == (
                SPECIALIST_WORKFLOW_VERSION, SPECIALIST_PROMPT_VERSION,
            )
            and row.checkpoint_schema_version == SPECIALIST_CHECKPOINT_SCHEMA_VERSION
        )
        if not (legacy_compatible or specialist_compatible):
            failed_context = HarnessContext(
                parsed_id, row.owner_id, row.claim_generation, session_factory, engine, settings, redis, registry,
                connection, key, asyncio.get_running_loop().time(), frozenset(row.allowed_tools),
                dict(row.tool_contracts), row.active_seconds,
                workflow_version=row.workflow_version, prompt_version=row.prompt_version,
                profile_snapshot=profile_snapshot,
            )
            await _finish_run(session_factory, failed_context, state=None, status="failed", error_code="incompatible_run_version")
            return
        context = HarnessContext(
            parsed_id, row.owner_id, row.claim_generation, session_factory, engine, settings, redis, registry,
            connection, key, asyncio.get_running_loop().time(), frozenset(row.allowed_tools),
            dict(row.tool_contracts), row.active_seconds,
            workflow_version=row.workflow_version, prompt_version=row.prompt_version,
            profile_snapshot=profile_snapshot,
        )
        await _refresh_stale_mcp_tools(ctx, registry, row)
        state: dict[str, Any] | None = None
        outcome = "execution_failed"
        try:
            remaining = max(0.0, MAX_ACTIVE_SECONDS - row.active_seconds)
            if remaining <= 0:
                raise RunLimitReached("Active execution budget exhausted")
            segment_timeout = min(SEGMENT_TIMEOUT_SECONDS, remaining)
            async with asyncio.timeout(segment_timeout):
                database_url: URL = make_url(settings.database_url)
                checkpoint_url = database_url.set(drivername="postgresql").render_as_string(hide_password=False)
                async with AsyncPostgresSaver.from_conn_string(
                    checkpoint_url, serde=StrictJsonSerializer(),
                ) as saver:
                    graph = build_workflow(context, saver)
                    graph_config = {
                        "configurable": {"thread_id": row.checkpoint_thread_id},
                        "recursion_limit": 32,
                    }
                    checkpoint = await saver.aget_tuple(graph_config)
                    if checkpoint is None:
                        initial: dict[str, Any] = {
                            "prompt": row.prompt, "messages": [], "pending_tool_calls": [],
                            "tool_slots": [], "tool_index": 0,
                            "source_fences": {"records": [], "source_generations": {}},
                            "answer": None, "segment_steps": 0, "segment_done": False,
                            "token_usage": row.token_usage, "token_usage_unknown": row.token_usage_unknown,
                        }
                    else:
                        initial = {"segment_steps": 0, "segment_done": False}
                        values = checkpoint.checkpoint.get("channel_values", {})
                        if profile_snapshot is not None:
                            if not isinstance(values, dict):
                                raise RunIncompatible("Profile checkpoint context is invalid")
                            context.assert_profile_checkpoint(values)
                        # Keep checkpointed partial usage when a crash happened before row accounting.
                        if row.token_usage_unknown:
                            initial["token_usage_unknown"] = True
                    if profile_snapshot is not None and checkpoint is None:
                        initial.update({
                            "profile_id": row.agent_id,
                            "profile_revision_hash": row.profile_revision_hash or "",
                            "owner_record_state": "authorized",
                        })
                    state = await graph.ainvoke(initial, graph_config)
            if not isinstance(state, dict):
                raise RuntimeError("Workflow state is invalid")
            context.assert_profile_checkpoint(state)
            await context._run_snapshot()
            if state.get("answer") is not None and not state.get("segment_done"):
                await _finish_run(session_factory, context, state=state, status="succeeded", error_code=None)
                return
            if state.get("segment_done"):
                await _account_segment(session_factory, context, state=state, outcome="continue")
                return
            raise RuntimeError("Workflow ended without a result or continuation")
        except TimeoutError:
            outcome = "segment_timeout"
        except RunCancelled:
            await _finish_run(session_factory, context, state=state, status="cancelled", error_code=None)
            return
        except RunLimitReached as exc:
            outcome = "token_budget_unavailable" if "token_budget_unavailable" in str(exc) else "run_limit_exceeded"
        except RunIncompatible:
            await _finish_run(session_factory, context, state=state, status="failed", error_code="incompatible_tool_contract")
            return
        except PermissionError:
            await _finish_run(session_factory, context, state=state, status="failed", error_code="model_policy_denied")
            return
        except Exception as exc:
            logger.warning("Agent run %s stopped after %s", parsed_id, type(exc).__name__)
        await _account_segment(session_factory, context, state=state, outcome=outcome)


async def reconcile_agent_dispatch(ctx: dict[str, object]) -> int:
    """Recover stale runs, replay bounded recent statuses, and enqueue durable queued generations.

    PostgreSQL remains authoritative; a process-local UUID cursor rotates bounded status pages so
    older terminal rows are not starved by newer runs. Restarts restart the scan, not run state.
    Repeated status publication is safe because chat deduplicates the latest status and rechecks
    its session and retention lifecycle. Queue dispatch retains its separate original oldest-first
    bounded scan, independent of the activity-delivery cursor.
    """
    global _activity_reconcile_cursor
    session_factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    engine = cast(AsyncEngine, ctx["db_engine"])
    redis = cast(Redis, ctx["redis"])
    await expire_pending_approvals(session_factory, MAX_RECONCILE_ROWS)
    async with session_factory() as session:
        stale_ids = list((await session.scalars(
            select(AgentRun.id).where(
                AgentRun.status == "running",
                AgentRun.claim_started_at < func.now() - text("interval '150 seconds'"),
            ).order_by(AgentRun.claim_started_at).limit(MAX_RECONCILE_ROWS)
        )).all())
    for run_id in stale_ids:
        await _recover_abandoned(session_factory, engine, run_id)
    status_query = select(
        AgentRun.id, AgentRun.dispatch_generation, AgentRun.owner_id,
        AgentRun.auth_session_hash, AgentRun.status,
    ).where(AgentRun.status.in_({"queued", "running", "waiting_approval", "succeeded", "failed", "cancelled"}))
    async with session_factory() as session:
        statement = status_query
        if _activity_reconcile_cursor is not None:
            statement = statement.where(AgentRun.id > _activity_reconcile_cursor)
        rows = list((await session.execute(
            statement.order_by(AgentRun.id).limit(MAX_RECONCILE_ROWS)
        )).all())
        if not rows and _activity_reconcile_cursor is not None:
            rows = list((await session.execute(
                status_query.order_by(AgentRun.id).limit(MAX_RECONCILE_ROWS)
            )).all())
    for run_id, generation, owner_id, auth_session_hash, status in rows:
        await publish_agent_activity_safely(
            session_factory, run_id=run_id, owner_id=owner_id,
            auth_session_hash=auth_session_hash, status=status,
        )
    if rows:
        _activity_reconcile_cursor = rows[-1][0]
    async with session_factory() as session:
        queued = list((await session.execute(
            select(AgentRun.id, AgentRun.dispatch_generation)
            .where(AgentRun.status == "queued", AgentRun.cancel_requested.is_(False))
            .order_by(AgentRun.created_at).limit(MAX_RECONCILE_ROWS)
        )).all())
    enqueued = 0
    for run_id, generation in queued:
        try:
            enqueued += int(await _enqueue_generation(redis, run_id, generation))
        except Exception:
            # PostgreSQL retains queued work; the next bounded reconciliation retries enqueue.
            continue
    return enqueued


async def compose_agent_registry(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
) -> tuple[ToolRegistry, McpAdmission, McpRuntime]:
    """Build worker-owned descriptors, native tools and reviewed MCP registrations independently of API state."""
    modules = register_modules()
    registry = ToolRegistry(module_registry=modules)
    declared_tools = {name for item in modules.values() if item.enabled for name in item.tools}
    if modules["tools"].enabled:
        register_builtin_tools(registry, frozenset(declared_tools))
        register_webhook_tool(registry, settings)
        register_browser_tool(registry)
        register_handoff_tool(registry, frozenset(declared_tools))
    admission = McpAdmission(redis)
    runtime = McpRuntime(
        registry, session_factory, redis, settings, admission,
        approved_destination_cidrs=settings.mcp_allowed_endpoint_cidrs,
    )
    if modules["tools"].enabled:
        await runtime.hydrate_connections(owner_id=1)
    return registry, admission, runtime
