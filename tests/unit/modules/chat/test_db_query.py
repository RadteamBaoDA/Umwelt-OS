"""Unit tests for collected-datasource database query tool, SQL validation, and realtime/retention specs."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.realtime.routes import _wait_for_realtime_wakeup, notify_realtime_wakeup
from modules.chat.db_query import (
    TOOL_QUERY_COLLECTED_DATA,
    build_db_query_tool_spec,
    has_collected_datasource,
    is_gadget_turn,
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


def test_is_gadget_turn_ignores_legacy_flag_and_keywords() -> None:
    """Only the gadget path is request-driven; the old db_query flag and keyword matching are gone."""
    assert is_gadget_turn({"kind": "gadget"}) is True
    assert is_gadget_turn({GADGET_SCOPE_KEY: {"renderer": "finance_chart"}}) is True
    assert is_gadget_turn({DB_QUERY_KEY: True}) is False
    assert is_gadget_turn(None) is False


class _OneRowSession:
    """Session stub whose EXISTS-style query returns one row or none."""

    def __init__(self, row: object | None) -> None:
        self.row = row
        self.statements: list[object] = []

    async def execute(self, stmt: object) -> SimpleNamespace:
        """Record the statement and return the configured first row."""
        self.statements.append(stmt)
        return SimpleNamespace(first=lambda: self.row)


@pytest.mark.asyncio
async def test_has_collected_datasource_follows_active_source_presence() -> None:
    """The grant condition is the existence of an active collected source, nothing in the prompt."""
    ws = uuid4()
    yes = _OneRowSession((uuid4(),))
    no = _OneRowSession(None)
    assert await has_collected_datasource(yes, workspace_id=ws, allow_local_only=False) is True  # type: ignore[arg-type]
    assert await has_collected_datasource(no, workspace_id=ws, allow_local_only=True) is False  # type: ignore[arg-type]
    assert "local_only" in str(yes.statements[0]) and "local_only" not in str(no.statements[0])


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


def test_db_query_tool_spec_is_pinned() -> None:
    """The built tool spec is byte-stable; refactors of the builder must not change it."""
    import hashlib
    import json

    digest = hashlib.sha256(json.dumps(build_db_query_tool_spec()).encode()).hexdigest()
    assert digest == "20f34c6d5789a48c4d99f5008fe0e2c3d8e290ff251fa3d7e3804a9b611af3ed"


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


class _EmptyObsSession:
    """Session stub whose every observation SELECT returns no rows."""

    async def execute(self, _stmt: object) -> SimpleNamespace:
        """Return an empty result for both ``.scalars().all()`` and ``.all()`` shapes."""
        return SimpleNamespace(all=lambda: [], scalars=lambda: SimpleNamespace(all=lambda: []))


@pytest.mark.asyncio
async def test_structured_event_feed_provider_returns_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-measurement provider (emsc_quakes) adds documents even for target='observations'."""
    import json
    from datetime import UTC, datetime

    from modules.chat import db_query

    ver_id = uuid4()
    row = SimpleNamespace(
        document_id=uuid4(),
        document_version_id=ver_id,
        provider="emsc_quakes",
        source_name="EMSC",
        title="M4.1 - Crete",
        url="https://example.test/q",
        published_at=None,
        observed_at=datetime(2026, 11, 1, tzinfo=UTC),
        excerpt="quake",
    )

    async def fake_select(_s: object, _f: object, _l: int) -> list[object]:
        return [row]

    async def fake_register(*_a: object, **_kw: object) -> dict:
        return {ver_id: 1}

    monkeypatch.setattr(db_query, "_select_structured_documents", fake_select)
    monkeypatch.setattr(db_query, "_register_evidence_for_versions", fake_register)
    args = db_query.QueryCollectedDataArgs(target="observations", providers=["emsc_quakes"])
    out = json.loads(
        await db_query._execute_structured_mode(
            _EmptyObsSession(),  # type: ignore[arg-type]
            args,
            workspace_id=uuid4(),
            allowed_source_ids=[uuid4()],
            date_from=None,
            date_to=None,
            evidence_list=[],
            available_providers=["emsc_quakes"],
        )
    )
    assert out["documents"][0]["citation"] == "[1]"
    # Always present so the model can correct provider ids even when this call returned rows.
    assert out["available_providers"] == ["emsc_quakes"]


@pytest.mark.asyncio
async def test_structured_empty_result_lists_available_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no rows at all the payload names the active providers so the model can retry."""
    import json

    from modules.chat import db_query

    async def fake_select(_s: object, _f: object, _l: int) -> list[object]:
        return []

    async def fake_register(*_a: object, **_kw: object) -> dict:
        return {}

    monkeypatch.setattr(db_query, "_select_structured_documents", fake_select)
    monkeypatch.setattr(db_query, "_register_evidence_for_versions", fake_register)
    out = json.loads(
        await db_query._execute_structured_mode(
            _EmptyObsSession(),  # type: ignore[arg-type]
            db_query.QueryCollectedDataArgs(providers=["emsc"]),
            workspace_id=uuid4(),
            allowed_source_ids=[uuid4()],
            date_from=None,
            date_to=None,
            evidence_list=[],
            available_providers=["emsc_quakes", "usgs"],
        )
    )
    assert out["available_providers"] == ["emsc_quakes", "usgs"]


@pytest.mark.asyncio
async def test_db_query_granted_by_source_gadget_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No keyword or flag: a source grants the tool, none withholds it, a lookup error withholds it."""
    from modules.chat._worker import setup as worker_setup

    c = SimpleNamespace(scope=SimpleNamespace(workspace_id=uuid4()), response_id=uuid4())
    open_scope = SimpleNamespace(selected_only=False)
    plain = SimpleNamespace(
        req_params={DB_QUERY_KEY: True}, answer_request=open_scope
    )  # legacy flag is ignored
    gadget = SimpleNamespace(req_params={"kind": "gadget"}, answer_request=open_scope)
    selected = SimpleNamespace(
        req_params={"kind": "gadget"}, answer_request=SimpleNamespace(selected_only=True)
    )

    async def run(turn: object, row: object | None) -> bool:
        return await worker_setup._db_query_granted(_OneRowSession(row), c, turn, False)  # type: ignore[arg-type]

    assert await run(plain, (uuid4(),)) is True
    assert await run(plain, None) is False
    assert await run(gadget, None) is True
    # Selected-only turns never get the workspace-wide data tool, even with a source or on a gadget turn (S5).
    assert await run(selected, (uuid4(),)) is False

    async def boom(*_a: object, **_k: object) -> bool:
        raise RuntimeError("db down")

    monkeypatch.setattr(worker_setup, "has_collected_datasource", boom)
    assert await run(plain, (uuid4(),)) is False


@pytest.mark.asyncio
async def test_register_evidence_cites_every_returned_version() -> None:
    """All 15 returned versions get evidence and a citation number (no 12-row cap)."""
    from datetime import UTC, datetime

    from modules.chat.db_query import _register_evidence_for_versions

    pairs = [(uuid4(), uuid4()) for _ in range(15)]
    chunk_rows = [
        SimpleNamespace(
            chunk_id=uuid4(),
            content="x",
            document_version_id=ver,
            version_number=1,
            observed_at=datetime(2026, 11, 1, tzinfo=UTC),
            published_at=None,
            document_id=doc,
            source_id=uuid4(),
            title="t",
            url=None,
            source_type="document",
            source_generation=1,
            local_only=False,
        )
        for doc, ver in pairs
    ]
    seen: list[object] = []

    class _Session:
        async def execute(self, stmt: object) -> SimpleNamespace:
            seen.append(stmt)
            return SimpleNamespace(all=lambda: chunk_rows)

    evidence: list = []
    numbers = await _register_evidence_for_versions(_Session(), pairs, evidence)  # type: ignore[arg-type]
    assert len(evidence) == 15
    assert sorted(numbers.values()) == list(range(1, 16))
    assert seen[0].compile().params["param_1"] == 15  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_structured_documents_rank_per_source() -> None:
    """The documents SELECT partitions by source and orders by that rank first (fair share across feeds)."""
    from modules.chat import db_query

    captured: list[str] = []

    class _Session:
        async def execute(self, stmt: object) -> SimpleNamespace:
            captured.append(str(stmt))
            return SimpleNamespace(all=lambda: [])

    await db_query._select_structured_documents(_Session(), [], 25)  # type: ignore[arg-type]
    sql = captured[0]
    assert "row_number() OVER (PARTITION BY documents.source_id" in sql
    assert "ORDER BY anon_1.source_rank" in sql


def test_gadget_note_lists_provider_ids_and_round_budget() -> None:
    """The prompt note names exact provider ids, the headline-only text_query rule and the 2-round budget."""
    from modules.chat._worker.setup import _gadget_note

    note = _gadget_note({}, True, ["emsc_quakes", "thanhnien_news"])
    assert "emsc_quakes, thanhnien_news" in note
    assert "at most 2 tool rounds" in note and "headline/body text only" in note
    assert "Active providers" not in _gadget_note({}, True)
    assert _gadget_note({}, False, ["emsc_quakes"]) == ""


@pytest.mark.asyncio
async def test_active_collected_providers_scoped_like_grant() -> None:
    """Provider ids come from one DISTINCT select fenced by workspace, active status and local-only."""
    from modules.chat.db_query import active_collected_providers

    class _S:
        def __init__(self) -> None:
            self.sql = ""

        async def execute(self, stmt: object) -> SimpleNamespace:
            self.sql = str(stmt)
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: ["emsc_quakes"]))

    s = _S()
    assert await active_collected_providers(s, workspace_id=uuid4(), allow_local_only=False) == [  # type: ignore[arg-type]
        "emsc_quakes"
    ]
    assert "DISTINCT" in s.sql and "local_only" in s.sql
    await active_collected_providers(s, workspace_id=uuid4(), allow_local_only=True)  # type: ignore[arg-type]
    assert "local_only" not in s.sql
