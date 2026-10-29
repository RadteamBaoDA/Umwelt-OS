"""Gateway reasoning split (provider fields, inline think tags across chunks) and upstream-error redaction."""

from types import SimpleNamespace
from typing import Any

from core.model_gateway.client import (
    _redact_secrets,
    _split_reasoning,
    _ThinkSplitter,
    _upstream_reason,
)


def _run(pieces: list[str]) -> tuple[str, str]:
    splitter, think, answer = _ThinkSplitter(), "", ""
    for piece in pieces:
        t, a = splitter.feed(piece)
        think, answer = think + t, answer + a
    t, a = splitter.flush()
    return think + t, answer + a


def test_inline_think_block_split_across_chunks() -> None:
    think, answer = _run(["  <thi", "nk>plan ", "more</th", "ink>\nHello", " world"])
    assert think == "plan more" and answer == "Hello world"


def test_plain_content_and_late_tag_stay_answer() -> None:
    assert _run(["Hello ", "<think>not reasoning</think>"]) == ("", "Hello <think>not reasoning</think>")
    assert _run(["<th", "ing else"]) == ("", "<thing else")


def test_unclosed_block_flushes_as_reasoning() -> None:
    assert _run(["<think>still going</th"]) == ("still going</th", "")


def test_split_reasoning_rewrites_chunk_fields() -> None:
    splitters: dict[int, _ThinkSplitter] = {}
    chunk: dict[str, Any] = {"choices": [{"index": 0, "delta": {"reasoning": "r1", "content": "<think>r2</think>ans"}}]}
    _split_reasoning(chunk, splitters)
    assert chunk["choices"][0]["delta"] == {"reasoning_content": "r1r2", "content": "ans"}
    empty: dict[str, Any] = {"choices": [{"index": 0, "delta": {"reasoning_content": "x"}}]}
    _split_reasoning(empty, splitters)
    assert empty["choices"][0]["delta"] == {"reasoning_content": "x"}


def test_upstream_reason_redacts_secrets() -> None:
    key = "sk-" + "A1b2C3d4" * 4
    exc: Any = SimpleNamespace(status_code=401, message=f"Bad key {key}; header Bearer abc.def.ghi; blob {'Z' * 40}")
    reason = _upstream_reason(exc)
    assert key not in reason and "abc.def.ghi" not in reason and "Z" * 40 not in reason
    assert reason.startswith("HTTP 401: Bad key [redacted]")
    assert _redact_secrets("model gpt-4o not found") == "model gpt-4o not found"
