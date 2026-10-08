"""P1 durable quota ledger: compiled SQL and control flow against a fake session (no DB, no network)."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Self
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from modules.connectors import quota
from modules.connectors.models import ConnectorQuotaDebit
from modules.connectors.provider_specs import get_provider_spec

NOW = datetime(2026, 10, 8, 13, 45, 30, 123456, tzinfo=UTC)


class FakeSession:
    """Scripted execute results; records compiled statements and added ORM rows."""

    def __init__(self, results: list[Any]) -> None:
        self.results = list(results)
        self.sql: list[str] = []
        self.added: list[Any] = []
        self.savepoint_exit: Any = "unset"

    def begin_nested(self) -> "FakeSession":
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, *_: object) -> bool:
        self.savepoint_exit = exc_type
        return False

    async def execute(self, statement: Any) -> Any:
        self.sql.append(str(statement.compile(dialect=postgresql.dialect())))
        value = self.results.pop(0) if self.results else None
        return SimpleNamespace(first=lambda: value)

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None


def _kwargs(**over: Any) -> dict[str, Any]:
    base = {
        "provider_id": "binance", "workspace_id": uuid4(), "request_id": uuid4(), "admission_token": uuid4(),
        "attempt": 1, "send_sequence": 0, "request_target": "https://data-api.binance.vision/api/v3/ticker/price",
        "now": NOW,
    }
    return base | over


def test_window_bounds_are_utc_floors() -> None:
    assert quota.window_bounds("second", NOW)[0] == datetime(2026, 10, 8, 13, 45, 30, tzinfo=UTC)
    assert quota.window_bounds("minute", NOW)[1] == datetime(2026, 10, 8, 13, 46, tzinfo=UTC)
    assert quota.window_bounds("day", NOW) == (datetime(2026, 10, 8, tzinfo=UTC), datetime(2026, 10, 9, tzinfo=UTC))
    december = datetime(2026, 12, 31, 23, 59, tzinfo=UTC)
    assert quota.window_bounds("month", december) == (
        datetime(2026, 12, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC))


def test_subject_hash_has_no_workspace_and_shares_credentials() -> None:
    policy = next(w for w in get_provider_spec("coingecko").quota if w.budget_kind == "credential")  # type: ignore[union-attr]
    fingerprint = quota.credential_fingerprint("secret-key", deployment_key="deploy")
    assert "secret-key" not in fingerprint
    same = quota.subject_hash(policy, "coingecko", credential=fingerprint, egress=None)
    assert same == quota.subject_hash(policy, "coingecko", credential=fingerprint, egress="other-egress")
    assert same != quota.subject_hash(policy, "coingecko", credential=None, egress=None)
    other = quota.credential_fingerprint("secret-key", deployment_key="other")
    assert other != fingerprint


async def test_debit_orders_windows_and_debits_each_atomically() -> None:
    # send insert, then one upsert per window (3 for binance)
    session = FakeSession([("sent",), (1,), (1,), (1,)])
    send_id = await quota.debit_send(session, **_kwargs())  # type: ignore[arg-type]
    assert send_id is not None
    assert session.savepoint_exit is None
    assert "connector_provider_sends" in session.sql[0] and "ON CONFLICT" in session.sql[0]
    upserts = session.sql[1:]
    assert len(upserts) == 3
    for statement in upserts:
        assert "ON CONFLICT (provider_id, budget_kind, subject_hash, policy_key, window_start) DO UPDATE" in statement
        assert "used_units + " in statement.replace("connector_quota_windows.used_units + ", "used_units + ")
        assert "blocked_until" in statement
        assert "workspace" not in statement
    assert sum("used_units + %(used_units_2)s" in statement and "<=" in statement for statement in upserts) == 2
    units = {(d.policy_key, d.units) for d in session.added if isinstance(d, ConnectorQuotaDebit)}
    assert units == {
        ("pilot_request_weight_utc_day", 2), ("pilot_request_weight_utc_minute", 2),
        ("official_request_weight_unknown", 2),
    }


async def test_exhausted_window_defers_without_provider_io_and_rolls_back_savepoint() -> None:
    # send ok, first window ok, second window refused; blocked_until row then looked up
    session = FakeSession([("sent",), (1,), None, (None,)])
    with pytest.raises(quota.QuotaDeferred) as caught:
        await quota.debit_send(session, **_kwargs())  # type: ignore[arg-type]
    assert caught.value.defer_until > NOW
    assert session.savepoint_exit is quota.QuotaDeferred  # savepoint unwound: counters unchanged


async def test_blocked_until_wins_over_window_end() -> None:
    blocked = datetime(2026, 10, 8, 13, 50, tzinfo=UTC)
    session = FakeSession([("sent",), None, (blocked,)])
    with pytest.raises(quota.QuotaDeferred) as caught:
        await quota.debit_send(session, **_kwargs(provider_id="usgs"))  # type: ignore[arg-type]
    assert caught.value.defer_until == blocked


async def test_replayed_send_permit_is_never_reusable() -> None:
    session = FakeSession([None])  # ON CONFLICT DO NOTHING returned no row
    with pytest.raises(quota.SendPermitConsumed):
        await quota.debit_send(session, **_kwargs())  # type: ignore[arg-type]
    assert len(session.sql) == 1  # no window was touched


async def test_unknown_provider_has_no_policy() -> None:
    with pytest.raises(ValueError, match="no quota policy"):
        await quota.debit_send(FakeSession([]), **_kwargs(provider_id="rss"))  # type: ignore[arg-type]


async def test_retry_after_only_extends_deadlines() -> None:
    session = FakeSession([])
    await quota.defer_provider(session, provider_id="coingecko", until=NOW, now=NOW)  # type: ignore[arg-type]
    statement = session.sql[0]
    assert "greatest(coalesce(connector_quota_windows.blocked_until" in statement.lower()
    assert "window_end >" in statement
