"""Unit tests for collected-datasource database query tool, SQL validation, and realtime/retention specs."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.realtime_routes import _wait_for_realtime_wakeup, notify_realtime_wakeup
from modules.chat.db_query import (
    TOOL_QUERY_COLLECTED_DATA,
    build_db_query_tool_spec,
    should_auto_enable_db_query,
    validate_readonly_sql,
)
from modules.chat.generative_ui.tool_specs import build_chat_tool_specs
from modules.chat.schemas import DB_QUERY_KEY, GADGET_SCOPE_KEY
from modules.connectors.provider_specs import (
    FREE_PROVIDER_SPECS,
    get_default_retention_days,
    get_realtime_tier,
)
from modules.connectors.public import ConnectorConfig


def test_validate_readonly_sql_accepts_scoped_views() -> None:
    """Valid read-only SELECT queries over scoped_observations and scoped_documents are accepted."""
    sql = validate_readonly_sql(
        "SELECT symbol, AVG(value) AS avg_val FROM scoped_observations GROUP BY symbol;",
    )
    assert "scoped_observations" in sql
    assert not sql.endswith(";")

    cte_sql = validate_readonly_sql(
        "WITH recent AS (SELECT title, published_at FROM scoped_documents LIMIT 5) SELECT * FROM recent",
    )
    assert cte_sql.endswith("SELECT * FROM recent")


@pytest.mark.parametrize(
    "bad_sql",
    [
        "",
        "DELETE FROM scoped_observations",
        "UPDATE scoped_observations SET value = 0",
        "SELECT * FROM scoped_observations; SELECT 1",
        "SELECT * FROM scoped_observations -- comment",
        "SELECT * FROM scoped_observations /* block */",
        "SELECT * FROM scoped_observations FOR UPDATE",
        "SELECT * FROM pg_catalog.pg_tables",
        "SELECT pg_sleep(5) FROM scoped_observations",
        "SELECT * FROM observations",
        "SELECT * FROM documents",
        "SELECT * FROM users",
        "SELECT 1",
    ],
)
def test_validate_readonly_sql_rejects_unsafe_or_unscoped_sql(bad_sql: str) -> None:
    """Unsafe keywords, comments, multiple statements, and physical un-scoped tables are rejected."""
    with pytest.raises(ValueError):
        validate_readonly_sql(bad_sql)


def test_should_auto_enable_db_query_triggers_on_flag_gadget_or_keywords() -> None:
    """Auto-detection enables query_collected_data for explicit toggle, gadget context, or analytical/realtime prompts."""
    assert should_auto_enable_db_query("Hello", {DB_QUERY_KEY: True}) is True
    assert should_auto_enable_db_query("Hello", {DB_QUERY_KEY: {"requested": True}}) is True
    assert should_auto_enable_db_query("Hello", {"kind": "gadget"}) is True
    assert should_auto_enable_db_query("Hello", {GADGET_SCOPE_KEY: {"renderer": "finance_chart"}}) is True
    assert should_auto_enable_db_query("What is the average BTC price today?", None) is True
    assert should_auto_enable_db_query("Giá vàng mới nhất và thống kê biến động?", None) is True
    assert should_auto_enable_db_query("Write a short poem about autumn leaves.", None) is False


def test_build_chat_tool_specs_includes_db_query_when_enabled() -> None:
    """build_chat_tool_specs appends query_collected_data only when include_db_query is True."""
    default_names = {t["function"]["name"] for t in build_chat_tool_specs(include_read_skill=False)}
    assert TOOL_QUERY_COLLECTED_DATA not in default_names

    enabled_names = {
        t["function"]["name"]
        for t in build_chat_tool_specs(include_read_skill=False, include_db_query=True)
    }
    assert TOOL_QUERY_COLLECTED_DATA in enabled_names
    spec = build_db_query_tool_spec()
    assert spec["function"]["name"] == TOOL_QUERY_COLLECTED_DATA


def test_provider_specs_realtime_tier_skip_embedding_and_retention() -> None:
    """Provider specs classify realtime vs near_realtime tiers, skip vector embeddings by default, and set retention."""
    assert get_realtime_tier("coingecko") == "realtime"
    assert get_realtime_tier("binance") == "realtime"
    assert get_realtime_tier("usgs") == "realtime"
    assert get_realtime_tier("gdelt_economy") == "near_realtime"
    assert get_realtime_tier("world_bank") == "near_realtime"

    assert get_default_retention_days("coingecko") == 14
    assert get_default_retention_days("gdelt_economy") == 60
    assert get_default_retention_days("world_bank") == 365

    for spec in FREE_PROVIDER_SPECS:
        assert spec.skip_embedding is True

    cfg = ConnectorConfig(retention_days=30, skip_embedding=True)
    assert cfg.retention_days == 30
    assert cfg.skip_embedding is True


@pytest.mark.asyncio
async def test_notify_realtime_wakeup_wakes_waiter_immediately() -> None:
    """In-process realtime wakeup wakes SSE pollers without waiting for the full poll timeout."""
    fake_request = SimpleNamespace(is_disconnected=lambda: asyncio.sleep(0, result=False))
    ws_id = uuid4()

    async def trigger_soon() -> None:
        await asyncio.sleep(0.02)
        notify_realtime_wakeup(ws_id)

    task = asyncio.create_task(trigger_soon())
    await asyncio.wait_for(
        _wait_for_realtime_wakeup(fake_request, ws_id, 5.0),  # type: ignore[arg-type]
        timeout=0.5,
    )
    await task
