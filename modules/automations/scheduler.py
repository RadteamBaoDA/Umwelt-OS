"""Internal rule schedules: cron slot math and the durable slot tick.

n8n remains the sole owner of collection/source polling schedules; this module only fires rules
whose trigger is ``schedule``. Slot state lives in ``automation_schedules`` (explicit timezone,
next slot, misfire policy) and is advanced in the same transaction that inserts the run, so a
crash can neither skip a slot nor fire it twice (the run identity also dedupes).

Cron semantics: five fields, ``*``, ``a``, ``a-b``, ``a,b``, ``*/n``, ``a-b/n``, ``a/n``; weekday
0 or 7 is Sunday; when both day-of-month and weekday are restricted a day matches if either does
(standard cron). ARQ's ``arq.cron`` helpers cannot express weekday sets from a cron string, so
this small parser is used instead of adding a dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modules.automations.models import Automation, AutomationRevision, AutomationSchedule

OWNER_ID = 1  # single-owner deployment
MIN_INTERVAL_SECONDS = 120  # 3600 / execution.MAX_RUNS_PER_HOUR (30): the cron floor never exceeds the cap
MIN_INTERVAL_AGENT_SECONDS = 300  # run_agent spends model tokens: no faster than every five minutes
SEARCH_DAYS = 366 * 5
_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


@dataclass(frozen=True)
class CronSpec:
    """Parsed cron: sorted minute/hour tuples, day/month/weekday sets and the star flags."""

    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    days_star: bool
    weekdays_star: bool


def _parse_field(token: str, low: int, high: int) -> frozenset[int]:
    """Expand one cron field into its value set, rejecting out-of-range values and zero steps."""
    values: set[int] = set()
    for part in token.split(","):
        rng, _, step_text = part.partition("/")
        if "/" in part and not step_text.isdigit():
            raise ValueError("invalid cron step")
        step = int(step_text) if step_text else 1
        if step < 1:
            raise ValueError("cron step must be at least 1")
        if rng == "*":
            start, end = low, high
        elif "-" in rng:
            a, _, b = rng.partition("-")
            if not (a.isdigit() and b.isdigit()):
                raise ValueError("invalid cron range")
            start, end = int(a), int(b)
        elif rng.isdigit():
            start = int(rng)
            end = high if step_text else start
        else:
            raise ValueError("invalid cron field")
        if not (low <= start <= end <= high):
            raise ValueError("cron value out of range")
        values.update(range(start, end + 1, step))
    return frozenset(values)


def parse_cron(expression: str) -> CronSpec:
    """Parse a five-field cron expression.

    Raises:
        ValueError: On a wrong field count, unsupported syntax or out-of-range value.
    """
    tokens = expression.split()
    if len(tokens) != 5:
        raise ValueError("cron must have five fields")
    minute, hour, day, month, weekday = (
        _parse_field(t, lo, hi) for t, (lo, hi) in zip(tokens, _RANGES, strict=True))
    return CronSpec(
        minutes=tuple(sorted(minute)), hours=tuple(sorted(hour)), days=day, months=month,
        weekdays=frozenset(w % 7 for w in weekday),  # 7 -> 0 (Sunday)
        days_star=tokens[2].startswith("*"), weekdays_star=tokens[4].startswith("*"),
    )


def next_slot(spec: CronSpec, tz: ZoneInfo, after: datetime) -> datetime | None:
    """Return the first slot strictly after ``after`` (UTC-aware), evaluated in wall time of ``tz``.

    Walks days then the sorted hour/minute tuples, so cost is bounded by ``SEARCH_DAYS``. Wall
    times that do not exist (DST gap) are skipped; an ambiguous wall time fires once (first
    occurrence). Returns None when no slot exists within the search window.
    """
    local = after.astimezone(tz).replace(second=0, microsecond=0, tzinfo=None) + timedelta(minutes=1)
    for offset in range(SEARCH_DAYS):
        day = local.date() + timedelta(days=offset)
        if day.month not in spec.months:
            continue
        day_ok, weekday_ok = day.day in spec.days, day.isoweekday() % 7 in spec.weekdays
        if not ((day_ok or weekday_ok) if not spec.days_star and not spec.weekdays_star else (day_ok and weekday_ok)):
            continue
        for hour in spec.hours:
            for minute in spec.minutes:
                if offset == 0 and (hour, minute) < (local.hour, local.minute):
                    continue
                naive = datetime(day.year, day.month, day.day, hour, minute)
                slot = naive.replace(tzinfo=tz).astimezone(UTC)
                if slot.astimezone(tz).replace(tzinfo=None) != naive or slot <= after:
                    continue  # nonexistent local time (DST gap)
                return slot
    return None


def min_interval_seconds(spec: CronSpec) -> int:
    """Smallest gap between consecutive slots over the next 60 slots (UTC reference clock).

    Raises:
        ValueError: If the expression never fires.
    """
    cursor, slots = datetime(2026, 1, 1, tzinfo=UTC), []
    for _ in range(61):
        nxt = next_slot(spec, ZoneInfo("UTC"), cursor)
        if nxt is None:
            break
        slots.append(nxt)
        cursor = nxt
    if not slots:
        raise ValueError("cron never fires")
    # A single slot in the window (for example yearly) is far slower than any floor.
    return int(min(((b - a).total_seconds() for a, b in zip(slots, slots[1:])), default=10**9))


def validate_schedule(cron: str, timezone: str, *, spends_model: bool) -> None:
    """Validate cron syntax and enforce the minimum interval (M5 rate cap at the source).

    Raises:
        ValueError: Invalid cron, a cron that never fires, or one faster than the allowed interval.
    """
    spec = parse_cron(cron)
    if next_slot(spec, ZoneInfo(timezone), datetime.now(UTC)) is None:
        raise ValueError("cron never fires")
    floor = MIN_INTERVAL_AGENT_SECONDS if spends_model else MIN_INTERVAL_SECONDS
    if min_interval_seconds(spec) < floor:
        raise ValueError(f"cron fires more often than every {floor // 60} minute(s)")


async def tick(factory: async_sessionmaker[AsyncSession], now: datetime | None = None) -> int:
    """Sync schedule rows with live rules, then fire every due slot once (coalescing misfires).

    Misfire policy ``coalesce``: all slots missed while the worker was down collapse into a single
    run identified by the *first* missed slot; ``next_slot`` then jumps past ``now`` so there is
    never an unbounded replay. A rule edit (new revision) resets the schedule from ``now`` without
    catch-up, so queued slots of an older revision are never fired. Returns runs created.
    """
    from modules.automations import execution  # lazy: execution imports nothing from this module's callers

    now = now or datetime.now(UTC)
    created = 0
    async with factory() as session:
        rules = (await session.execute(
            select(Automation, AutomationRevision).join(
                AutomationRevision,
                (AutomationRevision.automation_id == Automation.id) & (AutomationRevision.revision == Automation.revision),
            ).where(
                Automation.owner_id == OWNER_ID, Automation.deleted_at.is_(None), Automation.enabled.is_(True),
                AutomationRevision.trigger["type"].astext == "schedule",
            ).limit(100)
        )).all()
        live = {head.id: (head, rev) for head, rev in rules}
        existing = {row.automation_id: row for row in (await session.scalars(select(AutomationSchedule))).all()}
        for automation_id, row in existing.items():
            if automation_id not in live:
                await session.delete(row)  # paused, deleted or no longer a schedule rule
        for automation_id, (head, rev) in live.items():
            row = existing.get(automation_id)
            if row is not None and row.revision == head.revision:
                continue
            trigger = rev.trigger
            first = next_slot(parse_cron(trigger["cron"]), ZoneInfo(trigger["timezone"]), now)
            if first is None:
                continue
            stmt = insert(AutomationSchedule).values(
                automation_id=automation_id, revision=head.revision, cron=trigger["cron"],
                timezone=trigger["timezone"], next_slot=first, misfire_policy="coalesce")
            await session.execute(stmt.on_conflict_do_update(
                index_elements=[AutomationSchedule.automation_id],
                set_={"revision": head.revision, "cron": trigger["cron"], "timezone": trigger["timezone"],
                      "next_slot": first, "last_slot": None}))
        await session.flush()
        due = (await session.execute(
            select(AutomationSchedule, AutomationRevision).join(
                AutomationRevision,
                (AutomationRevision.automation_id == AutomationSchedule.automation_id)
                & (AutomationRevision.revision == AutomationSchedule.revision),
            ).where(AutomationSchedule.next_slot <= now)
            .order_by(AutomationSchedule.next_slot).limit(50)
            .with_for_update(of=AutomationSchedule, skip_locked=True)
        )).all()
        for schedule, rev in due:
            slot = schedule.next_slot
            spec, tz = parse_cron(schedule.cron), ZoneInfo(schedule.timezone)
            local = slot.astimezone(tz)
            run_id = await execution.plan_run(
                session, owner_id=OWNER_ID, rev=rev, trigger_type="schedule", trigger_key=f"slot:{slot.isoformat()}",
                trigger_event_id=None, slot=slot,
                payload={"weekday": local.isoweekday() % 7, "hour": local.hour},
                depth=1, origin_automation_id=None, origin_run_id=None,
            )
            created += run_id is not None
            nxt = next_slot(spec, tz, now)
            schedule.last_slot = slot
            if nxt is None:
                await session.delete(schedule)
            else:
                schedule.next_slot = nxt
        await session.commit()
    return created
