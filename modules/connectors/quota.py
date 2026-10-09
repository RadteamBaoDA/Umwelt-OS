"""Durable provider quota ledger: PostgreSQL is the authority, Redis may only pre-reject.

One physical provider send debits every applicable window atomically in one savepoint and the
caller commits BEFORE transmitting. Exhaustion leaves counters unchanged and performs no I/O.
Ambiguity is conservative: a debit that committed but never reached the wire is not refunded.
No credential, private URL parameter or payload is stored; credential budgets key on a
deployment-keyed HMAC fingerprint shared across workspaces.
"""

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from modules.connectors.models import (
    ConnectorProviderSend,
    ConnectorQuotaDebit,
    ConnectorQuotaWindow,
)
from modules.connectors.provider_specs import (
    QUOTA_POLICY_REVISION,
    QuotaWindowPolicy,
    get_provider_spec,
)

INSTANCE_GLOBAL_SUBJECT = "instance-global"
_W = ConnectorQuotaWindow


class QuotaDeferred(Exception):  # a control-flow signal, not an error
    """No provider I/O may happen before ``defer_until``; counters were left unchanged."""

    def __init__(self, policy_key: str, defer_until: datetime) -> None:
        super().__init__(f"Provider budget {policy_key} exhausted until {defer_until.isoformat()}")
        self.policy_key = policy_key
        self.defer_until = defer_until


class SendPermitConsumed(Exception):  # a consumed permit is never replayable
    """The (request, admission, sequence) send already committed; it cannot authorize another transmission."""


def credential_fingerprint(secret: str, *, deployment_key: str) -> str:
    """Deployment-keyed fingerprint so a shared free key has one budget across workspaces; never the key."""
    return hmac.new(deployment_key.encode(), secret.encode(), hashlib.sha256).hexdigest()


def window_bounds(window: str, now: datetime) -> tuple[datetime, datetime]:
    """UTC-floored ``[start, end)`` for a policy window."""
    now = now.astimezone(UTC)
    if window == "second":
        start = now.replace(microsecond=0)
        return start, start + timedelta(seconds=1)
    if window == "minute":
        start = now.replace(second=0, microsecond=0)
        return start, start + timedelta(minutes=1)
    if window == "day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)
    if window == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(year=start.year + (start.month == 12), month=start.month % 12 + 1)
        return start, end
    raise ValueError("Unknown quota window")


def subject_hash(
    policy: QuotaWindowPolicy, provider_id: str, *, credential: str | None, egress: str | None,
) -> str:
    """Shared-budget subject. Never includes a workspace; unknown credential/egress is instance-global."""
    if policy.budget_kind == "credential":
        subject = f"credential:{provider_id}:{credential or INSTANCE_GLOBAL_SUBJECT}"
    elif policy.budget_kind == "ip":
        subject = f"ip:{egress or INSTANCE_GLOBAL_SUBJECT}"
    else:
        subject = f"provider:{provider_id}"
    return hashlib.sha256(subject.encode()).hexdigest()


def _plan(
    provider_id: str, now: datetime, credential: str | None, egress: str | None,
) -> list[tuple[QuotaWindowPolicy, str, datetime, datetime]]:
    spec = get_provider_spec(provider_id)
    if spec is None:
        raise ValueError("Provider has no quota policy")
    rows = []
    for policy in spec.quota:
        start, end = window_bounds(policy.window, now)
        rows.append((policy, subject_hash(policy, provider_id, credential=credential, egress=egress), start, end))
    # Lexicographic complete-key order keeps concurrent debits deadlock-free.
    return sorted(rows, key=lambda r: (r[0].budget_kind, r[1], r[0].policy_key, r[2]))


async def debit_send(
    session: AsyncSession, *, provider_id: str, workspace_id: UUID, request_id: UUID, admission_token: UUID,
    attempt: int, send_sequence: int, request_target: str, credential: str | None = None,
    egress: str | None = None, now: datetime | None = None,
) -> UUID:
    """Record one physical send and debit all its windows atomically; flush only, the caller commits.

    ``request_target`` must already be redacted; only its digest is stored. Raises
    ``QuotaDeferred`` (no counters changed) or ``SendPermitConsumed`` (permit never replayable).
    """
    now = now or datetime.now(UTC)
    plan = _plan(provider_id, now, credential, egress)
    send_id = uuid4()
    async with session.begin_nested():
        inserted = (await session.execute(
            pg_insert(ConnectorProviderSend).values(
                send_id=send_id, request_id=request_id, workspace_id=workspace_id,
                admission_token=admission_token, attempt=attempt, send_sequence=send_sequence,
                provider_id=provider_id, request_target_digest=hashlib.sha256(request_target.encode()).hexdigest(),
            ).on_conflict_do_nothing(constraint="uq_connector_provider_sends_sequence")
            .returning(ConnectorProviderSend.send_id)
        )).first()
        if inserted is None:
            raise SendPermitConsumed
        for policy, subject, start, end in plan:
            limit, cost = policy.limit_units, policy.cost_per_send
            if limit is not None and cost > limit:
                raise QuotaDeferred(policy.policy_key, end)
            admitted = (await session.execute(
                pg_insert(_W).values(
                    provider_id=provider_id, budget_kind=policy.budget_kind, subject_hash=subject,
                    policy_key=policy.policy_key, window_start=start, window_end=end, unit=policy.unit,
                    limit_units=limit, used_units=cost, policy_revision=QUOTA_POLICY_REVISION,
                ).on_conflict_do_update(
                    index_elements=[_W.provider_id, _W.budget_kind, _W.subject_hash, _W.policy_key, _W.window_start],
                    # Policy edits apply to the live window; consumption is never reset.
                    set_={"used_units": _W.used_units + cost, "limit_units": limit, "unit": policy.unit,
                          "policy_revision": QUOTA_POLICY_REVISION},
                    where=and_(
                        True if limit is None else _W.used_units + cost <= limit,
                        or_(_W.blocked_until.is_(None), _W.blocked_until <= now),
                    ),
                ).returning(_W.used_units)
            )).first()
            if admitted is None:
                row = (await session.execute(select(_W.blocked_until).where(
                    _W.provider_id == provider_id, _W.budget_kind == policy.budget_kind,
                    _W.subject_hash == subject, _W.policy_key == policy.policy_key, _W.window_start == start,
                ))).first()
                blocked = row[0] if row is not None else None
                raise QuotaDeferred(policy.policy_key, blocked if blocked is not None and blocked > now else end)
            session.add(ConnectorQuotaDebit(
                send_id=send_id, provider_id=provider_id, budget_kind=policy.budget_kind, subject_hash=subject,
                policy_key=policy.policy_key, window_start=start, units=cost))
        await session.flush()
    return send_id


async def defer_provider(
    session: AsyncSession, *, provider_id: str, until: datetime, credential: str | None = None,
    egress: str | None = None, now: datetime | None = None,
) -> None:
    """Persist a 429/Retry-After deadline on every live applicable window; never shortens an existing one."""
    now = now or datetime.now(UTC)
    pairs = sorted({(policy.budget_kind, subject) for policy, subject, _, _ in _plan(
        provider_id, now, credential, egress)})
    await session.execute(
        update(_W).where(
            _W.provider_id == provider_id, _W.window_end > now,
            tuple_(_W.budget_kind, _W.subject_hash).in_(pairs),
        ).values(blocked_until=func.greatest(func.coalesce(_W.blocked_until, until), until))
    )
