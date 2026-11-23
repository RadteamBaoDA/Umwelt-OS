"""Tier-2 real-model eval of chat answers over collected data (Docker harness, opt-in, spends tokens).

Plan step 7, tier 2. Skipped unless ``INTEGRATION=1``, ``CHAT_REAL_EVAL=1`` and ``.env.e2e-real`` holds a key.
Each question is one whole chat turn driven through the worker's ``_stream_turn`` loop (real tool loop, real
``query_collected_data`` against the seeded matrix database, real prompts/tool specs, real grounding and
citation validation) with a real OpenAI-compatible model (base URL, model, key read from ``.env.e2e-real`` at
runtime only; the key is never printed or written, and error text is masked). The seed is the one from
``test_chat_collected_data_matrix``. The run is graded per case and written to
``test-results/chat-eval/report-<timestamp>.json``; the test asserts only that the run completed.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import re
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from core.workspaces import public as workspaces
from core.workspaces.schemas import WorkspaceContext
from modules.chat._worker import stream, tools
from modules.chat._worker.finalize import _grounding_for, _validate_citations
from modules.chat._worker.setup import _compose_system_text, _gadget_note
from modules.chat._worker.state import _RunCtx, _StreamState
from modules.chat._worker.stream import _query_rounds_used
from modules.chat._worker.tools import _ToolEnv, _TurnState
from modules.chat.db_query import (
    TOOL_QUERY_COLLECTED_DATA,
    active_collected_providers,
    sql_mode_available,
)
from modules.chat.generative_ui import (
    build_chat_tool_specs,
    build_generative_ui_system_prompt,
    list_skills,
)
from modules.chat.schemas import AnswerContext, AnswerContextRequest
from tests.integration.chat_eval.cases import CASES, Case
from tests.integration.chat_eval.grading import (
    RowIds,
    extract_row_ids,
    hidden_miss,
    min_recall_ok,
    recall,
    scope_ids,
    scope_leak,
    summarize_recall,
)
from tests.integration.test_chat_collected_data_matrix import (  # noqa: F401  # fixture + seed helpers
    FLAG,
    TZ,
    Matrix,
    _set,
    matrix,
)

ENV_FILE = Path(__file__).resolve().parents[2] / ".env.e2e-real"
REPORT_DIR = Path(__file__).resolve().parents[2] / "test-results" / "chat-eval"
CONCURRENCY = 3
CASE_TIMEOUT_S = 170.0
DEADLINE_S = 25 * 60.0  # cases not started by then are reported as skipped


def _load_env() -> dict[str, str]:
    """Parse ``.env.e2e-real`` into a dict (empty when absent). Values are never logged."""
    values: dict[str, str] = {}
    if ENV_FILE.exists():
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
    return values


_ENV = _load_env()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("INTEGRATION") != "1" or os.getenv("CHAT_REAL_EVAL") != "1",
        reason="requires INTEGRATION=1 and CHAT_REAL_EVAL=1 (spends real-model tokens)",
    ),
    pytest.mark.skipif(
        not (
            _ENV.get("E2E_REAL_API_KEY")
            and _ENV.get("E2E_REAL_BASE_URL")
            and _ENV.get("E2E_REAL_MODEL")
        ),
        reason="set E2E_REAL_* in .env.e2e-real",
    ),
]


# --------------------------------------------------------------------------- cases

NODATA = re.compile(
    r"no (?:data|record|result|information|matching|observation|such|price|entry|entries|events?)|"
    r"(?:not|n't|cannot|can't|unable|couldn't).{0,40}(?:found|find|available|access|see|retriev|any)|"
    r"doesn't (?:have|contain|include)|does not (?:have|contain|include)|nothing|not (?:yet )?(?:published|reported|collected)|"
    r"don't have|do not have|no .{0,30} (?:data|information)|"
    r"không (?:có|tìm thấy|thấy|truy cập|thể|đủ|chứa)|chưa (?:có|được|công bố)|không có dữ liệu|trống|rỗng",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- number matching
_SCALES = (
    (r"nghìn tỷ|ngàn tỷ|trillion", 1e12),
    (r"tỷ|tỉ|billion", 1e9),
    (r"triệu|million", 1e6),
    (r"nghìn|ngàn|thousand", 1e3),
)
_TOKEN = re.compile(
    r"(?<![\d.,])(\d[\d.,]*\d|\d)(?:\s*(nghìn tỷ|ngàn tỷ|trillion|tỷ|tỉ|billion|triệu|million|nghìn|ngàn|thousand))?",
    re.IGNORECASE,
)


def _candidates(token: str) -> list[float]:
    """Every plausible value of a numeric token under en-US (1,234.5) and vi-VN (1.234,5) formats."""
    out: set[float] = set()
    for thousands, decimal in ((",", "."), (".", ",")):
        cleaned = token.replace(thousands, "")
        if cleaned.count(decimal) <= 1:
            try:
                out.add(float(cleaned.replace(decimal, ".")))
            except ValueError:
                pass
    return sorted(out)


def _answer_numbers(text: str) -> list[list[float]]:
    """Candidate values per numeric token of ``text`` (scale words applied as an extra candidate)."""
    text = re.sub(r"\[\d+\]", " ", text)  # citation markers are not figures
    tokens: list[list[float]] = []
    for match in _TOKEN.finditer(text):
        values = _candidates(match.group(1))
        word = (match.group(2) or "").lower()
        if word:
            scale = next(s for pattern, s in _SCALES if re.fullmatch(pattern, word))
            values = [*values, *(v * scale for v in values)]
        tokens.append(values)
    return tokens


def _close(value: float, expected: float) -> bool:
    """Tolerance for model rounding: 0.06 % relative or 0.011 absolute."""
    return abs(value - expected) <= max(0.011, 0.0006 * abs(expected))


def _has_number(text: str, expected: float) -> bool:
    """True when some numeric token of ``text`` reads as ``expected`` in either locale format."""
    return any(_close(v, expected) for token in _answer_numbers(text) for v in token)


def _assoc_ok(text: str, label: str, expected: float) -> bool:
    """True when ``expected`` occurs within 90 characters after a ``label`` match (or 90 before it)."""
    for found in re.finditer(label, text, re.IGNORECASE):
        window = text[max(0, found.start() - 90) : found.end() + 90]
        if _has_number(window, expected):
            return True
    return False


# --------------------------------------------------------------------------- real model stream
@dataclass
class Recorder:
    """What one case's turn did: model rounds opened, each tool call's arguments/result, and the ids it returned."""

    model_rounds: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    version_ids: set[str] = field(default_factory=set)  # document versions in SQL rows (string ids)
    source_ids: set[str] = field(default_factory=set)  # sources in SQL rows (string ids)


_REC: contextvars.ContextVar[Recorder] = contextvars.ContextVar("rec")


def _mask(text: str) -> str:
    """Replace the API key in ``text`` (error bodies) so it can never reach a report or assertion message."""
    key = _ENV["E2E_REAL_API_KEY"]
    return text.replace(key, "***")


def _open_stream(
    messages: list[dict[str, Any]], tools_: list[dict[str, Any]] | None, tool_choice: str = "auto"
) -> AsyncIterator[str]:
    """Open one streamed chat completion on the real endpoint; yields raw ``data: ...`` SSE lines.

    Same contract as the worker's ``_ModelSetup.open_stream``. Retries HTTP 429/5xx before any line is
    streamed (three attempts, short backoff). Errors are masked.
    """
    _REC.get().model_rounds += 1
    base = _ENV["E2E_REAL_BASE_URL"].rstrip("/")
    body: dict[str, Any] = {"model": _ENV["E2E_REAL_MODEL"], "messages": messages, "stream": True}
    if tools_:
        body["tools"], body["tool_choice"] = tools_, tool_choice
    effort = _ENV.get("E2E_REAL_REASONING_EFFORT", "")
    if effort in {"low", "medium", "high"}:
        body["reasoning_effort"] = effort
    headers = {"Authorization": f"Bearer {_ENV['E2E_REAL_API_KEY']}"}

    async def gen() -> AsyncIterator[str]:
        """Stream the lines of the (retried) request."""
        async with httpx.AsyncClient(timeout=httpx.Timeout(150.0, connect=20.0)) as client:
            for attempt in range(3):
                async with client.stream(
                    "POST", f"{base}/chat/completions", json=body, headers=headers
                ) as resp:
                    if resp.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                        await asyncio.sleep(2.0 * (attempt + 1))
                        continue
                    if resp.status_code >= 400:
                        detail = _mask((await resp.aread()).decode("utf-8", "replace")[:300])
                        raise RuntimeError(f"model HTTP {resp.status_code}: {detail}")
                    async for line in resp.aiter_lines():
                        stripped = line.strip()
                        if stripped.startswith("data:"):
                            yield "data: " + stripped[5:].strip()
                    return

    return gen()


# --------------------------------------------------------------------------- one turn
async def _noop(*_: Any, **__: Any) -> None:
    """Stand-in for the status publisher (a stream-event write)."""


async def _never_cancelled(*_: Any, **__: Any) -> bool:
    """Stand-in for the Redis cancel check."""
    return False


async def _flush(_c: Any, st: _StreamState) -> bool:
    """Stand-in for the fenced flush: moves the buffers into the accumulated text."""
    st.accumulated_text += st.pending
    st.accumulated_reasoning += st.pending_reasoning
    st.drop_pending()
    return True


async def _run_turn(case: Case, m: Matrix, engine: AsyncEngine) -> dict[str, Any]:
    """Run one case through the worker loop and return the raw outcome (answer, grounding, citations, calls)."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    ws_scope = WorkspaceContext(
        user_id=m.user_id, workspace_id=m.workspace_id, role="owner", membership_revision=m.revision
    )
    async with factory() as session:
        fence = await workspaces.read_access_fence(
            session, scope=ws_scope, multi_workspace_enabled=FLAG
        )
        providers = await active_collected_providers(
            session, workspace_id=m.workspace_id, allow_local_only=False
        )
    sql_on = sql_mode_available()
    skills = list_skills()
    locale = "vi-VN" if case.lang == "vi" else "en-US"
    answer_context = AnswerContext(query=case.q, evidence=[])
    # Scoped cases pin the tool to their sources; the tool reads the allow-list from req_params.
    scope = scope_ids(case, m.sources)
    req_params: dict[str, Any] = {"timezone": TZ}
    if scope is not None:
        req_params["source_scope"] = scope
    turn_inputs = SimpleNamespace(
        day_scope=None, req_params=req_params, web_run=None, task_brief=None
    )
    system_text = _compose_system_text(
        turn_inputs,  # type: ignore[arg-type]
        answer_context,
        SimpleNamespace(system_prompts={}),  # type: ignore[arg-type]
        (),
        TZ,
    )
    ui_system = (
        system_text
        + _gadget_note(req_params, True, providers, sql_on)
        + "\n\n"
        + build_generative_ui_system_prompt(
            locale=locale,
            local_only=False,
            skills=skills,
            remote_media=False,
            todos=True,
            templates=True,
            theme=True,
            rich_answer=False,
        )
    )
    ui_tools = build_chat_tool_specs(
        include_read_skill=bool(skills),
        include_trip_images=False,
        include_todos=True,
        include_user_input=True,
        include_templates=True,
        include_theme=True,
        include_db_query=True,
        include_db_sql=sql_on,
        include_answer=False,
    )
    messages: list[dict[str, Any]] = [{"role": "system", "content": ui_system}]
    for user_text, assistant_text in case.history:
        messages += [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": assistant_text},
        ]
    messages.append({"role": "user", "content": case.q})
    ctx = _RunCtx(
        response_id=uuid4(),
        session_factory=factory,
        settings=SimpleNamespace(multi_workspace_enabled=FLAG),  # type: ignore[arg-type]
        redis=None,  # type: ignore[arg-type]
        conversation_id=uuid4(),
        privacy_fence=None,
        seq=0,
        job_scope=ws_scope,  # type: ignore[arg-type]
        access_fence=fence,
        answer_context=answer_context,
    )
    setup = SimpleNamespace(
        ai_config=SimpleNamespace(gateway_kind="openai_compatible"),
        db_query_on=True,
        trip_images_on=False,
        local_only_turn=False,  # a remote model: local_only sources stay hidden
        owner_timezone=TZ,
        ui_tools=ui_tools,
        open_stream=_open_stream,
    )
    turn = SimpleNamespace(
        user_prompt=case.q,
        req_params=req_params,
        answer_context=answer_context,
        answer_request=(
            AnswerContextRequest(
                query=case.q, source_scope=[UUID(s) for s in scope], source_scope_enforced=True
            )
            if scope is not None
            else AnswerContextRequest(query=case.q)
        ),
        web_run=None,
        prior_messages=[{"role": "assistant", "content": a} for _u, a in case.history],
    )
    ts = _TurnState(
        messages_payload=messages,
        stream_iter=_open_stream(messages, ui_tools),
        tools_active=True,
        max_parts=10,
        max_ui_rounds=10,
        input_ttl_seconds=60,
        last_cancel_check=0.0,
    )
    env = _ToolEnv(c=ctx, claimed=None, turn=turn, setup=setup, ts=ts)  # type: ignore[arg-type]
    st = _StreamState()
    await stream._stream_turn(env, st)
    answer = st.answer_text()
    cited = _validate_citations(answer, answer_context, turn.answer_request, None, ts.ui_parts)
    report, _ = _grounding_for(env, cited.final_answer, cited.valid_citations, [])
    return {
        "answer": answer,
        "final_answer": cited.final_answer,
        # Structured observations/documents carry no ids, so the turn's evidence list (every version the
        # tools returned with a chunk) is the retrieval source for both modes.
        "retrieved_versions": {str(e.document_version_id) for e in answer_context.evidence},
        "retrieved_sources": {str(e.source_id) for e in answer_context.evidence},
        "cited_versions": {
            str(c["documentVersionId"])
            for c in cited.valid_citations
            if c.get("documentVersionId") is not None
        },
        "citations": len(cited.valid_citations),
        "markers": sorted({int(n) for n in re.findall(r"\[(\d+)\]", answer)}),
        "kept_markers": sorted(cited.number_map),
        "unverified": list(report.unverified) if report is not None else [],
        "partially_uncited": bool(report and report.partially_uncited),
        "grounding_ran": report is not None,
        "query_rounds": _query_rounds_used(ts),
        "widgets": len(ts.ui_parts),
    }


def _grade(
    case: Case,
    out: dict[str, Any],
    rec: Recorder,
    key_map: Mapping[str, str] | None = None,
    scope: list[str] | None = None,
) -> dict[str, Any]:
    """Apply every criterion; ``None`` means not applicable. Pure.

    ``key_map`` resolves case keys (``gold_keys``, ``forbid_keys``) to document version id strings. It is empty
    until the matrix seed publishes its ids, so gold and forbidden sets are empty and recall is None.
    ``scope`` is the case's source-id allow-list from ``scope_ids`` (None when unscoped); any retrieved source
    outside it is a scope leak. Retrieval is the union of SQL-row ids and the turn's evidence; answer recall
    uses the kept citations.
    """
    answer: str = out["final_answer"]
    keys = key_map or {}
    gold = {keys[k] for k in case.gold_keys if k in keys}
    forbidden = {keys[k] for k in case.forbid_keys if k in keys}
    retrieved_versions = rec.version_ids | out["retrieved_versions"]
    retrieved_sources = rec.source_ids | out["retrieved_sources"]
    called = [c for c in rec.tool_calls if c.get("name") == TOOL_QUERY_COLLECTED_DATA]
    got_rows = any(c.get("rows") for c in called)
    nums_ok: bool | None = None
    if case.nums:
        found = all(_has_number(answer, n) for n in case.nums) and all(
            _assoc_ok(answer, label, n) for label, n in case.assoc
        )
        nums_ok = found or (case.nums_or_nodata and bool(NODATA.search(answer)))
    texts_ok: bool | None = None
    if case.texts:
        texts_ok = all(re.search(t, answer, re.IGNORECASE) for t in case.texts)
    forbidden_ok = not any(_has_number(answer, n) for n in case.forbid_nums) and not any(
        re.search(t, answer, re.IGNORECASE) for t in case.forbid_texts
    )
    invalid_markers = [n for n in out["markers"] if n not in out["kept_markers"]]
    cite_ok: bool | None = None
    if got_rows and not case.empty:
        cite_ok = out["citations"] >= 1 and not invalid_markers
    empty_ok: bool | None = None
    if case.empty:
        empty_ok = bool(NODATA.search(answer)) and not out["unverified"]
    criteria: dict[str, Any] = {
        "tool_called": bool(called),
        "numbers_correct": nums_ok,
        "texts_ok": texts_ok,
        "no_forbidden": forbidden_ok,
        "grounded": not out["unverified"],
        "citations_valid": cite_ok,
        "empty_ok": empty_ok,
    }
    # hidden_miss looks only at the criteria above: a recall leg is a diagnosis, not a pass/fail input.
    retrieval_recall = recall(gold, retrieved_versions)
    answer_recall = recall(gold, out["cited_versions"])
    # A pass/fail floor on retrieval; not in _DIAGNOSTIC, so a False fails the case and suppresses hidden_miss.
    criteria["min_recall_ok"] = min_recall_ok(case.min_recall, retrieval_recall)
    criteria["hidden_miss"] = hidden_miss(criteria, retrieval_recall, answer_recall)
    criteria["retrieval_recall"] = retrieval_recall
    criteria["answer_recall"] = answer_recall
    allowed = set(scope) if scope is not None else None
    criteria["scope_leak"] = (
        scope_leak(retrieved_sources, allowed, forbidden, retrieved_versions)
        if case.forbid_keys or allowed is not None
        else None
    )
    return criteria


_DIAGNOSTIC = frozenset({"retrieval_recall", "answer_recall", "hidden_miss", "scope_leak"})


def _passed(criteria: Mapping[str, Any]) -> bool:
    """True when no pass/fail criterion is False and no scope leak was recorded.

    Recall legs and ``hidden_miss`` are diagnostics and never fail a case on their own; a ``False``
    ``scope_leak`` means no leak, so it is excluded from the all() check and only a ``True`` fails it.
    """
    return all(v is not False for k, v in criteria.items() if k not in _DIAGNOSTIC) and not (
        criteria.get("scope_leak")
    )


# --------------------------------------------------------------------------- the run
async def test_real_model_eval(
    matrix: Matrix,  # noqa: F811  # imported fixture
    committed_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run every case against the real model, write the report, assert only that the run completed."""
    # Paused sources stay readable by design; archive the paused distractor so "latest" has one right answer.
    await _set(committed_engine, matrix, "bn_paused", "status = 'archived'")
    real_tool = tools.execute_db_query_tool  # type: ignore[attr-defined]

    async def recording_tool(session: Any, raw: dict[str, Any], **kwargs: Any) -> str:
        """Run the real query tool; record its arguments, whether it returned rows and the ids it carried."""
        result = await real_tool(session, raw, **kwargs)
        found = RowIds(set(), set())
        try:
            data = json.loads(result)
            rows = any(
                data.get(k) for k in ("observations", "documents", "rows", "observation_aggregates")
            )
            error = data.get("error")
            status = data.get("status")
            found = extract_row_ids(data)
        except json.JSONDecodeError:
            rows, error, status = False, "unparseable", None
        rec = _REC.get()
        rec.version_ids |= found.version_ids
        rec.source_ids |= found.source_ids
        rec.tool_calls.append(
            {
                "name": TOOL_QUERY_COLLECTED_DATA,
                "args": raw,
                "rows": rows,
                "error": error,
                "status": status,
            }
        )
        return result

    monkeypatch.setattr(tools, "execute_db_query_tool", recording_tool)
    for module in (tools, stream):
        monkeypatch.setattr(module, "_publish_status", _noop)
    monkeypatch.setattr(stream, "is_run_cancelled", _never_cancelled)
    monkeypatch.setattr(stream, "_flush_pending", _flush)

    started = time.monotonic()
    gate = asyncio.Semaphore(CONCURRENCY)
    results: dict[str, dict[str, Any]] = {}

    async def one(case: Case) -> None:
        """Run and grade one case under the concurrency gate, deadline and per-case timeout."""
        async with gate:
            if time.monotonic() - started > DEADLINE_S:
                results[case.id] = {"id": case.id, "status": "skipped_deadline"}
                return
            rec = Recorder()
            _REC.set(rec)
            entry: dict[str, Any] = {
                "id": case.id,
                "category": case.cat,
                "lang": case.lang,
                "question": case.q,
            }
            t0 = time.monotonic()
            try:
                out = await asyncio.wait_for(
                    _run_turn(case, matrix, committed_engine), CASE_TIMEOUT_S
                )
                entry |= {
                    "status": "completed",
                    "criteria": _grade(case, out, rec, scope=scope_ids(case, matrix.sources)),
                    "answer": out["answer"],
                    "tool_args": [c["args"] for c in rec.tool_calls],
                    "tool_results": [
                        {k: c[k] for k in ("rows", "error", "status")} for c in rec.tool_calls
                    ],
                    "unverified_numbers": out["unverified"],
                    "partially_uncited": out["partially_uncited"],
                    "citations": out["citations"],
                    "query_rounds": out["query_rounds"],
                    "model_rounds": rec.model_rounds,
                    "widgets": out["widgets"],
                }
            except Exception as exc:  # noqa: BLE001  # a failing case is reported, never raised
                entry |= {"status": "error", "error": _mask(f"{type(exc).__name__}: {exc}")[:400]}
            entry["seconds"] = round(time.monotonic() - t0, 1)
            results[case.id] = entry

    await asyncio.gather(*(one(c) for c in CASES))
    ordered = [results[c.id] for c in CASES]
    report = {
        "model": _ENV["E2E_REAL_MODEL"],
        "generated_at": datetime.now(UTC).isoformat(),
        "clock_utc": datetime.now(UTC).isoformat(),
        "summary": _summary(ordered),
        "cases": ordered,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"report-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(
        f"\nCHAT EVAL REPORT {path}\nCHAT EVAL SUMMARY {json.dumps(report['summary'], ensure_ascii=False)}"
    )
    assert len(ordered) == len(CASES)  # the run completed; scores live in the report


def _pct(values: list[bool]) -> float | None:
    """Percentage of true values (None when the list is empty)."""
    return round(100 * sum(values) / len(values), 1) if values else None


def _summary(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate scores over the completed cases (pure)."""
    done = [e for e in entries if e.get("status") == "completed"]
    crit: Callable[[str], list[bool]] = lambda k: [  # noqa: E731
        bool(e["criteria"][k]) for e in done if e["criteria"][k] is not None
    ]
    by_cat: dict[str, dict[str, int]] = {}
    for e in done:
        ok = _passed(e["criteria"])
        slot = by_cat.setdefault(e["category"], {"pass": 0, "total": 0})
        slot["total"] += 1
        slot["pass"] += int(ok)
    empties = crit("empty_ok")
    return {
        "cases": len(entries),
        "completed": len(done),
        "errors": [e["id"] for e in entries if e.get("status") == "error"],
        "skipped": [e["id"] for e in entries if e.get("status") == "skipped_deadline"],
        "tool_called_pct": _pct(crit("tool_called")),
        "numbers_correct_pct": _pct(crit("numbers_correct")),
        "texts_ok_pct": _pct(crit("texts_ok")),
        "no_forbidden_pct": _pct(crit("no_forbidden")),
        "grounded_pct": _pct(crit("grounded")),
        "fabricated_numbers_total": sum(len(e["unverified_numbers"]) for e in done),
        "citation_validity_pct": _pct(crit("citations_valid")),
        "empty_case_correct": f"{sum(empties)}/{len(empties)}",
        "mean_query_rounds": round(sum(e["query_rounds"] for e in done) / max(len(done), 1), 2),
        "mean_model_rounds": round(sum(e["model_rounds"] for e in done) / max(len(done), 1), 2),
        "fully_passed": sum(_passed(e["criteria"]) for e in done),
        "by_category": by_cat,
        **summarize_recall(done),
    }
