"""Web search query rewrite: privacy window, fallbacks, outcome schema and tool.status publish."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from modules.chat import worker
from modules.chat.schemas import WebSearchOutcomeRead
from tests.unit.modules.chat._worker_patch import patch_worker


def _config(*, reasoning: bool = True) -> Any:
    """Minimal AIExecutionConfig stand-in covering the fields the rewrite reads."""
    return SimpleNamespace(
        workspace_id=uuid4(),
        actor_user_id=1,
        membership_revision=1,
        gateway_identity="a" * 64,
        model_identity="a" * 64,
        configuration_revision=1,
        privacy=SimpleNamespace(allow_remote_reasoning=reasoning, reasoning_destinations=("d",)),
        endpoint_destination_id="d",
        gateway_base_url="https://x",
        gateway_api_key="k",
        gateway_kind="custom",
        allow_public_providers=True,
        defaults=SimpleNamespace(resolve=lambda role: None),
    )


class _Rig:
    """Patches config resolution and the gateway; records what the gateway received."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, reply: Any = None, *, reasoning: bool = True
    ) -> None:
        self.calls: list[tuple[Any, ...]] = []
        config = _config(reasoning=reasoning)
        monkeypatch.setattr(
            worker.settings_public, "get_ai_execution_config", AsyncMock(return_value=config)
        )
        monkeypatch.setattr(worker.settings_public, "derive_for_role", lambda cfg, role: cfg)
        rig = self

        class Gateway:
            """Gateway double: returns ``reply`` (or raises it when it is an exception)."""

            def __init__(self, **_: Any) -> None:
                pass

            async def structured(
                self, role: str, mapping: Any, policy: Any, messages: list[Any], *a: Any, **k: Any
            ) -> Any:
                rig.calls.append((policy, messages))
                if not policy.reasoning_allowed:
                    raise worker.PrivacyPolicyDenied("reasoning not allowed")
                if isinstance(reply, Exception):
                    raise reply
                return {"choices": [{"message": {"content": reply}}]}

        patch_worker(monkeypatch, "ModelGateway", Gateway)

        @asynccontextmanager
        async def factory() -> Any:
            """Session double."""
            yield MagicMock(rollback=AsyncMock())

        self.factory = factory

    async def rewrite(self, prompt: str, prior: list[dict[str, str]] | None = None) -> str:
        """Run the helper with doubles."""
        settings = SimpleNamespace(ai_allowed_endpoint_cidrs=())
        return await worker._rewrite_web_query(
            self.factory, settings, MagicMock(), MagicMock(), prompt, prior or []
        )  # type: ignore[arg-type]


async def test_rewrites_and_excludes_assistant_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _Rig(monkeypatch, json.dumps({"query": "  vinfast stock price  "}))
    prior = [
        {"role": "assistant", "content": "SECRET evidence"},
        {"role": "user", "content": "u1"},
        {"role": "user", "content": "u2"},
        {"role": "user", "content": "u3"},
        {"role": "user", "content": "u4" * 800},
    ]
    assert await rig.rewrite("giá cổ phiếu?", prior) == "vinfast stock price"
    messages = rig.calls[0][1]
    assert [m["role"] for m in messages] == ["system", "user", "user", "user", "user"]
    assert "SECRET" not in json.dumps(messages)
    assert [m["content"][:2] for m in messages[1:]] == ["u2", "u3", "u4", "gi"]
    assert len(messages[3]["content"]) == worker.REWRITE_MESSAGE_CHARS


async def test_gateway_error_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    assert (
        await _Rig(monkeypatch, RuntimeError("boom")).rewrite("literal prompt") == "literal prompt"
    )


async def test_reasoning_not_allowed_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _Rig(monkeypatch, json.dumps({"query": "x"}), reasoning=False)
    assert await rig.rewrite("literal prompt") == "literal prompt"
    assert rig.calls[0][0].reasoning_allowed is False


@pytest.mark.parametrize(
    "reply",
    [
        "not json",
        json.dumps({"query": "   "}),
        json.dumps({"q": "x"}),
        json.dumps({"query": "y" * 300}),
    ],
)
async def test_invalid_or_overlong_output_falls_back(
    monkeypatch: pytest.MonkeyPatch, reply: str
) -> None:
    assert await _Rig(monkeypatch, reply).rewrite("literal prompt") == "literal prompt"


def test_outcome_schema_with_and_without_query() -> None:
    old = WebSearchOutcomeRead.model_validate({"status": "used", "reason": None, "result_count": 2})
    assert old.query is None
    assert (
        WebSearchOutcomeRead.model_validate(
            {"status": "used", "result_count": 2, "query": "q"}
        ).query
        == "q"
    )


async def test_publish_tool_status_event(monkeypatch: pytest.MonkeyPatch) -> None:
    added: list[Any] = []
    session = MagicMock(add=added.append, commit=AsyncMock(), scalar=AsyncMock(return_value=4))

    @asynccontextmanager
    async def factory() -> Any:
        """Session double."""
        yield session

    patch_worker(
        monkeypatch, "_lock_live_response", AsyncMock(return_value=(True, SimpleNamespace()))
    )
    seq = await worker._publish_tool_status(
        factory,
        uuid4(),
        uuid4(),
        object(),
        2,
        "query_collected_data",
        "running",  # type: ignore[arg-type]
        scope=MagicMock(),
        access_fence=MagicMock(),
    )
    assert seq == 5
    assert added[0].event_type == "tool.status"
    assert added[0].data == {"tool": "query_collected_data", "state": "running"}
