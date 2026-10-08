"""Unit tests for core telemetry, metric registries, and log redaction.

Tests TraceContext binding, log sanitization/redaction, MetricsRegistry counters,
histograms, snapshot formatting, and async timing spans.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

import pytest

# Attempt standard import first; if not present, check out sibling P11 worktree
try:
    from core import telemetry
except ModuleNotFoundError:
    p11_path = Path("D:/Project/Umwelt-OS-p11/core/telemetry.py")
    if p11_path.exists():
        spec = importlib.util.spec_from_file_location("core.telemetry", p11_path)
        if spec and spec.loader:
            telemetry = importlib.util.module_from_spec(spec)
            sys.modules["core.telemetry"] = telemetry
            spec.loader.exec_module(telemetry)
        else:
            telemetry = None  # type: ignore[assignment]
    else:
        telemetry = None  # type: ignore[assignment]

pytestmark = pytest.mark.skipif(
    telemetry is None,
    reason="core.telemetry is only available when Phase 11 observability is present",
)


class TestTraceContext:
    """Test suite for TraceContext and contextvar bindings."""

    def test_default_empty_trace_context(self) -> None:
        """current_trace returns empty context when no trace IDs are bound."""
        trace = telemetry.current_trace()
        assert isinstance(trace, telemetry.TraceContext)
        assert trace.request_id is None
        assert trace.agent_run_id is None
        assert trace.ingestion_run_id is None
        assert trace.tool_call_id is None

    def test_bind_trace_context_manager(self) -> None:
        """bind_trace binds correlation IDs for the duration of the context block."""
        assert telemetry.current_trace().request_id is None

        with telemetry.bind_trace(request_id="req-123", agent_run_id="run-456") as ctx:
            assert ctx.request_id == "req-123"
            assert ctx.agent_run_id == "run-456"
            assert telemetry.current_trace().request_id == "req-123"

            # Nested bind merges additional IDs
            with telemetry.bind_trace(tool_call_id="call-789") as nested:
                assert nested.request_id == "req-123"
                assert nested.tool_call_id == "call-789"
                assert telemetry.current_trace().tool_call_id == "call-789"

            # After nested exit, tool_call_id is restored
            assert telemetry.current_trace().tool_call_id is None

        # After outer exit, context is restored to initial
        assert telemetry.current_trace().request_id is None


class TestLogSanitization:
    """Test suite for redact_text and redact_mapping (log extra sanitization)."""

    def test_redact_bearer_and_basic_tokens(self) -> None:
        """redact_text masks Bearer and Basic tokens."""
        text = "Request headers: Bearer my_secret_token_12345678"
        redacted = telemetry.redact_text(text)
        assert "Bearer [redacted]" in redacted
        assert "my_secret_token_12345678" not in redacted

    def test_redact_url_userinfo(self) -> None:
        """redact_text masks password/user in URL authority."""
        url = "postgres://admin:super_secret_pw@localhost:5432/umwelt"
        redacted = telemetry.redact_text(url)
        assert "postgres://[redacted]@localhost:5432/umwelt" == redacted
        assert "super_secret_pw" not in redacted

    def test_redact_provider_keys(self) -> None:
        """redact_text detects and masks OpenAI, GitHub, Slack, and Google key shapes."""
        openai_key = "sk-abcdefghijklmnopqrstuvwxyz1234"
        github_pat = "ghp_123456789012345678901234567890"
        google_key = "AIzaSyDummyGoogleKey123456789012345"

        msg = f"Connecting using {openai_key}, {github_pat}, and {google_key}"
        redacted = telemetry.redact_text(msg)

        assert openai_key not in redacted
        assert github_pat not in redacted
        assert google_key not in redacted
        assert "[redacted]" in redacted

    def test_sanitize_log_extra_mapping(self) -> None:
        """redact_mapping masks sensitive keys and summarizes content fields to length."""
        extra = {
            "api_key": "secret_key_123",
            "password": "my_password",
            "session_token": "token_abc",
            "prompt": "Summarize this long confidential document please.",
            "document": "Top secret content here.",
            "status": "active",
            "count": 42,
            "nested": {
                "credential": "internal_password",
                "normal_field": "visible",
            },
        }

        sanitized = telemetry.redact_mapping(extra)

        # Sensitive keys masked
        assert sanitized["api_key"] == "[redacted]"
        assert sanitized["password"] == "[redacted]"
        assert sanitized["session_token"] == "[redacted]"
        assert sanitized["nested"]["credential"] == "[redacted]"

        # Content keys converted to length summary
        assert sanitized["prompt"] == "[49 omitted]"
        assert sanitized["document"] == "[24 omitted]"

        # Harmless fields untouched
        assert sanitized["status"] == "active"
        assert sanitized["count"] == 42
        assert sanitized["nested"]["normal_field"] == "visible"

    def test_redact_mapping_deep_recursion_guard(self) -> None:
        """redact_mapping caps recursion depth at 6 to prevent stack overflow."""
        current: dict[str, Any] = {"leaf": "value"}
        for _ in range(10):
            current = {"child": current}

        sanitized = telemetry.redact_mapping(current)
        # Deepest child should be truncated to [redacted]
        deep = sanitized
        while isinstance(deep, dict) and "child" in deep:
            deep = deep["child"]
        assert deep == "[redacted]"


class TestMetricsRegistry:
    """Test suite for MetricsRegistry counters, histograms, and snapshots."""

    def test_counter_increments_and_snapshot(self) -> None:
        """MetricsRegistry tracks counters and generates structured snapshots."""
        reg = telemetry.MetricsRegistry()
        reg.inc("api_requests_total", 1, endpoint="/chat", status="200")
        reg.inc("api_requests_total", 3, endpoint="/chat", status="200")
        reg.inc("api_requests_total", 1, endpoint="/chat", status="500")

        snapshot = reg.snapshot()
        counters = snapshot["counters"]
        assert len(counters) == 2

        success_entry = next(c for c in counters if c["labels"]["status"] == "200")
        assert success_entry["name"] == "api_requests_total"
        assert success_entry["value"] == 4

        error_entry = next(c for c in counters if c["labels"]["status"] == "500")
        assert error_entry["value"] == 1

    def test_histogram_observations_and_buckets(self) -> None:
        """MetricsRegistry accumulates histogram counts, sum_ms, and bucket counts."""
        reg = telemetry.MetricsRegistry()
        reg.observe("request_duration_ms", 12.0, route="/sources")
        reg.observe("request_duration_ms", 45.0, route="/sources")
        reg.observe("request_duration_ms", 150.0, route="/sources")

        snapshot = reg.snapshot()
        histograms = snapshot["histograms"]
        assert len(histograms) == 1

        h = histograms[0]
        assert h["name"] == "request_duration_ms"
        assert h["count"] == 3
        assert h["sum_ms"] == pytest.approx(207.0, rel=1e-3)
        assert len(h["buckets"]) == len(telemetry.BUCKETS_MS) + 1

    def test_metrics_cardinality_bound_overflow(self) -> None:
        """MetricsRegistry folds excess series into overflow label when MAX_SERIES is hit."""
        reg = telemetry.MetricsRegistry()
        # Hit MAX_SERIES limit
        for i in range(telemetry.MAX_SERIES + 10):
            reg.inc("dynamic_metric", 1, item=f"id_{i}")

        snapshot = reg.snapshot()
        counters = snapshot["counters"]
        # Max distinct series is bounded
        assert len(counters) <= telemetry.MAX_SERIES + 1
        # Overflow series exists
        overflow_entries = [c for c in counters if c["labels"].get("overflow") == "true"]
        assert len(overflow_entries) == 1
        assert overflow_entries[0]["value"] == 10

    def test_isolated_swallows_sink_errors(self) -> None:
        """_isolated ensures errors in telemetry hooks never raise to callers."""
        def faulty_action() -> None:
            raise RuntimeError("Redis unreachable")

        # Must not raise
        telemetry._isolated(faulty_action)


class TestAsyncSpansAndTiming:
    """Test suite for async timed decorator and observe_ms."""

    @pytest.mark.asyncio
    async def test_timed_decorator_records_duration(self) -> None:
        """timed decorator records async execution duration in milliseconds."""
        initial_reg = telemetry.registry
        test_reg = telemetry.MetricsRegistry()
        telemetry.registry = test_reg

        try:
            @telemetry.timed("async_operation_ms", op="fetch_data")
            async def mock_async_op(delay_s: float) -> str:
                await asyncio.sleep(delay_s)
                return "completed"

            result = await mock_async_op(0.02)
            assert result == "completed"

            snapshot = test_reg.snapshot()
            histograms = snapshot["histograms"]
            assert len(histograms) == 1
            entry = histograms[0]
            assert entry["name"] == "async_operation_ms"
            assert entry["count"] == 1
            assert entry["labels"] == {"op": "fetch_data"}
            assert entry["sum_ms"] >= 15.0
        finally:
            telemetry.registry = initial_reg


class TestRedactedRecordsStayFormattable:
    """Redaction must not strip record.args: uvicorn's AccessFormatter unpacks it per request."""

    def _record(self, msg: str, args: tuple[Any, ...]) -> logging.LogRecord:
        telemetry.install_log_redaction()
        return logging.getLogger("uvicorn.access").makeRecord(
            "uvicorn.access", logging.INFO, __file__, 1, msg, args, None
        )

    def test_uvicorn_access_formatter_formats_redacted_record(self) -> None:
        from uvicorn.logging import AccessFormatter

        record = self._record(
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:1", "GET", "/api?token=sk-live-abcdef1234567890abcdef", "1.1", 200),
        )
        out = AccessFormatter("%(client_addr)s - %(request_line)s %(status_code)s", use_colors=False).format(record)
        assert "GET" in out and "200" in out
        assert "sk-live-abcdef1234567890abcdef" not in out

    def test_secret_spanning_template_and_args_is_still_redacted(self) -> None:
        record = self._record("Authorization: Bearer %s", ("abcdefghijklmnop1234567890",))
        assert "abcdefghijklmnop1234567890" not in record.getMessage()
        assert "abcdefghijklmnop1234567890" not in logging.Formatter("%(message)s").format(record)

    def test_plain_record_keeps_args_for_any_formatter(self) -> None:
        record = self._record("hello %s", ("world",))
        assert record.args == ("world",)
        assert logging.Formatter("%(message)s").format(record) == "hello world"


class TestRedactionStableRecords:
    """Collapse-triggering paths must stay formattable by uvicorn's real config; args are frozen by value."""

    def _record(self, msg: str, args: tuple[Any, ...]) -> logging.LogRecord:
        telemetry.install_log_redaction()
        return logging.getLogger("uvicorn.access").makeRecord(
            "uvicorn.access", logging.INFO, __file__, 1, msg, args, None
        )

    def test_cookie_path_formats_with_real_uvicorn_config(self) -> None:
        from uvicorn.config import LOGGING_CONFIG
        from uvicorn.logging import AccessFormatter

        record = self._record(
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:1", "GET", "/?cookie=SECRET&authorization=Bearer x", "1.1", 200),
        )
        cfg = LOGGING_CONFIG["formatters"]["access"]
        out = AccessFormatter(cfg["fmt"], use_colors=False).format(record)
        assert "200" in out  # still formattable; every str field of a secret-bearing line is masked
        assert "SECRET" not in out and "Bearer x" not in out
        assert "SECRET" not in record.getMessage()

    def test_secret_split_across_args_is_not_leaked(self) -> None:
        # Each arg is clean alone; only the joined line reveals the secret. Masking one arg at a time
        # would hide "cookie=" and let SECRETVALUE through.
        from uvicorn.config import LOGGING_CONFIG
        from uvicorn.logging import AccessFormatter

        record = self._record('%s - "%s %s HTTP/%s" %d', ("127.0.0.1:1", "cookie=", "SECRETVALUE", "1.1", 200))
        out = AccessFormatter(LOGGING_CONFIG["formatters"]["access"]["fmt"], use_colors=False).format(record)
        assert "SECRETVALUE" not in out
        assert "SECRETVALUE" not in record.getMessage()

    def test_decimal_args_stay_formattable(self) -> None:
        from decimal import Decimal

        record = self._record("amount %.2f count %d", (Decimal("12.345"), Decimal(3)))
        assert record.getMessage() == "amount 12.35 count 3"

    def test_fraction_arg_is_frozen(self) -> None:
        from fractions import Fraction

        record = self._record("ratio %s", (Fraction(1, 3),))
        assert isinstance(record.args, tuple) and isinstance(record.args[0], str)
        assert record.getMessage() == "ratio 1/3"

    def test_int_and_float_subclass_own_str_never_runs(self) -> None:
        class Evil(int):
            calls = 0

            def __str__(self) -> str:
                Evil.calls += 1
                return "ok" if Evil.calls == 1 else "api_key=sk-abcdefghijklmnop1234"

            __repr__ = __str__

        class EvilF(float):
            def __str__(self) -> str:
                return "api_key=sk-abcdefghijklmnop1234"

        record = self._record("a %s b %d c %.1f d %s", (Evil(1), Evil(2), EvilF(1.5), EvilF(2.5)))
        out = logging.Formatter("%(message)s").format(record)
        assert "sk-abcdefghijklmnop1234" not in out
        assert logging.Formatter("%(message)s").format(record) == out
        assert out == "a 1 b 2 c 1.5 d 2.5"

    def test_mapping_args_are_frozen_by_value(self) -> None:
        class Evil(int):
            calls = 0

            def __str__(self) -> str:
                Evil.calls += 1
                return "ok" if Evil.calls == 1 else "api_key=sk-abcdefghijklmnop1234"

            __repr__ = __str__

        record = self._record("%(a)s %(a)d", ({"a": Evil(1)},))
        out = logging.Formatter("%(message)s").format(record)
        assert "sk-abcdefghijklmnop1234" not in out
        assert logging.Formatter("%(message)s").format(record) == out
        assert out == "1 1"

    def test_mapping_float_arg_still_formats(self) -> None:
        record = self._record("%(b).1f %(n)s", ({"b": 1.25, "n": {"x": 1}},))
        assert record.getMessage() == "1.2 {'x': 1}"

    def test_decimal_subclass_with_custom_str_is_still_frozen(self) -> None:
        from decimal import Decimal

        class Sneaky(Decimal):
            def __str__(self) -> str:
                return "api_key=sk-abcdefghijklmnop1234"

        record = self._record("v %s", (Sneaky("1"),))
        assert "sk-abcdefghijklmnop1234" not in logging.Formatter("%(message)s").format(record)

    def test_non_str_args_are_frozen_by_value(self) -> None:
        class Odd:
            def __str__(self) -> str:
                return "fine"

            def __repr__(self) -> str:
                return "token=SECRETVALUE123456"

        record = self._record("obj %s", (Odd(),))
        rendered = logging.Formatter("%(message)s").format(record)
        assert "SECRETVALUE123456" not in rendered
        assert "SECRETVALUE123456" not in repr(record.args)
