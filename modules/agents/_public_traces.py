"""Agent run metadata listing and expired-trace redaction for retention sweeps."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.telemetry import RunMeta as _RunMeta
from core.workspaces.schemas import Scope
from modules.agents._public_types import (
    TERMINAL_STATUSES,
)
from modules.agents.access import admit
from modules.agents.models import (
    AgentApproval,
    AgentEffect,
    AgentRun,
    AgentToolCall,
)


async def list_run_meta(session: AsyncSession, limit: int) -> list[_RunMeta]:
    """Return at most ``limit`` (<=100) newest agent runs as metadata only; unknown token usage is None, never 0."""
    # Column projection: prompt/answer/activities/payload are never loaded for this metadata page.
    rows = await session.execute(
        select(
            AgentRun.id,
            AgentRun.status,
            AgentRun.error_code,
            AgentRun.created_at,
            AgentRun.updated_at,
            AgentRun.completed_at,
            AgentRun.token_usage_unknown,
            AgentRun.token_usage,
        )
        .order_by(AgentRun.created_at.desc())
        .limit(min(limit, 100))
    )
    return [
        _RunMeta(
            kind="agent",
            id=str(r.id),
            status=r.status,
            error_code=r.error_code,
            created_at=r.created_at,
            updated_at=r.updated_at,
            finished_at=r.completed_at,
            token_usage=None if r.token_usage_unknown else r.token_usage,
        )
        for r in rows
    ]


async def get_run_meta_by_id(session: AsyncSession, run_id: UUID) -> _RunMeta | None:
    """Return one metadata-only run projection by its indexed primary key."""
    row = await session.get(AgentRun, run_id)
    if row is None:
        return None
    return _RunMeta(
        kind="agent",
        id=str(row.id),
        status=row.status,
        error_code=row.error_code,
        created_at=row.created_at,
        updated_at=row.updated_at,
        finished_at=row.completed_at,
        token_usage=None if row.token_usage_unknown else row.token_usage,
    )


async def list_agent_trace_workspace_ids(
    session: AsyncSession,
    *,
    after: UUID | None = None,
    limit: int = 100,
) -> tuple[UUID, ...]:
    """Return at most ``limit`` (<=100) distinct run workspace IDs after ``after``, in ID order.

    Identity-only discovery for the retention maintenance pass: it reads no content and grants no
    authority. The caller derives a per-workspace job scope from the workspace owner, then calls
    ``redact_expired_agent_traces`` under that scope, one workspace per transaction.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Workspace discovery page size must be between 1 and 100")
    statement = select(AgentRun.workspace_id).distinct()
    if after is not None:
        statement = statement.where(AgentRun.workspace_id > after)
    return tuple(
        (await session.scalars(statement.order_by(AgentRun.workspace_id).limit(limit))).all()
    )


async def redact_expired_agent_traces(
    session: AsyncSession,
    *,
    cutoff: datetime,
    limit: int = 100,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> int:
    """Redact a bounded batch of old terminal run payloads while preserving canonical outcomes and action ledgers.

    Admission precedes the query and only this workspace's runs are candidates (predicate before LIMIT).

    Runs with unresolved approvals or reserved, in-flight, or review-required effects are excluded
    before LIMIT so they cannot starve later eligible runs, then their ledgers are locked and
    rechecked. The caller owns transaction commit.
    """
    if not 1 <= limit <= 500:
        raise ValueError("Retention batch size must be between 1 and 500")
    await admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    unresolved_approvals = (
        select(AgentApproval.run_id)
        .where(
            AgentApproval.run_id == AgentRun.id,
            AgentApproval.status.in_({"pending", "approved", "requires_review"}),
        )
        .exists()
    )
    unresolved_effects = (
        select(AgentEffect.run_id)
        .where(
            AgentEffect.run_id == AgentRun.id,
            AgentEffect.state.in_({"reserved", "in_flight", "requires_review"}),
        )
        .exists()
    )
    candidates = list(
        (
            await session.scalars(
                select(AgentRun)
                .where(
                    AgentRun.workspace_id == scope.workspace_id,
                    AgentRun.status.in_(TERMINAL_STATUSES),
                    AgentRun.completed_at < cutoff,
                    AgentRun.trace_redacted_at.is_(None),
                    ~unresolved_approvals,
                    ~unresolved_effects,
                )
                .order_by(AgentRun.completed_at, AgentRun.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    if not candidates:
        return 0
    run_ids = [row.id for row in candidates]
    # Lock and recheck child ledgers after the run locks; writers serialize their action changes
    # through the same AgentRun row, so an unresolved effect cannot race trace redaction.
    unresolved_approval_runs = set(
        (
            await session.scalars(
                select(AgentApproval.run_id)
                .where(
                    AgentApproval.run_id.in_(run_ids),
                    AgentApproval.status.in_({"pending", "approved", "requires_review"}),
                )
                .with_for_update()
            )
        ).all()
    )
    unresolved_effect_runs = set(
        (
            await session.scalars(
                select(AgentEffect.run_id)
                .where(
                    AgentEffect.run_id.in_(run_ids),
                    AgentEffect.state.in_({"reserved", "in_flight", "requires_review"}),
                )
                .with_for_update()
            )
        ).all()
    )
    now = datetime.now(UTC)
    redacted = 0
    eligible_ids: list[UUID] = []
    for row in candidates:
        if row.id in unresolved_approval_runs or row.id in unresolved_effect_runs:
            continue
        row.prompt = ""
        row.answer = None
        row.activities = []
        row.profile_snapshot = None
        row.source_fences = {}
        row.trace_redacted_at = now
        eligible_ids.append(row.id)
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            await session.execute(
                text(f"DELETE FROM {table} WHERE thread_id = :thread_id"),  # noqa: S608  # table name comes from a pinned literal tuple; thread id is bound
                {"thread_id": row.checkpoint_thread_id},
            )
        redacted += 1
    if eligible_ids:
        # Keep action identity, hashes, state, outcome and provider idempotency keys;
        # remove only resolved argument/effect payloads and their source trace details.
        await session.execute(
            update(AgentToolCall)
            .where(
                AgentToolCall.run_id.in_(eligible_ids),
            )
            .values(arguments={}, evidence_refs=[])
        )
        await session.execute(
            update(AgentApproval)
            .where(
                AgentApproval.run_id.in_(eligible_ids),
            )
            .values(arguments=None, source_fences={})
        )
        await session.execute(
            update(AgentEffect)
            .where(
                AgentEffect.run_id.in_(eligible_ids),
            )
            .values(payload=None)
        )
    await session.flush()
    return redacted
