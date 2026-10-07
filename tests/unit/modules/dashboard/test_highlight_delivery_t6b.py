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


@pytest.fixture
def frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "Clock":
            return cls.fromtimestamp(NOW.timestamp(), tz)

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
