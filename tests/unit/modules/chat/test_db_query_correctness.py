"""Correctness pass for ``query_collected_data``: arguments, dates, timezone, aggregates, truncation, SQL colons, errors."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError

from modules.chat import _db_scope, db_query
from modules.chat.db_query import (
    DATA_NOTICE,
    TRUNCATION_HINT,
    QueryCollectedDataArgs,
    _apply_aggregate_citations,
    _bounded_sql,
    _is_statement_timeout,
    _observation_aggregates,
    _observation_filters,
    _parse_bound,
    _query_error_result,
    execute_db_query_tool,
    owner_zone,
)

_HCM = ZoneInfo("Asia/Ho_Chi_Minh")  # UTC+7, no DST


def _args(**kw: object) -> QueryCollectedDataArgs:
    return QueryCollectedDataArgs.model_validate(kw)


def test_date_only_bounds_are_owner_local_days() -> None:
    """date_from is local midnight; date_to is the next local midnight (exclusive, whole day)."""
    start = _parse_bound("2026-03-05", _HCM, name="date_from", end=False)
    end = _parse_bound("2026-03-05", _HCM, name="date_to", end=True)
    assert start == datetime(2026, 3, 4, 17, 0, tzinfo=UTC)
    assert end == datetime(2026, 3, 5, 17, 0, tzinfo=UTC)


def test_timestamp_bounds_naive_in_owner_zone_and_offset_kept() -> None:
    """A naive timestamp is owner-local; an explicit offset wins; an end gets +1us for exclusivity."""
    naive = _parse_bound("2026-03-05T10:00:00", _HCM, name="date_from", end=False)
    assert naive == datetime(2026, 3, 5, 3, 0, tzinfo=UTC)
    off = _parse_bound("2026-03-05T10:00:00+00:00", _HCM, name="date_from", end=False)
    assert off == datetime(2026, 3, 5, 10, 0, tzinfo=UTC)
    end = _parse_bound("2026-03-05T10:00:00+00:00", _HCM, name="date_to", end=True)
    assert end is not None and end > datetime(2026, 3, 5, 10, 0, tzinfo=UTC)
    assert _parse_bound(None, _HCM, name="date_to", end=True) is None


@pytest.mark.parametrize("bad", ["yesterday", "2026-13-40", "05/03/2026"])
def test_unparseable_date_is_an_error(bad: str) -> None:
    """A bad date is an error naming the field, never a silently dropped filter."""
    with pytest.raises(
        ValueError, match="invalid date_to: expected ISO-8601 timestamp or YYYY-MM-DD"
    ):
        _parse_bound(bad, _HCM, name="date_to", end=True)


def test_owner_zone_falls_back_to_utc() -> None:
    """Unknown or missing timezone names resolve to UTC."""
    assert owner_zone("Asia/Tokyo").key == "Asia/Tokyo"
    assert owner_zone("Not/AZone").key == "UTC"
    assert owner_zone(None).key == "UTC"


def test_args_forbid_unknown_keys_but_keep_legacy_aggregation() -> None:
    """Unknown arguments are rejected; the retired aggregation values still map to none."""
    with pytest.raises(ValueError, match="extra_forbidden"):
        _args(provider="binance")
    assert _args(aggregation="latest").aggregation == "none"


def _patch_fence(monkeypatch: pytest.MonkeyPatch) -> object:
    fence = object()

    async def _read(*_a: object, **_k: object) -> object:
        return fence

    monkeypatch.setattr(db_query.workspaces, "read_access_fence", _read)
    return fence


async def _call(
    monkeypatch: pytest.MonkeyPatch, session: object, raw: dict[str, object], **kw: object
) -> dict[str, object]:
    fence = _patch_fence(monkeypatch)

    async def _scopes(_session: object, ids: list, **_k: object) -> dict:
        return {sid: "d" * 64 for sid in ids}

    monkeypatch.setattr(_db_scope.connectors, "chat_scope_discriminators", _scopes)
    out = await execute_db_query_tool(
        session,  # type: ignore[arg-type]
        raw,
        scope=SimpleNamespace(workspace_id=uuid4()),  # type: ignore[arg-type]
        multi_workspace_enabled=False,
        expected_access_fence=fence,  # type: ignore[arg-type]
        retrieval_context=None,
        allow_local_only=True,
        evidence_list=[],
        **kw,  # type: ignore[arg-type]
    )
    return json.loads(out)


@pytest.mark.asyncio
async def test_invalid_arguments_report_loc_and_message_without_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validation errors list loc: msg, are bounded, and never echo the supplied value."""
    res = await _call(monkeypatch, object(), {"limit": 999, "bogus": "SECRET-VALUE"})
    err = str(res["error"])
    assert err.startswith("invalid_arguments: ")
    assert "limit" in err and "bogus" in err
    assert "SECRET-VALUE" not in err and "999" not in err
    assert len(err) <= 500 and res["rows"] == []


@pytest.mark.asyncio
async def test_invalid_date_is_reported_before_any_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unparseable date returns the date error and touches no source or table."""
    res = await _call(monkeypatch, object(), {"date_from": "last week"})
    assert res["error"] == "invalid date_from: expected ISO-8601 timestamp or YYYY-MM-DD"


class _Nested:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_a: object) -> None:
        return None


class _Sess:
    def begin_nested(self) -> _Nested:
        return _Nested()

    async def execute(self, *_a: object) -> None:
        return None


@pytest.mark.asyncio
async def test_results_carry_timezone_and_data_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Results include the resolved zone, the untrusted-data notice and owner-zone UTC date bounds."""
    seen: dict[str, object] = {}

    async def _allowed(*_a: object, **_k: object) -> list[tuple[object, str, str]]:
        return [(uuid4(), "binance", "n")]

    async def _structured(*_a: object, **k: object) -> str:
        seen.update(k)
        return json.dumps({"mode": "structured"})

    monkeypatch.setattr(db_query, "_allowed_sources", _allowed)
    monkeypatch.setattr(db_query, "_execute_structured_mode", _structured)
    out = await _call(
        monkeypatch,
        _Sess(),
        {"date_from": "2026-03-05", "date_to": "2026-03-05"},
        timezone="Asia/Ho_Chi_Minh",
    )
    assert out["timezone"] == "Asia/Ho_Chi_Minh" and out["data_notice"] == DATA_NOTICE
    assert list(seen["source_scopes"].values()) == ["d" * 64]  # type: ignore[attr-defined]
    assert seen["date_from"] == datetime(2026, 3, 4, 17, 0, tzinfo=UTC)
    assert seen["date_to"] == datetime(2026, 3, 5, 17, 0, tzinfo=UTC)


def test_date_to_filter_is_exclusive() -> None:
    """The upper bound compiles to a strict < so a date-only day covers exactly 24 local hours."""
    from sqlalchemy import and_

    filters = _observation_filters(
        _args(),
        workspace_id=uuid4(),
        allowed_source_ids=[uuid4()],
        date_from=None,
        date_to=datetime(2026, 3, 5, 17, 0, tzinfo=UTC),
    )
    sql = str(and_(*filters).compile(dialect=postgresql.dialect()))
    assert "observed_at <" in sql and "observed_at <=" not in sql


def test_sql_colons_are_escaped_but_casts_and_max_rows_stay() -> None:
    """Literal colons never become binds; ``::`` casts survive; only ``:max_rows`` is a bind."""
    stmt = _bounded_sql("SELECT '10:30', x::int, 'a:b' FROM scoped_observations")
    assert set(stmt.compile().params) == {"max_rows"}
    rendered = str(stmt.compile())
    assert "'10:30'" in rendered and "x::int" in rendered and "'a:b'" in rendered


def test_statement_timeout_detection_and_mapping() -> None:
    """sqlstate 57014 / QueryCanceledError, bare or DBAPI-wrapped, maps to query_timeout."""

    class QueryCanceledError(Exception):
        sqlstate = "57014"

    wrapped = DBAPIError("stmt", None, QueryCanceledError("canceling statement"))
    assert _is_statement_timeout(QueryCanceledError()) and _is_statement_timeout(wrapped)
    assert not _is_statement_timeout(RuntimeError("x"))
    assert json.loads(_query_error_result(wrapped))["error"].startswith("query_timeout:")
    assert json.loads(_query_error_result(RuntimeError("x")))["error"] == (
        "query_execution_failed: RuntimeError"
    )


class _AggResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def all(self) -> list[object]:
        return self._rows


class _AggSession:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows
        self.stmt: object = None

    async def execute(self, stmt: object) -> _AggResult:
        self.stmt = stmt
        return _AggResult(self.rows)


@pytest.mark.asyncio
async def test_aggregates_expose_region_nulls_first_seen_and_newest_citation() -> None:
    """Aggregates group by region, report null_values/first_observed_at, and cite the newest observation."""
    doc_new, ver_new = uuid4(), uuid4()
    t0, t1 = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
    row = SimpleNamespace(
        provider="world_bank",
        metric="gdp",
        symbol=None,
        region="VN",
        unit="usd",
        currency=None,
        row_count=3,
        null_count=1,
        avg_value=2.0,
        min_value=1.0,
        max_value=3.0,
        sum_value=6.0,
        first_observed_at=t0,
        latest_observed_at=t1,
        document_id=doc_new,
        document_version_id=ver_new,
    )
    session = _AggSession([row])
    rows = await _observation_aggregates(session, [], 5)  # type: ignore[arg-type]
    compiled = str(session.stmt.compile(dialect=postgresql.dialect()))  # type: ignore[attr-defined]
    assert "region" in compiled.split("GROUP BY")[1]
    # Citation comes from a LATERAL newest-row probe, not an ordered array_agg over every row.
    assert "array_agg" not in compiled and "LATERAL" in compiled
    assert rows[0]["region"] == "VN" and rows[0]["null_values"] == 1
    assert rows[0]["first_observed_at"] == t0.isoformat()
    _apply_aggregate_citations(rows, {ver_new: 4})
    assert rows[0]["citation"] == "[4]" and "_version_id" not in rows[0]


@pytest.mark.asyncio
async def test_structured_mode_flags_truncation_and_trims_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A truncated fetch yields truncated=true plus the narrowing hint."""
    ver = uuid4()

    def _obs(i: int) -> SimpleNamespace:
        return SimpleNamespace(
            provider="p",
            metric="m",
            symbol=None,
            region=None,
            value=float(i),
            unit=None,
            currency=None,
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
            is_current=True,
            document_id=uuid4(),
            document_version_id=ver,
        )

    async def _collect(*_a: object, **_k: object) -> tuple[list[object], list[object], bool]:
        return [], [_obs(1), _obs(2)], True

    async def _register(*_a: object, **_k: object) -> dict[object, int]:
        return {}

    async def _attrs(*_a: object, **_k: object) -> dict[object, dict[str, object]]:
        return {ver: {"depth": 10.0}}

    monkeypatch.setattr(db_query, "_collect_observations", _collect)
    monkeypatch.setattr(db_query, "_register_evidence_for_versions", _register)
    monkeypatch.setattr(db_query, "fetch_attributes", _attrs)
    out = json.loads(
        await db_query._execute_structured_mode(
            None,  # type: ignore[arg-type]
            _args(target="observations", limit=2),
            workspace_id=uuid4(),
            allowed_source_ids=[uuid4()],
            date_from=None,
            date_to=None,
            evidence_list=[],
        )
    )
    assert out["truncated"] is True and out["hint"] == TRUNCATION_HINT
    assert len(out["observations"]) == 2
    assert out["observations"][0]["attributes"] == {"depth": 10.0}
    assert out["observations"][0]["latitude"] is None


@pytest.mark.asyncio
async def test_collect_observations_fetches_limit_plus_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """The observation query asks for limit+1 rows and trims to limit."""
    rows = [SimpleNamespace(id=i) for i in range(3)]

    class _Scalars:
        def all(self) -> list[object]:
            return rows

    class _Res:
        def scalars(self) -> _Scalars:
            return _Scalars()

    class _S:
        stmt: object = None

        async def execute(self, stmt: object) -> _Res:
            self.stmt = stmt
            return _Res()

    sess = _S()
    aggs, obs, truncated = await db_query._collect_observations(
        sess,  # type: ignore[arg-type]
        _args(target="observations", limit=2),
        workspace_id=uuid4(),
        allowed_source_ids=[uuid4()],
        date_from=None,
        date_to=None,
        out={},
    )
    assert aggs == [] and len(obs) == 2 and truncated is True
    assert "LIMIT 3" in str(sess.stmt.compile(compile_kwargs={"literal_binds": True}))  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_sql_mode_fetches_one_extra_row_and_flags_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQL mode asks the reader for limit+1 rows, returns limit and flags the cut."""
    asked: dict[str, int] = {}

    async def _run(_sql: str, **k: int) -> list[dict[str, int]]:
        asked["max_rows"] = k["max_rows"]
        return [{"n": i} for i in range(3)]

    async def _register(*_a: object, **_k: object) -> dict[object, int]:
        return {}

    monkeypatch.setattr(db_query, "_run_sql_as_reader", _run)
    monkeypatch.setattr(db_query, "_register_evidence_for_versions", _register)
    out = json.loads(
        await db_query._execute_sql_mode(
            SimpleNamespace(bind=None),  # type: ignore[arg-type]
            "SELECT 1 FROM scoped_observations",
            workspace_id=uuid4(),
            allowed_source_ids=[uuid4()],
            limit=2,
            evidence_list=[],
        )
    )
    assert asked["max_rows"] == 3 and out["row_count"] == 2
    assert out["truncated"] is True and out["hint"] == TRUNCATION_HINT
    # No document_version_id selected: nothing is citable, so the model is told how to fix the SELECT.
    assert out["citation_hint"] == db_query.SQL_UNCITED_HINT


@pytest.mark.asyncio
async def test_handler_rejects_malformed_json_without_querying() -> None:
    """Unparseable or non-object tool arguments return invalid_arguments and run no query."""
    from modules.chat._worker.tools import _QueryCollectedDataHandler

    for bad in ("{not json", "[1, 2]", '"x"'):
        out = await _QueryCollectedDataHandler().run(
            SimpleNamespace(),  # type: ignore[arg-type]
            {"name": "query_collected_data", "arguments": bad},
        )
        assert json.loads(out) == {"error": "invalid_arguments: malformed JSON", "rows": []}


@pytest.mark.asyncio
async def test_aggregation_with_truncated_raw_rows_is_not_flagged_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1 group + 30 raw rows at limit 25: no top-level truncated/hint, only observations_truncated."""

    async def _aggs(*_a: object, **_k: object) -> list[dict[str, object]]:
        return [{"metric": "m", "count": 30}]

    monkeypatch.setattr(db_query, "_observation_aggregates", _aggs)

    class _Res:
        def scalars(self) -> _Res:
            return self

        def all(self) -> list[object]:
            return [SimpleNamespace(id=i) for i in range(26)]  # limit + 1 fetched

    class _S:
        async def execute(self, _stmt: object) -> _Res:
            return _Res()

    out: dict[str, object] = {}
    aggs, rows, truncated = await db_query._collect_observations(
        _S(),  # type: ignore[arg-type]
        _args(target="observations", aggregation="count", limit=25, date_from="2026-01-01"),
        workspace_id=uuid4(),
        allowed_source_ids=[uuid4()],
        date_from=None,
        date_to=None,
        out=out,
    )
    assert len(aggs) == 1 and len(rows) == 25 and truncated is False
    assert out["observations_truncated"] is True


@pytest.mark.asyncio
async def test_truncated_groups_still_report_exact_total(monkeypatch: pytest.MonkeyPatch) -> None:
    """More groups than ``limit``: the total comes from a separate COUNT and groups are flagged cut."""
    calls: list[list[object]] = []

    async def _aggs(*_a: object, **_k: object) -> list[dict[str, object]]:
        return [{"symbol": f"CVE-{i}", "count": 1} for i in range(26)]  # limit + 1

    async def _total(_session: object, filters: list[object]) -> int:
        calls.append(filters)
        return 36

    class _Res:
        def scalars(self) -> _Res:
            return self

        def all(self) -> list[object]:
            return []

    class _S:
        async def execute(self, _stmt: object) -> _Res:
            return _Res()

    monkeypatch.setattr(db_query, "_observation_aggregates", _aggs)
    monkeypatch.setattr(db_query, "_observation_total_count", _total)
    out: dict[str, object] = {}
    aggs, _rows, truncated = await db_query._collect_observations(
        _S(),  # type: ignore[arg-type]
        _args(target="observations", aggregation="count", limit=25, date_from="2026-01-01"),
        workspace_id=uuid4(),
        allowed_source_ids=[uuid4()],
        date_from=None,
        date_to=None,
        out=out,
    )
    assert len(aggs) == 25 and truncated is True and len(calls) == 1
    assert out["aggregate_total_count"] == 36
    assert out["groups_truncated"] is True
    db_query._attach_aggregates(out, aggs, {})
    assert out["aggregate_total_count"] == 36  # not overwritten by the 25-group sum


def test_compact_date_is_a_whole_local_day() -> None:
    start = _parse_bound("20260305", _HCM, name="date_from", end=False)
    end = _parse_bound("20260305", _HCM, name="date_to", end=True)
    assert start == datetime(2026, 3, 4, 17, 0, tzinfo=UTC)
    assert end == datetime(2026, 3, 5, 17, 0, tzinfo=UTC)


def test_json_cells_are_bounded() -> None:
    attrs = {f"k{i}": "v" * 5000 for i in range(200)}
    got = db_query._bound_json_cell("attributes", attrs)
    assert len(json.dumps(got)) <= 4000 and "k0" in got
    other = db_query._bound_json_cell("blob", {"a": "x" * 9000})
    assert isinstance(other, str) and len(other) == db_query.MAX_SQL_CELL_CHARS


@pytest.mark.asyncio
async def test_fence_cleanup_runs_when_rollback_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    deleted: list[object] = []

    class _Conn:
        async def execute(self, stmt: object, params: object = None) -> object:
            return SimpleNamespace(
                scalar_one=lambda: 42, mappings=lambda: SimpleNamespace(all=lambda: [])
            )

        async def rollback(self) -> None:
            raise RuntimeError("rollback failed")

    class _Ctx:
        def __init__(self, obj: object) -> None:
            self.obj = obj

        async def __aenter__(self) -> object:
            return self.obj

        async def __aexit__(self, *_a: object) -> None:
            return None

    class _App:
        async def execute(self, stmt: object, params: object = None) -> None:
            deleted.append(stmt)

    class _Engine:
        def connect(self) -> _Ctx:
            return _Ctx(_Conn())

        def begin(self) -> _Ctx:
            return _Ctx(_App())

    monkeypatch.setattr(db_query, "_reader_engine", lambda: _Engine())
    with pytest.raises(RuntimeError, match="rollback failed"):
        await db_query._run_sql_as_reader(
            "SELECT 1",
            app_engine=_Engine(),  # type: ignore[arg-type]
            workspace_id=uuid4(),
            allowed_source_ids=[],
            max_rows=1,
        )
    assert db_query._DELETE_FENCE in deleted and db_query._DELETE_FENCE_SCOPE in deleted


def test_cap_keeps_aggregate_groups_and_sheds_sample_rows_first() -> None:
    """A 36-group aggregate plus 36 sample rows over the bound loses sample rows, not groups."""
    aggs = [{"symbol": f"CVE-{i}", "count": 1, "note": "a" * 300} for i in range(36)]
    obs = [{"symbol": f"CVE-{i}", "attributes": {"b": "x" * 500}} for i in range(36)]
    raw = json.dumps({"observation_aggregates": aggs, "observations": obs})
    data = json.loads(db_query.cap_tool_output(raw))
    assert data["truncated"] is True
    assert len(data["observation_aggregates"]) == 36
    assert len(data["observations"]) < 36


@pytest.mark.asyncio
async def test_structured_aggregation_reports_total_row_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Aggregation adds ``aggregate_total_count`` (sum of group counts) as a scalar the row cap cannot drop."""
    aggs = [
        {"symbol": f"CVE-{i}", "count": 1, "_document_id": None, "_version_id": None}
        for i in range(36)
    ]

    async def _collect(*_a: object, **_k: object) -> tuple[list[object], list[object], bool]:
        return aggs, [], False

    async def _register(*_a: object, **_k: object) -> dict[object, int]:
        return {}

    async def _attrs(*_a: object, **_k: object) -> dict[object, dict[str, object]]:
        return {}

    monkeypatch.setattr(db_query, "_collect_observations", _collect)
    monkeypatch.setattr(db_query, "_register_evidence_for_versions", _register)
    monkeypatch.setattr(db_query, "fetch_attributes", _attrs)
    out = json.loads(
        await db_query._execute_structured_mode(
            None,  # type: ignore[arg-type]
            _args(target="observations", aggregation="count"),
            workspace_id=uuid4(),
            allowed_source_ids=[uuid4()],
            date_from=None,
            date_to=None,
            evidence_list=[],
        )
    )
    assert out["aggregate_total_count"] == 36
