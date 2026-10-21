"""Account-erasure checkpoint ownership; ordinary Agent admission is never bypassed."""

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.deletion_service import authorize_account_erasure
from core.auth.erasure_contracts import AccountErasureScope
from modules.agents.leases import try_agent_run_lease_in_uow
from modules.agents.models import AgentRun

_CHECKPOINT_TABLES = ('checkpoint_writes', 'checkpoint_blobs', 'checkpoints')
_ACTIVE_STATES = ('queued', 'running', 'waiting_approval')


async def erase_account_checkpoints_page(
    session: AsyncSession, scope: AccountErasureScope, *, limit: int = 25,
) -> bool:
    """Cancel a bounded run page and erase its saver rows only after its execution lease ends.

    The deletion receipt and original workspace bind every row. Transaction-level run leases
    conflict with active saver sessions until the caller commits. Missing saver tables are
    verified absent, never created here. Return False for more pages or a still-active saver;
    the caller must commit any earlier page progress before retrying.
    """
    if type(limit) is not int or not 1 <= limit <= 25:
        raise ValueError('Checkpoint erasure limit must be 1..25')
    await authorize_account_erasure(session, scope)
    present = [name for name in _CHECKPOINT_TABLES if await session.scalar(
        text('SELECT to_regclass(:name) IS NOT NULL'), {'name': name},
    )]
    # Only the pinned internal table names enter SQL; all thread IDs remain bound values.
    checkpoint_rows = [text(
        f'EXISTS (SELECT 1 FROM {name} WHERE {name}.thread_id = agent_runs.checkpoint_thread_id)'
    ) for name in present]
    rows = list((await session.scalars(select(AgentRun).where(
        AgentRun.workspace_id == scope.workspace_id,
        AgentRun.owner_id == scope.account_id,
        or_(AgentRun.status.in_(_ACTIVE_STATES), *checkpoint_rows),
    ).order_by(AgentRun.id).limit(limit))).all())
    for run in rows:
        if not await try_agent_run_lease_in_uow(session, run.id):
            return False
        run.cancel_requested = True
        run.evidence_revoked = True
        if run.status in _ACTIVE_STATES:
            run.status = 'cancelled'
            run.claim_generation += 1
        for name in present:
            await session.execute(text(f'DELETE FROM {name} WHERE thread_id = :thread_id'), {
                'thread_id': run.checkpoint_thread_id,
            })
    await session.flush()
    return len(rows) < limit
