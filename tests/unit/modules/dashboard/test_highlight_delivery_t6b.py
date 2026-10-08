# ruff: noqa: F811
"""P15-T6b: per-rule delivery (cooldown, expiry, quiet hours, fingerprint reset)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from modules.dashboard import public
from modules.dashboard.highlights import notification_allowed
from modules.dashboard.schemas import HighlightRule
from modules.settings import public as settings_public
from tests.unit.modules.dashboard.test_highlight_rules_t6a import (
    FP_SOURCE,
    LEGACY_FINGERPRINT,
    FakeNews,
    definition_row,
    emit_spy,  # noqa: F401  (fixture)
    item,
    news,  # noqa: F401  (fixture)
    run_emit,
)

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def rule(**kw: Any) -> HighlightRule:
    return HighlightRule(**{
        "id": uuid4(), "keywords": ["rates"], "severity": "warning", "notify": True, **kw,
    })


def test_schema_bounds_and_defaults() -> None:
    assert rule().model_dump(mode="json", exclude_defaults=True).keys() == {"id", "keywords", "severity", "notify"}
    assert rule(cooldown_minutes=7 * 24 * 60).cooldown_minutes == 10080
    for bad in ({"cooldown_minutes": -1}, {"cooldown_minutes": 10081}, {"expires_at": "2026-01-01T00:00:00"},
                {"quiet_start": "22:00"}, {"quiet_start": "25:00", "quiet_end": "01:00"},
                {"quiet_start": "08:00", "quiet_end": "08:00"}):
        with pytest.raises(ValidationError):
            rule(**bad)


def test_cooldown_window() -> None:
    r = rule(cooldown_minutes=30)
    assert not notification_allowed(r, NOW, UTC, NOW - timedelta(minutes=29))
    assert notification_allowed(r, NOW, UTC, NOW - timedelta(minutes=30))
    assert notification_allowed(r, NOW, UTC, None)
    assert notification_allowed(rule(), NOW, UTC, NOW)  # cooldown 0 = off


def test_expiry() -> None:
    r = rule(expires_at=NOW)
    assert not notification_allowed(r, NOW, UTC, None)
    assert notification_allowed(r, NOW - timedelta(seconds=1), UTC, None)


def test_quiet_hours_in_owner_timezone_with_midnight_wrap() -> None:
    r = rule(quiet_start="22:00", quiet_end="07:00")
    hcm = ZoneInfo("Asia/Ho_Chi_Minh")  # NOW 12:00 UTC = 19:00 local
    assert notification_allowed(r, NOW, hcm, None)
    assert not notification_allowed(r, NOW + timedelta(hours=4), hcm, None)  # 23:00 local
    assert not notification_allowed(r, NOW + timedelta(hours=11), hcm, None)  # 06:00 local
    assert notification_allowed(r, NOW + timedelta(hours=12), hcm, None)  # 07:00 local
    assert not notification_allowed(rule(quiet_start="11:00", quiet_end="13:00"), NOW, UTC, None)


CLOCK = [NOW]


@pytest.fixture
def frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    CLOCK[0] = NOW

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "Clock":
            return cls.fromtimestamp(CLOCK[0].timestamp(), tz)

    monkeypatch.setattr(public, "datetime", Clock)

    async def prefs(_s: Any) -> Any:
        return SimpleNamespace(timezone="UTC")

    monkeypatch.setattr(settings_public, "read_owner_preferences", prefs)


def dump(r: HighlightRule) -> dict[str, Any]:
    return r.model_dump(mode="json")


@pytest.mark.asyncio
async def test_emit_cooldown_suppresses_second_but_lists_both(
    monkeypatch: pytest.MonkeyPatch, news: FakeNews, emit_spy: list[Any], frozen: None,
) -> None:
    r = rule(cooldown_minutes=60)
    items = [item(FP_SOURCE, "rates up"), item(FP_SOURCE, "rates down")]
    session = run_emit(monkeypatch, definition_row([dump(r)], [FP_SOURCE]), items)
    matches = await public.evaluate_gadget_highlights(session, 1, uuid4(), emit_notifications=True)  # type: ignore[arg-type]
    assert len(matches) == 2 and len(emit_spy) == 1
    assert session.progress.rule_last_notified == {str(r.id): NOW.isoformat()}


@pytest.mark.asyncio
async def test_emit_expired_and_quiet_never_notify_but_are_listed(
    monkeypatch: pytest.MonkeyPatch, news: FakeNews, emit_spy: list[Any], frozen: None,
) -> None:
    expired = rule(expires_at=NOW - timedelta(days=1))
    quiet = rule(quiet_start="11:00", quiet_end="13:00")
    session = run_emit(monkeypatch, definition_row([dump(expired), dump(quiet)], [FP_SOURCE]), [item(FP_SOURCE, "rates")])
    matches = await public.evaluate_gadget_highlights(session, 1, uuid4(), emit_notifications=True)  # type: ignore[arg-type]
    assert len(matches) == 2 and emit_spy == []
    assert session.progress.rule_last_notified == {}


@pytest.mark.asyncio
async def test_state_resets_when_fingerprint_changes_and_legacy_fingerprint_holds(
    monkeypatch: pytest.MonkeyPatch, news: FakeNews, emit_spy: list[Any], frozen: None,
) -> None:
    legacy = {"id": "00000000-0000-0000-0000-0000000000a1", "keywords": ["rates"], "severity": "warning", "notify": True}
    session = run_emit(monkeypatch, definition_row([legacy], [FP_SOURCE]), [])
    await public.evaluate_gadget_highlights(session, 1, uuid4(), emit_notifications=True)  # type: ignore[arg-type]
    assert session.progress.rules_fingerprint == LEGACY_FINGERPRINT
    session.progress.rule_last_notified = {legacy["id"]: NOW.isoformat()}
    # Same rules: state kept (and unknown rule ids pruned).
    session.progress.rule_last_notified[str(UUID(int=7))] = NOW.isoformat()
    await public.evaluate_gadget_highlights(session, 1, uuid4(), emit_notifications=True)  # type: ignore[arg-type]
    assert set(session.progress.rule_last_notified) == {legacy["id"]}
    # Cooldown edit changes the fingerprint: state resets.
    session.definition.highlight_rules = [{**legacy, "cooldown_minutes": 5}]
    await public.evaluate_gadget_highlights(session, 1, uuid4(), emit_notifications=True)  # type: ignore[arg-type]
    assert session.progress.rules_fingerprint != LEGACY_FINGERPRINT
    assert session.progress.rule_last_notified == {}


@pytest.fixture
def dedupe_emit(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """emit double with real dedupe semantics: a repeated key returns False."""
    import modules.notifications.public as notif

    keys: list[str] = []

    async def spy(_s: Any, _o: int, payload: Any, **_kw: Any) -> bool:
        if payload.dedupe_key in keys:
            return False
        keys.append(payload.dedupe_key)
        return True

    monkeypatch.setattr(notif, "emit", spy)
    return keys


async def evaluate(session: Any) -> list[Any]:
    return await public.evaluate_gadget_highlights(session, 1, uuid4(), emit_notifications=True)


@pytest.mark.asyncio
async def test_quiet_hours_suppress_not_defer_across_runs(
    monkeypatch: pytest.MonkeyPatch, news: FakeNews, dedupe_emit: list[str], frozen: None,
) -> None:
    r = rule(quiet_start="11:00", quiet_end="13:00")
    session = run_emit(monkeypatch, definition_row([dump(r)], [FP_SOURCE]), [item(FP_SOURCE, "rates")])
    await evaluate(session)
    assert dedupe_emit == [] and len(session.suppressed) == 1
    CLOCK[0] = NOW + timedelta(hours=2)  # 14:00, outside quiet hours
    await evaluate(session)
    assert dedupe_emit == []


@pytest.mark.asyncio
async def test_expired_rule_writes_no_suppression_row(
    monkeypatch: pytest.MonkeyPatch, news: FakeNews, dedupe_emit: list[str], frozen: None,
) -> None:
    r = rule(expires_at=NOW - timedelta(days=1))
    session = run_emit(monkeypatch, definition_row([dump(r)], [FP_SOURCE]), [item(FP_SOURCE, "rates")])
    await evaluate(session)
    assert dedupe_emit == [] and len(session.suppressed) == 0


@pytest.mark.asyncio
async def test_cooldown_suppresses_not_defers_across_runs(
    monkeypatch: pytest.MonkeyPatch, news: FakeNews, dedupe_emit: list[str], frozen: None,
) -> None:
    r = rule(cooldown_minutes=60)
    items = [item(FP_SOURCE, "rates up"), item(FP_SOURCE, "rates down")]
    session = run_emit(monkeypatch, definition_row([dump(r)], [FP_SOURCE]), items)
    await evaluate(session)
    assert len(dedupe_emit) == 1
    CLOCK[0] = NOW + timedelta(minutes=61)
    await evaluate(session)
    assert len(dedupe_emit) == 1  # first is deduped, second stays suppressed
    # Persisted cooldown state alone also blocks a brand-new match inside the window.
    CLOCK[0] = NOW + timedelta(minutes=30)
    session.progress.rule_last_notified = {str(r.id): NOW.isoformat()}
    session.suppressed.clear()
    session.definition.highlight_rules = [dump(r)]
    await evaluate(session)
    assert len(dedupe_emit) == 1


@pytest.mark.asyncio
async def test_fingerprint_change_clears_suppression(
    monkeypatch: pytest.MonkeyPatch, news: FakeNews, dedupe_emit: list[str], frozen: None,
) -> None:
    r = rule(quiet_start="11:00", quiet_end="13:00")
    session = run_emit(monkeypatch, definition_row([dump(r)], [FP_SOURCE]), [item(FP_SOURCE, "rates")])
    await evaluate(session)
    assert len(session.suppressed) == 1
    session.definition.revision = 2
    session.definition.highlight_rules = []
    await evaluate(session)  # no rules: early return, nothing cleared yet
    session.definition.highlight_rules = [dump(rule(quiet_start="11:00", quiet_end="13:00"))]
    await evaluate(session)
    assert len(session.suppressed) == 1 and dedupe_emit == []


@pytest.mark.asyncio
async def test_cooldown_not_armed_when_emit_returns_false(
    monkeypatch: pytest.MonkeyPatch, news: FakeNews, frozen: None,
) -> None:
    import modules.notifications.public as notif

    results = [False, True]
    calls: list[Any] = []

    async def spy(*args: Any, **_kw: Any) -> bool:
        calls.append(args)
        return results.pop(0)

    monkeypatch.setattr(notif, "emit", spy)
    r = rule(cooldown_minutes=60)
    items = [item(FP_SOURCE, "rates up"), item(FP_SOURCE, "rates down")]
    session = run_emit(monkeypatch, definition_row([dump(r)], [FP_SOURCE]), items)
    await evaluate(session)
    assert len(calls) == 2
    assert session.progress.rule_last_notified == {str(r.id): NOW.isoformat()}


@pytest.mark.asyncio
async def test_failed_emit_leaves_cooldown_unarmed(
    monkeypatch: pytest.MonkeyPatch, news: FakeNews, frozen: None,
) -> None:
    import modules.notifications.public as notif

    async def spy(*_a: Any, **_kw: Any) -> bool:
        return False

    monkeypatch.setattr(notif, "emit", spy)
    r = rule(cooldown_minutes=60)
    session = run_emit(monkeypatch, definition_row([dump(r)], [FP_SOURCE]), [item(FP_SOURCE, "rates")])
    await evaluate(session)
    assert session.progress.rule_last_notified == {}


def test_quiet_hours_dst_fall_back_new_york() -> None:
    ny = ZoneInfo("America/New_York")
    r = rule(quiet_start="01:00", quiet_end="03:00")
    for hour, minute, allowed in ((5, 30, False), (6, 30, False), (8, 30, True)):  # 01:30 EDT, 01:30 EST, 03:30 EST
        assert notification_allowed(r, datetime(2026, 11, 1, hour, minute, tzinfo=UTC), ny, None) is allowed


def test_owner_tzinfo_falls_back_to_utc() -> None:
    for name in ("Bad/Zone", "", "America"):
        assert public._owner_tzinfo(name) is UTC
