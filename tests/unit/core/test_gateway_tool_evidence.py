"""F2.2 tool-rejection classification, F2.4 transient failures, and the 1-hour TTL of ``unsupported`` evidence."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError

from core.model_gateway.client import (
    CapabilityUnsupported,
    ModelGateway,
    ModelGatewayError,
    _GatewayTimeout,
    is_tool_capability_rejection,
)
from core.model_gateway.schemas import ModelMapping
from modules.settings.models import PostgresCapabilityEvidence
from tests.unit.core.test_model_gateway import _MAPPING, _gateway


def _status(code: int, message: str) -> APIStatusError:
    """Provider HTTP error as the OpenAI SDK raises it."""
    response = httpx.Response(code, request=httpx.Request("POST", "https://x/v1/chat/completions"))
    return APIStatusError(message, response=response, body=None)


def _rejected(code: int, message: str) -> CapabilityUnsupported:
    """The gateway's mapped error for a 4xx answer, with its cause chain intact."""
    cause = _status(code, message)
    error = CapabilityUnsupported(f"Gateway rejected streaming (HTTP {code}: {message})")
    error.__cause__ = cause
    return error


@pytest.mark.parametrize("message", [
    "tools are not supported by this model", "This model does not support tools", "tool use is not supported",
    "function calling is not supported for this model", "No endpoints found that support tool use.",
    "model doesn't support function calling"])
def test_explicit_capability_phrases_are_tool_rejections(message: str) -> None:
    """Only explicit capability statements qualify."""
    assert is_tool_capability_rejection(_rejected(400, message))


@pytest.mark.parametrize("message", [
    "messages.2: tool_use ids were found without tool_result blocks; tool_use_id toolu_1",
    "Invalid parameter: tool_call_id does not match any function call", "tools.0.input_schema: invalid JSON schema",
    "Invalid function arguments: expected string", "context length exceeded, reduce the prompt tools",
    "Forced tool_choice is not supported together with extended thinking"])
def test_unrelated_400_mentioning_tool_or_function_is_not_a_rejection(message: str) -> None:
    """Pairing, schema, argument and context errors never mark the model unsupported."""
    assert not is_tool_capability_rejection(_rejected(400, message))


@pytest.mark.parametrize("code", [404, 405])
def test_404_and_405_on_the_endpoint_count_as_rejection(code: int) -> None:
    """The endpoint does not exist for this call: tools cannot work."""
    assert is_tool_capability_rejection(_rejected(code, "Not Found"))


class _Evidence:
    """In-memory evidence store capturing what the gateway writes."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    async def lookup(self, role: str, mapping: ModelMapping, capability: str) -> str | None:
        return None

    async def record(self, role: str, mapping: ModelMapping, capability: str, result: str) -> None:
        self.records.append((capability, result))


def _gw(evidence: _Evidence) -> ModelGateway:
    """Gateway with in-memory evidence."""
    return _gateway(MagicMock(), evidence=evidence)


def _cause(error: ModelGatewayError, cause: BaseException) -> ModelGatewayError:
    """Attach ``cause`` the way the gateway's ``raise ... from`` does."""
    error.__cause__ = cause
    return error


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    _GatewayTimeout("Model gateway request timed out"),
    _cause(ModelGatewayError("Model gateway request failed"), APITimeoutError(request=httpx.Request("POST", "https://x"))),
    _cause(ModelGatewayError("Model gateway request failed"), APIConnectionError(message="x", request=httpx.Request("POST", "https://x"))),
    _cause(ModelGatewayError("Model gateway error (HTTP 503: down)"), _status(503, "down")),
    _cause(ModelGatewayError("Model gateway error (HTTP 429: slow)"), _status(429, "slow")),
])
async def test_transient_failures_record_nothing(error: ModelGatewayError) -> None:
    """A timeout, connection fault, 429 or 5xx must not overwrite a stored ``supported``."""
    evidence = _Evidence()
    await _gw(evidence)._record_failure("chat", _MAPPING, "streaming", error, rejected_tools=True)
    assert evidence.records == []


@pytest.mark.asyncio
async def test_deterministic_failure_is_still_recorded_failed() -> None:
    """A 401 is a deterministic answer: recorded failed, never unsupported."""
    evidence = _Evidence()
    error = _cause(ModelGatewayError("Model gateway error (HTTP 401: bad key)"), _status(401, "bad key"))
    await _gw(evidence)._record_failure("chat", _MAPPING, "streaming", error, rejected_tools=True)
    assert evidence.records == [("streaming", "failed")]


@pytest.mark.asyncio
async def test_tool_rejection_records_unsupported_only_for_clear_signal() -> None:
    """A tool_use_id 400 records failed only; an explicit phrase also records tools unsupported."""
    evidence = _Evidence()
    gw = _gw(evidence)
    await gw._record_failure("chat", _MAPPING, "streaming", _rejected(400, "unexpected tool_use_id in tool_result"), rejected_tools=True)
    assert evidence.records == [("streaming", "failed")]
    evidence.records.clear()
    await gw._record_failure("chat", _MAPPING, "streaming", _rejected(400, "This model does not support tools"), rejected_tools=True)
    assert ("tools", "unsupported") in evidence.records


class _Row:
    """Result of ``execute(...)`` yielding one (result, checked_at) row."""

    def __init__(self, row: tuple[str, datetime] | None) -> None:
        self._row = row

    def first(self) -> tuple[str, datetime] | None:
        return self._row


def _store(row: tuple[str, datetime] | None) -> PostgresCapabilityEvidence:
    """Evidence store over a fake session returning ``row``."""
    session = MagicMock()
    session.execute = AsyncMock(return_value=_Row(row))

    @asynccontextmanager
    async def factory() -> Any:
        yield session

    return PostgresCapabilityEvidence(factory, workspace_id=uuid4(), model_identity="a" * 64)


@pytest.mark.asyncio
async def test_unsupported_evidence_expires_after_one_hour() -> None:
    """A fresh ``unsupported`` blocks; one older than the TTL reads as unknown so the provider is asked again."""
    now = datetime.now(UTC)
    assert await _store(("unsupported", now - timedelta(minutes=59))).lookup("chat", _MAPPING, "tools") == "unsupported"
    assert await _store(("unsupported", now - timedelta(minutes=61))).lookup("chat", _MAPPING, "tools") is None
    assert await _store(("supported", now - timedelta(days=30))).lookup("chat", _MAPPING, "tools") == "supported"
    assert await _store(None).lookup("chat", _MAPPING, "tools") is None
