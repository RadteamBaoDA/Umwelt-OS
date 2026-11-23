"""Opt-in one-call smoke of the JEV judge endpoint: prints only the response's JSON key structure.

Skipped unless ``CHAT_JUDGE_SMOKE=1`` and ``E2E_JUDGE_*`` are set in ``.env.e2e-real``. It makes exactly one
real request with a fixed toy question, so the owner approves each run. It never prints the request headers,
the key, or any response value: only the HTTP status and the key names and value types of the JSON body.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.integration.chat_eval.judge import (
    build_body,
    decisions_url,
    judge_config,
    load_env_file,
)

ENV_FILE = Path(__file__).resolve().parents[2] / ".env.e2e-real"
_CFG = judge_config(load_env_file(ENV_FILE))


def _shape(value: Any) -> Any:
    """Key names and value type names only, so the printed structure carries no content (pure)."""
    if isinstance(value, dict):
        return {key: _shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_shape(value[0])] if value else []
    return type(value).__name__


@pytest.mark.skipif(
    os.getenv("CHAT_JUDGE_SMOKE") != "1",
    reason="set CHAT_JUDGE_SMOKE=1 to make one real judge call (owner approval)",
)
@pytest.mark.skipif(_CFG is None, reason="set E2E_JUDGE_BASE_URL/MODEL/API_KEY in .env.e2e-real")
def test_judge_endpoint_key_structure() -> None:
    """One decisions call with a toy question; prints the status and the JSON key structure."""
    assert _CFG is not None
    body = build_body(
        _CFG.model,
        "What colour is the sky on a clear day?",
        ("The sky is blue on a clear day.",),
        False,
        "Blue.",
    )

    async def call() -> httpx.Response:
        """POST the toy body to the decisions endpoint with the bearer header."""
        async with httpx.AsyncClient(timeout=60.0) as client:
            return await client.post(
                decisions_url(_CFG.base_url),
                json=body,
                headers={"Authorization": f"Bearer {_CFG.api_key}"},
            )

    resp = asyncio.run(call())
    print(f"\nJUDGE SMOKE status={resp.status_code}")
    try:
        payload = resp.json()
    except ValueError:
        print("JUDGE SMOKE body=not-json")
        pytest.fail("judge endpoint did not return JSON")
    print("JUDGE SMOKE shape=" + json.dumps(_shape(payload), sort_keys=True))
    assert resp.status_code < 400
