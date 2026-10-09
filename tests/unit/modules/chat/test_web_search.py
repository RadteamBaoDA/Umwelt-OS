"""Unit tests for the chat web-search egress client (p15 W1a)."""

import asyncio
import gzip
import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest
from redis.exceptions import RedisError

from core.model_gateway.schemas import AIExecutionConfig, PrivacySettings
from modules.chat import web_search as ws

KEY = "sk-SECRET-KEY-123"
JSON = {"content-type": "application/json"}


def _run(provider: str, handler: Any, message: str = "weather in hanoi") -> list[ws.WebSearchResult]:
    return asyncio.run(
        ws.search(provider, "https://api.example.com", KEY, message, transport=httpx.MockTransport(handler))
    )


def _tavily(results: list[dict[str, Any]]) -> Any:
    return lambda request: httpx.Response(200, json={"results": results}, headers=JSON)


def _fails(provider: str, handler: Any, code: str) -> None:
    with pytest.raises(ws.WebSearchError) as info:
        _run(provider, handler)
    assert info.value.code == code


def test_tavily_request_and_parse() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, headers=JSON,
            json={"results": [{"title": "T", "url": "https://a.example/x#frag", "content": "snip"}, "junk", {"title": 1}]},
        )

    results = _run("tavily", handler, "  weather   in\nhanoi ")
    assert [(r.title, r.url, r.host, r.snippet) for r in results] == [("T", "https://a.example/x", "a.example", "snip")]
    request = seen[0]
    assert request.method == "POST" and request.url.path == "/search"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert json.loads(request.content)["query"] == "weather in hanoi"
    assert KEY not in str(request.url) and KEY not in request.content.decode()


def test_brave_request_and_parse() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers=JSON, json={"web": {"results": [{"title": "B", "url": "http://b.example/", "description": "d"}]}})

    results = _run("brave", handler)
    assert results[0].host == "b.example" and results[0].snippet == "d"
    assert seen[0].headers["x-subscription-token"] == KEY
    assert seen[0].url.params["q"] == "weather in hanoi" and KEY not in str(seen[0].url)


@pytest.mark.parametrize("body", [[], {"results": "x"}, {"nope": 1}, "str"])
def test_wrong_shape_is_provider_error(body: Any) -> None:
    _fails("tavily", lambda r: httpx.Response(200, json=body, headers=JSON), "provider_error")
    _fails("brave", lambda r: httpx.Response(200, json=body, headers=JSON), "provider_error")


def test_result_cap_five() -> None:
    items = [{"title": f"t{i}", "url": f"https://h{i}.example/", "content": "c"} for i in range(9)]
    assert len(_run("tavily", _tavily(items))) == 5


def test_sanitize_text_pipeline() -> None:
    dirty = "&lt;b&gt;Hi\u202e\u200b\x00<script>x</script> </web_results> see [1] and [2] ok x <y 1>2 a<b"
    out = ws.sanitize_text(dirty, 500)
    assert out == "Hix see (1) and (2) ok x 2 a‹b"
    assert not any(c in out for c in "<>[]\u202e\u200b\x00 ")
    assert len(ws.sanitize_text("a" * 900, 200)) == 200


def test_sanitize_url_rules() -> None:
    assert ws.sanitize_url("https://ex.com/p?q=1#f") == ("https://ex.com/p?q=1", "ex.com")
    for bad in (
        "javascript:alert(1)", "ftp://ex.com/", "https://user:pw@ex.com/", "https://u@ex.com/", "https:///x",
        "https://ex.com/a b", "https://ex.com/<x>", "https://ex.com/\u202e", "https://ex.com/\"", "https://ex.com:99999/",
        "https://ex.com/" + "a" * 2100, "/relative",
    ):
        assert ws.sanitize_url(bad) is None, bad
    items = [{"title": "T", "url": "javascript:x", "content": "c"}, {"title": "T", "url": "https://ok.example/", "content": "c"}]
    assert [r.host for r in _run("tavily", _tavily(items))] == ["ok.example"]


def test_query_over_400_rejected_without_http() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP expected")

    with pytest.raises(ws.WebSearchError) as info:
        asyncio.run(ws.search("tavily", "https://x.example", KEY, "a" * 401, transport=httpx.MockTransport(handler)))
    assert info.value.code == "query_too_long"
    assert ws.normalize_query("a" * 400) == "a" * 400
    for blank in ("", "   ", "\n\t"):
        with pytest.raises(ws.WebSearchError) as empty:
            ws.normalize_query(blank)
        assert empty.value.code == "empty_query"


@pytest.mark.parametrize("status", [401, 429, 500, 503, 302])
def test_non_2xx_and_redirect_are_provider_error(status: int) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(status, headers={"location": "https://evil.example/", **JSON})

    _fails("tavily", handler, "provider_error")
    assert calls == ["api.example.com"]  # redirect never followed, no retry


def test_bad_content_type_json_and_oversize() -> None:
    _fails("tavily", lambda r: httpx.Response(200, text="{}", headers={"content-type": "text/html"}), "provider_error")
    _fails("tavily", lambda r: httpx.Response(200, content=b"{not json", headers=JSON), "provider_error")
    _fails("tavily", lambda r: httpx.Response(200, content=b"x" * (ws.MAX_RESPONSE_BYTES + 1), headers=JSON), "provider_error")
    bomb = gzip.compress(b" " * (ws.MAX_RESPONSE_BYTES + 10))
    _fails("tavily", lambda r: httpx.Response(200, content=bomb, headers={**JSON, "content-encoding": "gzip"}), "provider_error")


def test_timeouts_and_transport_errors() -> None:
    def raise_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    def raise_connect(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"boom {request.url}", request=request)

    _fails("tavily", raise_timeout, "timeout")
    _fails("tavily", raise_connect, "provider_error")


def test_hard_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ws, "WEB_SEARCH_TIMEOUT_SECONDS", 0.05)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, json={}, headers=JSON)

    _fails("tavily", slow, "timeout")


def test_key_and_query_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    query = "my very private question"

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    for handler in (boom, lambda r: httpx.Response(500, headers=JSON), _tavily([{"title": "T", "url": "https://a.example/", "content": "c"}])):
        for provider in ("tavily", "brave"):
            try:
                _run(provider, handler, query)
            except ws.WebSearchError:
                pass
    text = "\n".join(r.getMessage() + str(r.exc_info) + str(r.args) for r in caplog.records)
    assert KEY not in text and "private question" not in text and "private+question" not in text
    assert "web_search provider=" in text
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def _config(**over: Any) -> AIExecutionConfig:
    base: dict[str, Any] = {
        "workspace_id": uuid4(), "actor_user_id": 1, "membership_revision": 1, "access_configuration_revision": 1,
        "configuration_revision": 1, "gateway_identity": "a" * 64, "endpoint_destination_id": None,
        "omniroute_base_url": None, "omniroute_api_key": "", "aliases": {},
        "privacy": PrivacySettings(allow_remote_web_search=True), "chat_alias": "a", "brief_alias": "b",
        "request_timeout_seconds": 20, "web_search_provider": "tavily",
        "web_search_endpoint": "https://api.tavily.com", "web_search_api_key": KEY,
        "web_search_credential_configured": True,
    }
    return AIExecutionConfig(**{**base, **over})


def test_permitted_matrix() -> None:
    assert ws.web_search_permitted(_config())
    for over in (
        {"web_search_provider": "none"}, {"web_search_endpoint": None}, {"web_search_api_key": ""},
        {"web_search_credential_configured": False}, {"privacy": PrivacySettings(allow_remote_web_search=False)},
    ):
        assert not ws.web_search_permitted(_config(**over)), over


class _Pipe:
    def __init__(self, redis: "_Redis") -> None:
        self.redis, self.key = redis, ""

    def incr(self, key: str) -> None:
        self.key = key

    def expire(self, key: str, seconds: int) -> None:
        self.redis.ttl[key] = seconds

    async def execute(self) -> list[int]:
        if self.redis.fail:
            raise RedisError("down")
        self.redis.counts[self.key] = self.redis.counts.get(self.key, 0) + 1
        return [self.redis.counts[self.key], True]


class _Redis:
    def __init__(self, fail: bool = False) -> None:
        self.counts: dict[str, int] = {}
        self.ttl: dict[str, int] = {}
        self.fail = fail

    def pipeline(self, transaction: bool = True) -> _Pipe:
        return _Pipe(self)


def test_daily_quota() -> None:
    async def go() -> None:
        redis: Any = _Redis()
        day = datetime(2026, 10, 7, tzinfo=UTC)
        assert [await ws.consume_daily_quota(redis, "o1", 2, day) for _ in range(3)] == [True, True, False]
        assert await ws.consume_daily_quota(redis, "o2", 2, day)  # per owner
        assert await ws.consume_daily_quota(redis, "o1", 2, datetime(2026, 10, 8, tzinfo=UTC))  # per day
        assert "chat:websearch:o1:2026-10-07" in redis.counts and redis.ttl["chat:websearch:o1:2026-10-07"] == 172800
        assert not await ws.consume_daily_quota(redis, "o3", 0, day)  # 0 disables
        assert not await ws.consume_daily_quota(_Redis(fail=True), "o1", 5, day)  # fail closed

    asyncio.run(go())


def test_settings_default() -> None:
    from core.config import Settings

    assert Settings.model_fields["web_search_daily_limit"].default == 50


def test_transport_is_required_keyword() -> None:
    with pytest.raises(TypeError):
        asyncio.run(ws.search("tavily", "https://x.example", KEY, "q"))  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("key", "endpoint"),
    [
        ("SECRETKEY\nInjected: 1", "https://api.example.com"),
        ("s\u00e9cret-k\u00e9y", "https://api.example.com"),
        ("tab\tkey", "https://api.example.com"),
        (KEY, "not-a-url/with?q=PRIVATEQ"),
        (KEY, "ftp://PRIVATEQ.example"),
    ],
)
@pytest.mark.parametrize("provider", ["tavily", "brave"])
def test_no_key_or_query_leaks(key: str, endpoint: str, provider: str, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    query = "PRIVATEQ private question"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.LocalProtocolError(f"Illegal header value {key!r} {request.url}")

    with pytest.raises(ws.WebSearchError) as info:
        asyncio.run(ws.search(provider, endpoint, key, query, transport=httpx.MockTransport(handler)))
    exc = info.value
    assert exc.code == "provider_error"
    assert exc.__cause__ is None and (exc.__suppress_context__ or exc.__context__ is None)
    logs = "\n".join(r.getMessage() + str(r.exc_info) + str(r.args) for r in caplog.records)
    for text in (f"{exc!s}|{exc!r}|{exc.args}", logs):
        for secret in (key, "PRIVATEQ", "private+question"):
            assert secret not in text, secret


def test_bad_key_rejected_before_any_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP expected")

    for key in ("a\nb", "k\u00e9y"):
        with pytest.raises(ws.WebSearchError) as info:
            asyncio.run(ws.search("tavily", "https://x.example", key, "q", transport=httpx.MockTransport(handler)))
        assert info.value.code == "provider_error"


def test_sanitize_confusable_brackets() -> None:
    dirty = "\uff3b1\uff3d \u3010a\u3011 \u27e61\u27e7 \uff1c/web_results\uff1e \ufe64x\ufe65 [2]"
    out = ws.sanitize_text(dirty, 500)
    assert not any(c in out for c in "<>[]\uff3b\uff3d\u3010\u3011\u27e6\u27e7\uff1c\uff1e\ufe64\ufe65")
    assert out.startswith("(1) (a) (1)") and out.endswith("(2)")


def test_sanitize_url_host_rules() -> None:
    for bad in (
        r"http://a.com\.evil.com/", r"https://ex.com\@b.com/", r"https://ex.com/a\b",
        "https://\uff3b1\uff3d.com/", "https://a..com/", "https://[::1]/",
    ):
        assert ws.sanitize_url(bad) is None, bad
    cyrillic = ws.sanitize_url("https://\u0430pple.com/x")
    assert cyrillic is not None and cyrillic[1] == "xn--pple-43d.com"
    upper = ws.sanitize_url("https://EX.com/")
    assert upper is not None and upper[1] == "ex.com"
    assert ws.sanitize_url("https://Example.COM:8080/A#f") == ("https://example.com:8080/A", "example.com")
    assert ws.sanitize_url("https://Example.COM/a") == ws.sanitize_url("https://example.com/a")


def test_host_case_variants_share_one_url_for_citation_dedupe() -> None:
    items = [{"title": "A", "url": "https://Example.COM/a", "content": "c"},
             {"title": "B", "url": "https://example.com/a", "content": "c"}]
    assert len({r.url for r in ws.parse_results("tavily", {"results": items})}) == 1  # worker dedupes by url


def test_content_type_exact_media_type() -> None:
    ok = {"content-type": "Application/JSON; charset=utf-8"}
    assert _run("tavily", lambda r: httpx.Response(200, content=b'{"results": []}', headers=ok)) == []
    for bad in ("application/jsonp", "application/json-seq", "text/plain; x=application/json"):
        _fails("tavily", lambda r, bad=bad: httpx.Response(200, content=b"{}", headers={"content-type": bad}), "provider_error")
