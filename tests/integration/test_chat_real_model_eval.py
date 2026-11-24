"""Tier-2 real-model eval of chat answers over collected data (Docker harness, opt-in, spends tokens).

Plan step 7, tier 2. Skipped unless ``INTEGRATION=1``, ``CHAT_REAL_EVAL=1`` and ``.env.e2e-real`` holds a key.
Each question is one whole chat turn driven through the worker's ``_stream_turn`` loop (real tool loop, real
``query_collected_data`` against the seeded matrix database, real prompts/tool specs, real grounding and
citation validation) with a real OpenAI-compatible model (base URL, model, key read from ``.env.e2e-real`` at
runtime only; the key is never printed or written, and error text is masked). The seed is the one from
``test_chat_collected_data_matrix``. The run is graded per case and written to
``test-results/chat-eval/report-<timestamp>.json``; the test asserts only that the run completed.

Seed-only verification: ``CHAT_EVAL_ONLY=__seed_only__`` seeds the matrix, every ``seed_all`` provider and the
planted gold, then writes ``test-results/chat-eval/report-<timestamp>.json`` with ``mode: "seed_only"``, the
``not_seeded`` rows (including providers whose normalization failed) and ``key_map_size``. No case runs and no
model is called. Any other ``CHAT_EVAL_ONLY`` value still selects case ids.

Report shape: a header (``model``, ``legs_run``, ``rerank``, ``rerank_rpm``, ``judge``, ``sql_mode``, ``deadline_s``,
``not_seeded``, ``baseline``) then ``summary`` keyed by leg (``tool`` / ``hybrid``), each computed from that leg's
own entries. ``CHAT_EVAL_DEADLINE_MIN`` (default 25) sets the run deadline in minutes; ``CHAT_EVAL_BASELINE`` names
the tool baseline report that ``baseline_regressions`` compares against (default: the 2026-11-23 tool baseline).
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import re
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from core.config import Settings
from core.workspaces import public as workspaces
from core.workspaces.schemas import WorkspaceContext
from modules.chat._worker import stream, tools
from modules.chat._worker.finalize import _grounding_for, _validate_citations
from modules.chat._worker.setup import _compose_system_text
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
from modules.chat.schemas import AnswerContext
from tests.integration.chat_eval.cases import ALL_CASES, Case
from tests.integration.chat_eval.gadget_note import gadget_note_for_turn
from tests.integration.chat_eval.grading import (
    RowIds,
    cited_version_ids,
    extract_row_ids,
    forbidden_ok,
    has_number,
    hidden_miss,
    min_recall_ok,
    missed_keys,
    recall,
    scope_ids,
    scope_leak,
    tool_used,
)
from tests.integration.chat_eval.hybrid import (
    HybridEnv,
    RedactedEnv,
    configure_ai,
    enable_search,
    hybrid_context,
    hybrid_env,
    index_all,
    local_only_blocked,
    mask_secrets,
    retrieval_request,
)
from tests.integration.chat_eval.judge import (
    evidence_from_result,
    judge,
    judge_config,
    load_env_file,
    to_record,
)
from tests.integration.chat_eval.pacing import StartPacer, rerank_rpm
from tests.integration.chat_eval.planted import PLANTED_PATH, load_planted, seed_planted
from tests.integration.chat_eval.report import build_summaries
from tests.integration.chat_eval.seed_all import seed_all
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
DEFAULT_DEADLINE_MIN = (
    25.0  # a full 101-case x 2-leg run needs about 75 minutes; set CHAT_EVAL_DEADLINE_MIN
)
BASELINE_DEFAULT = REPORT_DIR / "baseline-20261123T105143Z.json"  # the tool-leg baseline (45 cases)
# Local-only guard outcomes that count as a pass: withholding local data is the expected behaviour there.
LOCAL_ONLY_PASS_CATS = frozenset({"local_only", "scope_local_only"})
SEED_ONLY_MODE = (
    "__seed_only__"  # CHAT_EVAL_ONLY value: seed everything, write the report, run no case
)


def _load_env() -> dict[str, str]:
    """Parse ``.env.e2e-real`` into a dict (empty when absent). Values are never logged."""
    return RedactedEnv(load_env_file(ENV_FILE))


def _deadline_s() -> float:
    """Run deadline in seconds from ``CHAT_EVAL_DEADLINE_MIN`` (minutes; blank or unset means the default).

    Cases not started by the deadline are reported as ``skipped_deadline``. Read at call time so the header
    and the run agree on the same value.
    """
    return float(os.getenv("CHAT_EVAL_DEADLINE_MIN") or DEFAULT_DEADLINE_MIN) * 60.0


def _load_baseline() -> tuple[str | None, list[dict[str, Any]] | None]:
    """Path and cases of the tool baseline report, or ``(None, None)`` when the file is absent.

    ``CHAT_EVAL_BASELINE`` names the file; blank falls back to ``BASELINE_DEFAULT``. A missing file is reported
    as no baseline instead of failing the run, so ``baseline_regressions`` is then empty.
    """
    path = Path(os.getenv("CHAT_EVAL_BASELINE") or BASELINE_DEFAULT)
    if not path.is_file():
        return None, None
    data = json.loads(path.read_text(encoding="utf-8"))
    return str(path), list(data.get("cases", []))


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


def _assoc_ok(text: str, label: str, expected: float) -> bool:
    """True when ``expected`` occurs within 90 characters after a ``label`` match (or 90 before it)."""
    for found in re.finditer(label, text, re.IGNORECASE):
        window = text[max(0, found.start() - 90) : found.end() + 90]
        if has_number(window, expected):
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
    # Tool-result row excerpts the judge may see (bounded by evidence_block).
    evidence_texts: list[str] = field(default_factory=list)


_REC: contextvars.ContextVar[Recorder] = contextvars.ContextVar("rec")


def _mask(text: str) -> str:
    """Replace every ``E2E_*API_KEY`` value in ``text`` (error bodies) so no key reaches a report or assertion."""
    return mask_secrets(text, _ENV)


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


async def _run_turn(
    case: Case, m: Matrix, engine: AsyncEngine, *, hybrid: tuple[Settings, Redis] | None = None
) -> dict[str, Any]:
    """Run one case through the worker loop and return the raw outcome (answer, grounding, citations, calls).

    ``hybrid`` (real Settings and Redis) switches to the hybrid leg: the evidence comes from ``hybrid_context``
    (rewrite, ``build_context``) instead of none. A turn with ``local_only`` evidence is not sent to the model and
    returns ``{"blocked": True, ...}``.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    ws_scope = WorkspaceContext(
        user_id=m.user_id, workspace_id=m.workspace_id, role="owner", membership_revision=m.revision
    )
    async with factory() as session:
        fence = await workspaces.read_access_fence(
            session, scope=ws_scope, multi_workspace_enabled=FLAG
        )
        # ``(ids, overflow)``: the overflow count is passed on so the prompt hint matches production.
        providers_lookup = await active_collected_providers(
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
    if hybrid is not None:
        answer_context = await hybrid_context(
            case, retrieval_request(case, scope), ws_scope, fence, factory, *hybrid
        )
        if local_only_blocked(answer_context):
            return {"blocked": True, "rerank_status": answer_context.rerank_status}
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
        + gadget_note_for_turn(req_params, providers_lookup, sql_on)
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
        task_draft_on=False,  # draft_scheduled_task is not offered in the eval
        web_images=(),
    )
    turn = SimpleNamespace(
        user_prompt=case.q,
        req_params=req_params,
        answer_context=answer_context,
        answer_request=retrieval_request(case, scope),
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
        # Validated citations are alias dumps (snake_case ``document_version_id``); see cited_version_ids.
        "cited_versions": cited_version_ids(cited.valid_citations),
        "citations": len(cited.valid_citations),
        "rerank_status": answer_context.rerank_status,
        "context_warnings": list(answer_context.warnings),
        "markers": sorted({int(n) for n in re.findall(r"\[(\d+)\]", answer)}),
        "kept_markers": sorted(cited.number_map),
        "unverified": list(report.unverified) if report is not None else [],
        "partially_uncited": bool(report and report.partially_uncited),
        "grounding_ran": report is not None,
        "query_rounds": _query_rounds_used(ts),
        "widgets": len(ts.ui_parts),
        # What the turn had, for the judge: retrieved chunk content first, then the query-tool row excerpts.
        "evidence_texts": [
            *(item.content for item in answer_context.evidence),
            *_REC.get().evidence_texts,
        ],
    }


def _grade(
    case: Case,
    out: dict[str, Any],
    rec: Recorder,
    leg: str,
    key_map: Mapping[str, str] | None = None,
    scope: list[str] | None = None,
) -> dict[str, Any]:
    """Apply every criterion; ``None`` means not applicable. Pure.

    ``leg`` is the retrieval leg (``tool`` or ``hybrid``). ``tool_called`` uses ``tool_used``: on the hybrid leg
    the build_context evidence counts as using collected data, since that leg has no query tool.

    ``key_map`` resolves case keys (``gold_keys``, ``forbid_keys``) to document version id strings (the
    ``EvalSeed.key_map`` from ``eval_seed``). A key missing from the map is ignored, so the gold and
    forbidden sets shrink to the resolvable keys and recall is None when none resolve.
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
        found = all(has_number(answer, n) for n in case.nums) and all(
            _assoc_ok(answer, label, n) for label, n in case.assoc
        )
        nums_ok = found or (case.nums_or_nodata and bool(NODATA.search(answer)))
    texts_ok: bool | None = None
    if case.texts:
        texts_ok = all(re.search(t, answer, re.IGNORECASE) for t in case.texts)
    invalid_markers = [n for n in out["markers"] if n not in out["kept_markers"]]
    cite_ok: bool | None = None
    if got_rows and not case.empty:
        cite_ok = out["citations"] >= 1 and not invalid_markers
    empty_ok: bool | None = None
    if case.empty:
        empty_ok = bool(NODATA.search(answer)) and not out["unverified"]
    criteria: dict[str, Any] = {
        "tool_called": tool_used(
            leg, bool(called), out["retrieved_versions"], expects_no_tool=case.expects_no_tool
        ),
        "numbers_correct": nums_ok,
        "texts_ok": texts_ok,
        "no_forbidden": forbidden_ok(answer, case),
        "grounded": not out["unverified"],
        "citations_valid": cite_ok,
        "empty_ok": empty_ok,
    }
    # A pass/fail floor on retrieval; not in _DIAGNOSTIC, so a False fails the case and suppresses hidden_miss.
    retrieval_recall = recall(gold, retrieved_versions)
    answer_recall = recall(gold, out["cited_versions"])
    criteria["min_recall_ok"] = min_recall_ok(case.min_recall, retrieval_recall)
    allowed = set(scope) if scope is not None else None
    # Computed before hidden_miss: a scope leak must suppress hidden_miss, which reads it from criteria.
    criteria["scope_leak"] = (
        scope_leak(retrieved_sources, allowed, forbidden, retrieved_versions)
        if case.forbid_keys or allowed is not None
        else None
    )
    # hidden_miss looks only at the criteria above: a recall leg is a diagnosis, not a pass/fail input.
    criteria["hidden_miss"] = hidden_miss(criteria, retrieval_recall, answer_recall)
    criteria["retrieval_recall"] = retrieval_recall
    criteria["answer_recall"] = answer_recall
    return criteria


# --------------------------------------------------------------------------- seeding
@dataclass(frozen=True)
class EvalSeed:
    """Everything the eval run needs from the seed: the matrix (with fixture and planted sources), the
    planted key -> document_version_id map, and the providers that were not seeded (with reasons)."""

    matrix: Matrix
    key_map: dict[str, str]
    not_seeded: list[dict[str, str]]


_EVAL_SEED: EvalSeed | None = None  # per-session cache: committed rows persist between tests


@pytest.fixture
async def eval_seed(
    matrix: Matrix,  # noqa: F811  # imported fixture
    ready_owner_client: httpx.AsyncClient,
    committed_engine: AsyncEngine,
) -> EvalSeed:
    """Seed every fixture provider and the planted gold once per session, after the matrix seed."""
    global _EVAL_SEED
    if _EVAL_SEED is None:
        not_seeded = await seed_all(ready_owner_client, committed_engine, matrix)
        key_map = await seed_planted(
            ready_owner_client, committed_engine, matrix, load_planted(PLANTED_PATH)
        )
        _EVAL_SEED = EvalSeed(matrix, key_map, not_seeded)
    return _EVAL_SEED


# --------------------------------------------------------------------------- the run
async def test_real_model_eval(
    matrix: Matrix,  # noqa: F811  # imported fixture
    eval_seed: EvalSeed,
    committed_engine: AsyncEngine,
    ready_owner_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run every selected case on each selected leg against the real model, write the report, assert only completion.

    With ``CHAT_EVAL_ONLY=__seed_only__`` the seed fixture has already run; the report records what seeded
    (``not_seeded`` and the planted key count) and the test returns before any case, grader or model call.
    """
    if os.getenv("CHAT_EVAL_ONLY", "").strip() == SEED_ONLY_MODE:
        seed_report: dict[str, Any] = {
            "mode": "seed_only",
            "generated_at": datetime.now(UTC).isoformat(),
            "not_seeded": eval_seed.not_seeded,
            "key_map_size": len(eval_seed.key_map),
        }
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        seed_path = REPORT_DIR / f"report-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
        seed_path.write_text(
            json.dumps(seed_report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print(f"\nCHAT EVAL SEED REPORT {seed_path}")
        return
    # Paused sources stay readable by design; archive the paused distractor so "latest" has one right answer.
    await _set(committed_engine, matrix, "bn_paused", "status = 'archived'")
    real_tool = tools.execute_db_query_tool  # type: ignore[attr-defined]

    async def recording_tool(session: Any, raw: dict[str, Any], **kwargs: Any) -> str:
        """Run the real query tool; record its arguments, whether it returned rows and the ids it carried."""
        result = await real_tool(session, raw, **kwargs)
        found = RowIds(set(), set())
        texts = evidence_from_result(result)
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
        rec.evidence_texts.extend(texts)
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

    hybrid = hybrid_env(_ENV)
    legs = _legs(hybrid is not None)
    if not legs:
        pytest.skip("CHAT_EVAL_LEGS selects only hybrid, but E2E_REAL_EMBED_* is not set")
    # Raises before indexing or any model call when the value is malformed.
    rpm = rerank_rpm(os.environ)
    pacer = StartPacer(rpm) if rpm is not None else None
    cases = _selected_cases()
    runtime = (
        await _prepare_hybrid(ready_owner_client, committed_engine, hybrid)
        if hybrid is not None and "hybrid" in legs
        else None
    )
    baseline_path, baseline_cases = _load_baseline()
    try:
        ordered = await _run_all(cases, legs, matrix, eval_seed, committed_engine, runtime, pacer)
    finally:
        if runtime is not None:
            await runtime.redis.aclose()
    report: dict[str, Any] = {
        "model": _ENV["E2E_REAL_MODEL"],
        "generated_at": datetime.now(UTC).isoformat(),
        "legs_run": list(legs),
        "rerank": runtime.rerank if runtime is not None else "off",
        "rerank_rpm": rpm,  # None means unpaced
        "judge": "on" if judge_config(_ENV) is not None else "off",
        "sql_mode": "on" if sql_mode_available() else "off",
        "deadline_s": _deadline_s(),
        "not_seeded": eval_seed.not_seeded,
        "baseline": baseline_path,
        "summary": build_summaries(ordered, legs, baseline_cases=baseline_cases),
        "cases": ordered,
    }
    if runtime is not None:
        report["indexed_chunks"] = runtime.indexed
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"report-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(
        f"\nCHAT EVAL REPORT {path}\nCHAT EVAL SUMMARY {json.dumps(report['summary'], ensure_ascii=False)}"
    )
    assert len(ordered) == len(cases) * len(legs)  # the run completed; scores live in the report


def _legs(hybrid_on: bool) -> tuple[str, ...]:
    """Legs to run: ``tool`` always, ``hybrid`` only when its env is set. ``CHAT_EVAL_LEGS`` (comma list) narrows it."""
    # A blank CHAT_EVAL_LEGS (an env file that sets it empty) means the default, not "no legs".
    wanted = {leg.strip() for leg in (os.getenv("CHAT_EVAL_LEGS") or "tool,hybrid").split(",")}
    return tuple(
        leg for leg in ("tool", "hybrid") if leg in wanted and (leg == "tool" or hybrid_on)
    )


def _selected_cases() -> tuple[Case, ...]:
    """``ALL_CASES``, or only the ids in ``CHAT_EVAL_ONLY`` (comma list) for a targeted run such as an owner smoke."""
    only = {cid.strip() for cid in os.getenv("CHAT_EVAL_ONLY", "").split(",") if cid.strip()}
    return tuple(case for case in ALL_CASES if not only or case.id in only)


@dataclass(frozen=True)
class HybridRun:
    """Real Settings and Redis for the hybrid leg, what indexing did, and the rerank status for the report header."""

    settings: Settings
    redis: Redis
    indexed: int
    rerank: str


_COMPOSE_TEST_AI_CREDENTIAL_KEY = (
    "ZmFrZS1tb2RlbC10ZXN0LWtleS0wMDAwMDAwMDAwMDA="  # docker-compose.test.yml default
)
# Endpoint identity inputs the compose worker uses (docker-compose.test.yml, AI_ALLOWED_ENDPOINT_HOSTS and
# AI_ALLOWED_ENDPOINT_CIDRS). The embedding model_identity hashes them, so host Settings must mirror them or the
# host and the worker fail each other's index generation. Must mirror docker-compose.test.yml.
_COMPOSE_AI_ALLOWED_ENDPOINT_HOSTS = frozenset({"fake-model:8000", "searxng:8080"})
_COMPOSE_AI_ALLOWED_ENDPOINT_CIDRS = ["172.16.0.0/12", "192.168.0.0/16", "10.0.0.0/8"]


async def _prepare_hybrid(
    client: httpx.AsyncClient, engine: AsyncEngine, h: HybridEnv
) -> HybridRun:
    """Store the AI settings, enable search, index the seeded chunks and open real Settings and Redis.

    The caller closes ``redis``. Raises (after closing it) when a provider is refused or indexing does not drain.
    """
    # Host pytest must decrypt the credentials the compose API saved, so it uses the stack's (public, fake)
    # test key from docker-compose.test.yml instead of the developer .env key. The endpoint allowlists and the
    # public-provider switch mirror the worker's env too: a developer .env would otherwise change the embedding
    # model_identity, and each side would then fail the other's generation (failed/model_unavailable).
    # AI_ALLOW_PUBLIC_PROVIDERS in the compose file is ${E2E_ALLOW_PUBLIC_PROVIDERS:-false}.
    public_providers = os.environ.get("E2E_ALLOW_PUBLIC_PROVIDERS", "false").lower() == "true"
    settings = Settings(
        ai_credential_encryption_key=os.environ.get(
            "TEST_AI_CREDENTIAL_ENCRYPTION_KEY", _COMPOSE_TEST_AI_CREDENTIAL_KEY
        ),
        ai_allowed_endpoint_hosts=set(_COMPOSE_AI_ALLOWED_ENDPOINT_HOSTS),
        ai_allowed_endpoint_cidrs=list(_COMPOSE_AI_ALLOWED_ENDPOINT_CIDRS),
        ai_allow_public_providers=public_providers,
    )
    redis = Redis.from_url(settings.redis_url)
    try:
        await configure_ai(client, h)
        await enable_search(client)
        indexed = await index_all(engine, settings, redis, mask=lambda t: mask_secrets(t, _ENV))
    except BaseException:
        await redis.aclose()
        raise
    return HybridRun(settings, redis, indexed, h.rerank_label)


async def _run_all(
    cases: tuple[Case, ...],
    legs: tuple[str, ...],
    matrix: Matrix,  # noqa: F811  # imported fixture
    eval_seed: EvalSeed,
    engine: AsyncEngine,
    runtime: HybridRun | None,
    pacer: StartPacer | None = None,
) -> list[dict[str, Any]]:
    """Run every (leg, case) under the concurrency gate, the deadline and the per-case timeout; entries in order.

    ``pacer`` (from ``CHAT_EVAL_RERANK_RPM``) spaces the starts of hybrid cases, each of which may call the rerank
    model once. It waits before the per-case timeout starts, so a paced wait never eats into a case's budget.
    """
    started = time.monotonic()
    deadline_s = _deadline_s()
    gate = asyncio.Semaphore(CONCURRENCY)
    results: dict[tuple[str, str], dict[str, Any]] = {}

    async def one(case: Case, leg: str) -> None:
        """Run and grade one case on one leg under the gate, deadline and per-case timeout.

        A ``needs_sql`` case is reported ``skipped_no_sql`` without a model call when the SQL reader
        DSN is not configured, so it never grades as a failure of the query path it cannot reach.
        """
        key = (case.id, leg)
        if case.needs_sql and not sql_mode_available():
            results[key] = {"id": case.id, "leg": leg, "status": "skipped_no_sql"}
            return
        async with gate:
            if time.monotonic() - started > deadline_s:
                results[key] = {"id": case.id, "leg": leg, "status": "skipped_deadline"}
                return
            rec = Recorder()
            _REC.set(rec)
            entry: dict[str, Any] = {
                "id": case.id,
                "leg": leg,
                "category": case.cat,
                "lang": case.lang,
                "question": case.q,
            }
            t0 = time.monotonic()
            hybrid_arg = (runtime.settings, runtime.redis) if runtime and leg == "hybrid" else None
            if pacer is not None and leg == "hybrid":
                await pacer.acquire()
            try:
                out = await asyncio.wait_for(
                    _run_turn(case, matrix, engine, hybrid=hybrid_arg), CASE_TIMEOUT_S
                )
                entry |= _outcome_entry(case, leg, out, rec, matrix, eval_seed)
                if entry["status"] == "completed":
                    await _add_judge(case, entry, out["evidence_texts"])
            except Exception as exc:  # noqa: BLE001  # a failing case is reported, never raised
                entry |= {"status": "error", "error": _mask(f"{type(exc).__name__}: {exc}")[:400]}
            entry["seconds"] = round(time.monotonic() - t0, 1)
            results[key] = entry

    await asyncio.gather(*(one(c, leg) for leg in legs for c in cases))
    return [results[(c.id, leg)] for leg in legs for c in cases]


async def _add_judge(case: Case, entry: dict[str, Any], evidence: list[str]) -> None:
    """Attach the report-only JEV verdict to a completed entry as ``entry["judge"]``.

    ``evidence`` is the turn's retrieved text, shown to the judge as bounded excerpts. Does nothing when the
    judge env is incomplete. Never raises: a judge failure is stored as an ``invalid`` verdict with a masked
    error, so it cannot fail the case or the run.
    """
    try:
        verdict = await judge(case, entry["answer"], _ENV, evidence)
    except Exception as exc:  # noqa: BLE001  # report-only grader: a judge bug must not fail the case
        error = _mask(f"{type(exc).__name__}: {exc}")[:300]
        entry["judge"] = {"label": "", "probs": {}, "status": "invalid", "error": error}
        return
    if verdict is not None:
        entry["judge"] = to_record(verdict)


def _outcome_entry(
    case: Case,
    leg: str,
    out: dict[str, Any],
    rec: Recorder,
    matrix: Matrix,  # noqa: F811  # imported fixture
    eval_seed: EvalSeed,
) -> dict[str, Any]:
    """Report fields of one finished turn: a graded ``completed`` entry, or ``local_only_blocked`` (not sent).

    A blocked turn passes in the ``local_only`` and ``scope_local_only`` categories, where withholding local data is
    the expected behaviour. A completed entry carries ``missed_keys``: the gold keys retrieval never surfaced, the
    input to the report's ``miss_list``. ``rerank_status`` is recorded for the hybrid leg only.
    """
    if out.get("blocked"):
        return {
            "status": "local_only_blocked",
            "rerank_status": out["rerank_status"],
            "passed": case.cat in LOCAL_ONLY_PASS_CATS,
        }
    fields: dict[str, Any] = {
        "status": "completed",
        "criteria": _grade(
            case,
            out,
            rec,
            leg,
            key_map=eval_seed.key_map,
            scope=scope_ids(case, matrix.sources),
        ),
        "answer": out["answer"],
        "tool_args": [c["args"] for c in rec.tool_calls],
        "tool_results": [{k: c[k] for k in ("rows", "error", "status")} for c in rec.tool_calls],
        "unverified_numbers": out["unverified"],
        "partially_uncited": out["partially_uncited"],
        "citations": out["citations"],
        "query_rounds": out["query_rounds"],
        "model_rounds": rec.model_rounds,
        "widgets": out["widgets"],
        "evidence_items": len(out["evidence_texts"]),
        "missed_keys": missed_keys(
            case.gold_keys, eval_seed.key_map, rec.version_ids | out["retrieved_versions"]
        ),
    }
    if leg == "hybrid":
        fields["rerank_status"] = out["rerank_status"]
        # Why rerank or semantic search degraded for this case.
        fields["context_warnings"] = out["context_warnings"]
    return fields
