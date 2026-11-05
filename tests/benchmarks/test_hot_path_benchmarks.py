"""Micro-benchmarks for hot pure functions: SQL guard parse, chunking, citation validation, SSE chunk parsing.

Run: ``RUN_BENCHMARKS=1 python -m pytest tests/benchmarks -q -s -p no:cacheprovider``. Each test asserts the
function's result (correctness first) and prints a ``BENCH`` line with min/median/p95/mean per-call microseconds.
When ``BENCHMARK_OUT`` names a file, the results are also written there as JSON. Baselines are recorded in
docs/performance-report.md; they are machine-specific, so no time ceiling is asserted.
"""

import json
import os
import platform
import statistics
import time
from collections.abc import Callable, Iterator
from typing import Any
from uuid import uuid4

import pytest

from core.chunking import chunk_text
from modules.chat import worker
from modules.chat.citations import validate_citations
from modules.chat.schemas import Citation, EvidenceItem
from modules.chat.sql_guard import check_select_policy
from modules.chat.stream import format_sse_event

pytestmark = pytest.mark.benchmark

_RESULTS: dict[str, dict[str, float]] = {}


def measure(name: str, fn: Callable[[], Any], *, rounds: int, warmup: int = 3) -> Any:
    """Time ``fn`` for ``rounds`` calls after ``warmup`` untimed calls and record per-call microseconds.

    Returns the last call's result so the caller can assert on it. Side effect: stores the statistics in
    ``_RESULTS`` (flushed to ``BENCHMARK_OUT`` by the module fixture) and prints a ``BENCH`` line.
    """
    result = None
    for _ in range(warmup):
        result = fn()
    samples: list[float] = []
    for _ in range(rounds):
        started = time.perf_counter()
        result = fn()
        samples.append((time.perf_counter() - started) * 1_000_000)
    samples.sort()
    stats = {
        "rounds": float(rounds),
        "min_us": samples[0],
        "median_us": statistics.median(samples),
        "p95_us": samples[min(len(samples) - 1, int(len(samples) * 0.95))],
        "mean_us": statistics.fmean(samples),
    }
    _RESULTS[name] = stats
    print(
        f"BENCH {name}: min={stats['min_us']:.1f}us median={stats['median_us']:.1f}us "
        f"p95={stats['p95_us']:.1f}us mean={stats['mean_us']:.1f}us rounds={rounds}"
    )
    return result


@pytest.fixture(scope="module", autouse=True)
def _flush_results() -> Iterator[None]:
    """Write the collected statistics to ``BENCHMARK_OUT`` (JSON) after the module, when requested."""
    yield
    target = os.getenv("BENCHMARK_OUT")
    if target and _RESULTS:
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(
                {"python": platform.python_version(), "machine": platform.machine(), "results": _RESULTS},
                handle, indent=2, sort_keys=True,
            )


_SIMPLE_SQL = "SELECT count(*) FROM scoped_observations WHERE observed_at > now() - interval '7 days'"
_CTE_SQL = (
    "WITH recent AS (SELECT provider, value, observed_at FROM scoped_observations "
    "WHERE observed_at > now() - interval '30 days'), "
    "agg AS (SELECT provider, avg(value) AS mean_value, max(observed_at) AS last_seen FROM recent GROUP BY provider) "
    "SELECT a.provider, a.mean_value, a.last_seen, d.title FROM agg a "
    "LEFT JOIN scoped_documents d ON d.title ILIKE '%' || a.provider || '%' "
    "ORDER BY a.mean_value DESC LIMIT 50"
)


def test_bench_sql_guard_simple() -> None:
    """Parse + policy check of a one-table aggregate (the most common model-written query)."""
    assert measure("sql_guard.simple", lambda: check_select_policy(_SIMPLE_SQL), rounds=500) is None


def test_bench_sql_guard_cte_join() -> None:
    """Parse + policy check of a two-CTE query with a join, ILIKE and ORDER BY."""
    assert measure("sql_guard.cte_join", lambda: check_select_policy(_CTE_SQL), rounds=300) is None


def test_bench_sql_guard_reject() -> None:
    """Rejection path: a forbidden relation raises ``ValueError`` (the cost the model pays per bad retry)."""

    def reject() -> str:
        try:
            check_select_policy("SELECT * FROM chat_messages")
        except ValueError as exc:
            return str(exc)
        raise AssertionError("expected rejection")

    assert measure("sql_guard.reject", reject, rounds=500)


_MARKDOWN = "\n\n".join(
    f"## Section {i}\n\n"
    + " ".join(f"Sentence {i}-{j} describes fictional dashboard metric alpha{j}." for j in range(40))
    for i in range(30)
)


def test_bench_chunk_text() -> None:
    """Token-aware chunking of a ~60 KB Markdown document with headings (default 750 tokens, 12 % overlap)."""
    chunks = measure("chunking.chunk_text_60kb", lambda: chunk_text(_MARKDOWN), rounds=15, warmup=2)
    assert len(chunks) > 5


def _citation_fixture(evidence_count: int, citation_count: int) -> tuple[list[EvidenceItem], list[Citation | dict[str, Any]]]:
    """Build matching evidence items and citations whose quotes are contained in the evidence."""
    source_id = uuid4()
    evidence: list[EvidenceItem] = []
    citations: list[Citation | dict[str, Any]] = []
    for index in range(evidence_count):
        item = EvidenceItem(
            source_id=source_id, source_generation=1, local_only=False, document_id=uuid4(),
            document_version_id=uuid4(), version_number=1, chunk_id=uuid4(), chunk_index=index,
            content=f"Evidence {index}: the quarterly report shows revenue growth of {index} percent. " * 20,
            title=f"Document {index}", canonical_url=f"https://example.test/doc/{index}",
        )
        evidence.append(item)
        if index < citation_count:
            citations.append({
                "sourceId": str(item.source_id), "documentId": str(item.document_id),
                "documentVersionId": str(item.document_version_id), "chunkId": str(item.chunk_id),
                "title": "ignored", "quote": f"revenue growth of {index} percent",
            })
    return evidence, citations


def test_bench_validate_citations() -> None:
    """Validate 5 citations against 20 retrieved evidence items (typical grounded answer)."""
    evidence, citations = _citation_fixture(20, 5)
    result = measure("citations.validate_5_of_20", lambda: validate_citations(citations, evidence), rounds=300)
    assert result.is_valid and len(result.valid_citations) == 5


def test_bench_validate_citations_large() -> None:
    """Validate 20 citations against 100 evidence items (upper bound of the retrieval context)."""
    evidence, citations = _citation_fixture(100, 20)
    result = measure("citations.validate_20_of_100", lambda: validate_citations(citations, evidence), rounds=100)
    assert result.is_valid and len(result.valid_citations) == 20


_DELTA = json.dumps({"id": "chatcmpl-1", "object": "chat.completion.chunk", "choices": [
    {"index": 0, "delta": {"content": "tok12 "}, "finish_reason": None}]})
_REASONING = json.dumps({"choices": [{"index": 0, "delta": {"reasoning_content": "Considering the question"}}]})


def test_bench_parse_sse_chunk() -> None:
    """Decode one provider ``data:`` payload into delta fields (executed once per streamed chunk)."""
    chunk = measure("sse.parse_chunk_content", lambda: worker._parse_sse_chunk(_DELTA), rounds=5000)
    assert chunk is not None and chunk.content == "tok12 "
    reasoning = measure("sse.parse_chunk_reasoning", lambda: worker._parse_sse_chunk(_REASONING), rounds=5000)
    assert reasoning is not None and reasoning.reasoning == "Considering the question"


def test_bench_format_sse_event() -> None:
    """Format one outgoing SSE frame (executed once per delivered event)."""
    frame = measure(
        "sse.format_event", lambda: format_sse_event("message.delta", {"text": "tok12 "}, event_id="r:12"), rounds=5000
    )
    assert frame == 'id: r:12\nevent: message.delta\ndata: {"text":"tok12 "}\n\n'
