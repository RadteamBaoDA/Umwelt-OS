"""Chat's public one-shot web search for background collectors: consent, quota, commit-before-egress."""

import hashlib
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest

from modules.chat._public import web_search_command as command

ENDPOINT = "https://search.example.test"
DESTINATION = f"web-search:{hashlib.sha256(ENDPOINT.encode()).hexdigest()[:32]}"
JSON = {"content-type": "application/json"}
SCOPE = SimpleNamespace(workspace_id=uuid4(), user_id=7, actor_user_id=7)


class _Session:
    """Records commit and rollback in the shared event log."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def commit(self) -> None:
        self.events.append("commit")

    async def rollback(self) -> None:
        self.events.append("rollback")


class _World:
    """Patches settings, quota and transport; ``events`` orders commit, quota and request."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, consent: bool = True) -> None:
        self.events: list[str] = []
        self.quota_ok = True
        self.quota_calls: list[tuple[str, int]] = []
        self.handler: Any = lambda request: httpx.Response(
            200,
            headers=JSON,
            json={
                "results": [
                    {"title": "<b>Hello</b>", "url": "https://a.example/x#f", "content": "snip"}
                ]
            },
        )
        privacy = SimpleNamespace(
            allow_remote_web_search=consent,
            web_search_destinations=frozenset({DESTINATION}),
        )
        config = SimpleNamespace(
            privacy=privacy,
            web_search_provider="searxng",
            web_search_endpoint=ENDPOINT,
            web_search_api_key="",
            web_search_credential_configured=False,
            web_search_fallbacks=(),
        )

        async def lock(session: Any, *, scope: Any) -> bool:
            self.events.append("lock")
            return True

        async def get_config(session: Any, settings: Any, redis: Any, *, scope: Any) -> Any:
            return config

        async def quota(redis: Any, owner: object, limit: int) -> bool:
            self.events.append("quota")
            self.quota_calls.append((str(owner), limit))
            return self.quota_ok

        def transport(endpoint: str, *args: Any, **kwargs: Any) -> httpx.MockTransport:
            def handle(request: httpx.Request) -> httpx.Response:
                self.events.append("request")
                return self.handler(request)

            return httpx.MockTransport(handle)

        monkeypatch.setattr(command.settings_public, "lock_ai_settings_for_share", lock)
        monkeypatch.setattr(command.settings_public, "get_ai_execution_config", get_config)
        monkeypatch.setattr(command.web, "consume_daily_quota", quota)
        monkeypatch.setattr(command, "approved_web_search_transport", transport)
        self.settings = SimpleNamespace(
            web_search_daily_limit=5, ai_allowed_endpoint_hosts=(), web_search_allowed_cidrs=()
        )

    async def run(self, query: str = "solid state batteries") -> command.CollectorSearchOutcome:
        return await command.search_web_for_collection(
            lambda: _Session(self.events),  # type: ignore[arg-type]
            self.settings,  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            query,
            scope=SCOPE,  # type: ignore[arg-type]
        )


async def test_consent_missing_is_not_configured_and_sends_nothing(monkeypatch) -> None:
    world = _World(monkeypatch, consent=False)
    outcome = await world.run()
    assert outcome.error == "not_configured" and outcome.results == ()
    assert "request" not in world.events and "quota" not in world.events


async def test_quota_charged_once_and_daily_limit_sends_nothing(monkeypatch) -> None:
    world = _World(monkeypatch)
    assert (await world.run()).error is None
    assert world.quota_calls == [(f"{SCOPE.workspace_id}:7", 5)]
    world.events.clear()
    world.quota_ok = False
    outcome = await world.run()
    assert outcome.error == "daily_limit" and "request" not in world.events


async def test_success_returns_sanitized_results(monkeypatch) -> None:
    world = _World(monkeypatch)
    outcome = await world.run()
    assert outcome.error is None and outcome.provider == "searxng"
    assert [(r.title, r.url) for r in outcome.results] == [("Hello", "https://a.example/x")]


async def test_provider_503_is_provider_error(monkeypatch) -> None:
    world = _World(monkeypatch)
    world.handler = lambda request: httpx.Response(503)
    outcome = await world.run()
    assert outcome.error == "provider_error" and outcome.results == ()


async def test_session_committed_before_request(monkeypatch) -> None:
    world = _World(monkeypatch)
    await world.run()
    assert world.events.index("commit") < world.events.index("request")
    assert world.events.index("quota") < world.events.index("commit")


@pytest.mark.parametrize(
    ("query", "code"), [("   ", "empty_query"), ("x" * 5000, "query_too_long")]
)
async def test_bad_query_codes_without_egress(monkeypatch, query: str, code: str) -> None:
    world = _World(monkeypatch)
    outcome = await world.run(query)
    assert outcome.error == code and world.events == []
