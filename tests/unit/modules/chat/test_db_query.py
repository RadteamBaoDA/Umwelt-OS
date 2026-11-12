"""Unit tests for collected-datasource database query tool, SQL validation, and realtime/retention specs."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
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
    assert digest == "8a0c9e5682bc41dca8d9058599f8a7d9dea619c78c7c2f2b5ac07e8f9a541fa1"


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
    assert "url=null" in out["citation_rule"]
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
    numbers = await _register_evidence_for_versions(
        _Session(),  # type: ignore[arg-type]
        pairs,
        evidence,
        workspace_id=uuid4(),
        allowed_source_ids=[uuid4()],
    )
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
async def test_active_collected_providers_follow_turn_scope() -> None:
    """The hint derives from the allowed sources: unscoped = workspace, empty/invalid scope = none."""
    from modules.chat.db_query import active_collected_providers

    rows = [
        SimpleNamespace(id=uuid4(), provider="usgs", name="a"),
        SimpleNamespace(id=uuid4(), provider="emsc_quakes", name="b"),
        SimpleNamespace(id=uuid4(), provider=None, name="c"),
    ]

    class _S:
        def __init__(self) -> None:
            self.sql: list[str] = []

        async def execute(self, stmt: object) -> SimpleNamespace:
            self.sql.append(str(stmt))
            return SimpleNamespace(all=lambda: rows)

    s = _S()
    kw = {"workspace_id": uuid4(), "allow_local_only": False}
    assert await active_collected_providers(s, **kw) == ["emsc_quakes", "usgs"]  # type: ignore[arg-type]
    assert "local_only" in s.sql[0]
    n = len(s.sql)
    assert await active_collected_providers(s, source_scope=[], **kw) == []  # type: ignore[arg-type]
    assert await active_collected_providers(s, source_scope=["nope"], **kw) == []  # type: ignore[arg-type]
    assert len(s.sql) == n  # fail closed without touching the database
    await active_collected_providers(s, source_scope=[str(uuid4())], **kw)  # type: ignore[arg-type]
    assert len(s.sql) == n + 1 and "sources.id IN" in s.sql[-1]


def test_cap_tool_output_drops_trailing_rows_and_flags_truncation() -> None:
    """Oversized results shed trailing rows until they fit; small ones are untouched."""
    import json

    from modules.chat.db_query import MAX_TOOL_OUTPUT_CHARS, cap_tool_output

    small = json.dumps({"rows": [{"a": 1}]})
    assert cap_tool_output(small) == small
    rows = [{"i": i, "body": "x" * 1000} for i in range(50)]
    out = cap_tool_output(json.dumps({"mode": "sql", "row_count": 50, "rows": rows}))
    data = json.loads(out)
    assert len(out) <= MAX_TOOL_OUTPUT_CHARS
    assert data["truncated"] is True
    assert 0 < len(data["rows"]) < 50 and data["row_count"] == len(data["rows"])
    assert [r["i"] for r in data["rows"]] == list(range(len(data["rows"])))  # leading rows kept


@pytest.mark.asyncio
async def test_sql_mode_caps_each_string_cell(monkeypatch: pytest.MonkeyPatch) -> None:
    """A huge text cell is cut to MAX_SQL_CELL_CHARS."""
    import json

    from modules.chat import db_query

    async def fake_reader(*_a: object, **_k: object) -> list[dict]:
        return [{"body": "y" * 5000, "n": 1}]

    monkeypatch.setattr(db_query, "_run_sql_as_reader", fake_reader)
    out = json.loads(
        await db_query._execute_sql_mode(
            SimpleNamespace(bind=None),  # type: ignore[arg-type]
            "SELECT 1 FROM scoped_observations",
            workspace_id=uuid4(),
            allowed_source_ids=[uuid4()],
            limit=5,
            evidence_list=[],
        )
    )
    assert len(out["rows"][0]["body"]) == db_query.MAX_SQL_CELL_CHARS
    assert out["rows"][0]["n"] == 1


def test_tool_spec_hides_sql_mode_when_unavailable() -> None:
    """Without a reader DSN the spec offers structured mode only and no sql_query parameter."""
    spec = build_db_query_tool_spec(sql_enabled=False)["function"]
    props = spec["parameters"]["properties"]
    assert props["mode"]["enum"] == ["structured"]
    assert "sql_query" not in props
    assert "sql" not in spec["description"].lower()
    names = {
        t["function"]["name"]: t
        for t in build_chat_tool_specs(
            include_read_skill=False, include_db_query=True, include_db_sql=False
        )
    }
    mode = names[TOOL_QUERY_COLLECTED_DATA]["function"]["parameters"]["properties"]["mode"]
    assert mode["enum"] == ["structured"]


def test_gadget_note_omits_sql_advice_when_unavailable() -> None:
    """The prompt note drops mode='sql' when SQL mode is off and states the call budget."""
    from modules.chat._worker.setup import _gadget_note

    assert "mode='sql'" in _gadget_note({}, True, ["usgs"])
    off = _gadget_note({}, True, ["usgs"], sql_on=False)
    assert "mode='sql'" not in off and "2 tool calls per round" in off


@pytest.mark.asyncio
async def test_sql_mode_without_reader_returns_neutral_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model sees ``sql_mode_unavailable`` with no env var name."""
    import json

    from modules.chat import db_query

    async def fence(*_a: object, **_k: object) -> str:
        return "f"

    async def sources(*_a: object, **_k: object) -> list:
        return [(uuid4(), "usgs", "n")]

    class _Nested:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_a: object) -> bool:
            return False

    session = SimpleNamespace(
        begin_nested=lambda: _Nested(), execute=AsyncMock(return_value=None), bind=None
    )
    monkeypatch.setattr(db_query.workspaces, "read_access_fence", fence)
    monkeypatch.setattr(db_query, "_allowed_sources", sources)
    monkeypatch.setattr(
        db_query, "Settings", lambda: SimpleNamespace(chat_sql_reader_database_url=None)
    )
    out = await db_query.execute_db_query_tool(
        session,  # type: ignore[arg-type]
        {"mode": "sql", "sql_query": "SELECT 1 FROM scoped_observations"},
        scope=SimpleNamespace(workspace_id=uuid4()),  # type: ignore[arg-type]
        multi_workspace_enabled=False,
        expected_access_fence="f",  # type: ignore[arg-type]
        retrieval_context=None,
        allow_local_only=False,
        evidence_list=[],
    )
    assert json.loads(out) == {"error": "sql_mode_unavailable", "rows": []}
    assert "CHAT_SQL" not in out


def test_db_query_outcome_reports_counts_or_error() -> None:
    """The tool.status detail carries rows/sources on success and an error flag otherwise."""
    import json

    from modules.chat._worker.tools import _db_query_outcome

    ok = json.dumps(
        {
            "observations": [{"provider": "usgs"}, {"provider": "usgs"}],
            "documents": [{"provider": "emsc"}],
        }
    )
    assert _db_query_outcome(ok) == {"rows": 3, "sources": 2}
    empty = json.dumps({"status": "no_active_sources_in_scope", "rows": []})
    assert _db_query_outcome(empty) == {"rows": 0, "sources": 0}
    assert _db_query_outcome(json.dumps({"error": "x", "rows": []})) == {"error": 1}
    assert _db_query_outcome("not json") == {"error": 1}


@pytest.mark.asyncio
async def test_over_cap_data_calls_are_answered_with_budget_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A third data call in a round is kept (so it gets a result) but flagged over-cap."""
    from modules.chat._worker import stream

    handler = SimpleNamespace(data_round=True, available=lambda _e: True, round_cap=lambda _e: 2)
    monkeypatch.setattr(stream, "_TOOL_HANDLERS", {TOOL_QUERY_COLLECTED_DATA: handler})
    monkeypatch.setattr(stream, "_round_terminals", lambda *_a: [])
    calls = [
        {"id": f"c{i}", "name": TOOL_QUERY_COLLECTED_DATA, "arguments": "{}"} for i in range(3)
    ]
    env = SimpleNamespace(c=SimpleNamespace(response_id=uuid4()), ts=SimpleNamespace(), setup=None)
    rc = await stream._classify_round(env, None, SimpleNamespace(acc=None), calls)  # type: ignore[arg-type]
    assert rc.handled == calls and rc.over_cap == {id(calls[2])}


@pytest.mark.asyncio
async def test_register_evidence_drops_unverified_versions() -> None:
    """A version id from another workspace or a disallowed source is never registered or cited."""
    from datetime import UTC, datetime

    from modules.chat.db_query import _register_evidence_for_versions

    ws, other_ws, ok_src, bad_src = uuid4(), uuid4(), uuid4(), uuid4()
    rows = []
    for ws_id, src in ((ws, ok_src), (other_ws, ok_src), (ws, bad_src)):
        rows.append(
            SimpleNamespace(
                ws=ws_id,
                chunk_id=uuid4(),
                content="x",
                document_version_id=uuid4(),
                version_number=1,
                observed_at=datetime(2026, 11, 1, tzinfo=UTC),
                published_at=None,
                document_id=uuid4(),
                source_id=src,
                title="t",
                url=None,
                source_type="document",
                source_generation=1,
                local_only=False,
            )
        )

    class _Session:
        """Applies the statement's own workspace/source bind values like the database would."""

        async def execute(self, stmt: object) -> SimpleNamespace:
            params = stmt.compile().params.values()  # type: ignore[attr-defined]
            flat = [v for x in params for v in (x if isinstance(x, (list, tuple)) else [x])]
            kept = [r for r in rows if r.ws in flat and r.source_id in flat]
            return SimpleNamespace(all=lambda: kept)

    pairs = [(r.document_id, r.document_version_id) for r in rows]
    evidence: list = []
    numbers = await _register_evidence_for_versions(
        _Session(),  # type: ignore[arg-type]
        pairs,
        evidence,
        workspace_id=ws,
        allowed_source_ids=[ok_src],
    )
    assert [e.document_version_id for e in evidence] == [rows[0].document_version_id]
    assert list(numbers) == [rows[0].document_version_id]
    assert rows[1].document_version_id not in numbers
    assert rows[2].document_version_id not in numbers


@pytest.mark.asyncio
async def test_sql_mode_literal_foreign_version_gets_no_citation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model-written literal version id outside the fence yields no evidence and a null citation."""
    import json

    from modules.chat import db_query

    ver = uuid4()

    async def fake_reader(*_a: object, **_k: object) -> list[dict]:
        return [{"document_id": uuid4(), "document_version_id": ver}]

    class _Session:
        bind = None

        async def execute(self, _stmt: object) -> SimpleNamespace:
            return SimpleNamespace(all=list)  # the fenced lookup finds nothing

    monkeypatch.setattr(db_query, "_run_sql_as_reader", fake_reader)
    evidence: list = []
    out = json.loads(
        await db_query._execute_sql_mode(
            _Session(),  # type: ignore[arg-type]
            "SELECT 1 FROM scoped_observations",
            workspace_id=uuid4(),
            allowed_source_ids=[uuid4()],
            limit=5,
            evidence_list=evidence,
        )
    )
    assert evidence == []
    assert "citation" not in out["rows"][0]


@pytest.mark.asyncio
async def test_register_evidence_numbers_in_row_order_not_query_order() -> None:
    """Rows returned by SQL in reverse order still get [1], [2], [3] in the caller's row order."""
    from datetime import UTC, datetime

    from modules.chat.db_query import _register_evidence_for_versions

    pairs = [(uuid4(), uuid4()) for _ in range(3)]
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
        for doc, ver in reversed(pairs)
    ]

    class _Session:
        async def execute(self, _stmt: object) -> SimpleNamespace:
            return SimpleNamespace(all=lambda: chunk_rows)

    evidence: list = []
    numbers = await _register_evidence_for_versions(
        _Session(),  # type: ignore[arg-type]
        pairs,
        evidence,
        workspace_id=uuid4(),
        allowed_source_ids=[uuid4()],
    )
    assert [numbers[ver] for _doc, ver in pairs] == [1, 2, 3]
    assert [e.document_version_id for e in evidence] == [ver for _doc, ver in pairs]


def test_finalize_keeps_in_range_markers_for_tool_evidence() -> None:
    """Tool-appended evidence (no prompt evidence, no web run) keeps both in-range markers."""
    from modules.chat._worker.finalize import _validate_citations
    from modules.chat.schemas import AnswerContext, AnswerContextRequest
    from tests.unit.modules.chat.test_rest_refactor_characterization import _evidence

    ctx = AnswerContext(query="q")
    ctx.evidence.extend(_evidence(f"quake record {n}") for n in range(25))
    cited = _validate_citations(
        "Guam [3] Oaxaca [17]", ctx, AnswerContextRequest(query="q"), None, []
    )
    assert len(cited.valid_citations) == 2
    assert not cited.uncited


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "queries", "expected_rows"),
    [([], 0, 0), (["not-a-uuid", None], 0, 0), (None, 1, 1)],
)
async def test_allowed_sources_fails_closed_on_provided_scope(
    scope: list[str] | None, queries: int, expected_rows: int
) -> None:
    """A provided scope with no valid id permits nothing; ``None`` keeps the whole-workspace default."""
    from modules.chat.db_query import _allowed_sources

    row = SimpleNamespace(id=uuid4(), provider="p", name="n")
    executed: list[object] = []

    class _S:
        async def execute(self, stmt: object) -> SimpleNamespace:
            executed.append(stmt)
            return SimpleNamespace(all=lambda: [row])

    got = await _allowed_sources(
        _S(),  # type: ignore[arg-type]
        workspace_id=uuid4(),
        source_scope=scope,  # type: ignore[arg-type]
        allow_local_only=True,
    )
    assert len(got) == expected_rows
    assert len(executed) == queries


@pytest.mark.asyncio
async def test_enforced_empty_source_scope_skips_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gadget turn with an empty scope must not search (empty source_ids would mean whole workspace)."""
    from modules.chat import retrieval
    from modules.chat.schemas import AnswerContextRequest

    search = AsyncMock()
    monkeypatch.setattr(retrieval.search_public, "search", search)
    collector = retrieval._RefCollector()
    request = AnswerContextRequest(query="q", source_scope_enforced=True)
    await retrieval._collect_search_refs(None, None, None, request, {}, collector, [])  # type: ignore[arg-type]
    search.assert_not_called()
    assert collector.refs == []
