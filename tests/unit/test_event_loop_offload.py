"""Event-loop offload (plan 5A.2 A1-A12): identical output to the former inline code, no callback over 100 ms.

Each offloaded path is exercised with a large input under ``loop_block_guard`` (asyncio debug mode,
``slow_callback_duration = 0.1``). Negative controls prove the guard really flags inline execution.
"""

import ast
import asyncio
import base64
import hashlib
import inspect
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from core import async_runtime
from core.auth.routes import get_auth_redis
from core.heavy_work import to_thread_joined
from modules.connectors import collection, live_map
from modules.connectors.providers import google_api, local_services, research, rest, social
from modules.connectors.providers.macro import ProviderPayloadError
from modules.connectors.public import ConnectorRecord
from modules.ingestion import parsers
from modules.ingestion import worker as ingestion_worker
from modules.knowledge.documents import public as documents
from tests.unit.modules._documents_patch import monkeypatch_documents

_CPU_DEFAULT = min(32, (os.cpu_count() or 1) + 4)  # asyncio's own implicit default, made explicit
BIG_PAGE_REPEAT = 40_000  # about 2 MB of HTML: inline HTMLParser.feed takes several hundred ms


def _big_html() -> str:
    """Return a multi-megabyte page with text, anchors and skipped script blocks."""
    unit = '<div><p>row &amp; text</p><a href="/p/{n}#f">l</a><script>x()</script></div>'
    return "".join(unit.format(n=n) for n in range(BIG_PAGE_REPEAT))


# ---------------------------------------------------------------- guardrail self-test

async def test_guard_flags_an_inline_blocking_callback(loop_block_guard):
    """Negative control: a synchronous 150 ms call on the loop must be reported by the guard."""
    time.sleep(0.15)  # noqa: ASYNC251  # deliberate blocking call: this is the thing the guard must catch
    await asyncio.sleep(0)
    assert loop_block_guard.slow, "debug-mode slow callback was not captured"
    loop_block_guard.slow.clear()


# ---------------------------------------------------------------- A1

def test_parse_page_matches_the_former_inline_parser():
    html = '<p>a</p><style>s</style><a href="/x">x</a><noscript>n</noscript><b>b &amp; c</b>'
    page = collection._PageText()
    page.feed(html)
    assert collection._parse_page(html) == (page.parts, page.links)


async def test_run_web_parses_big_pages_off_the_loop(monkeypatch, loop_block_guard):
    html = _big_html()
    await loop_block_guard.reset()  # building the input is test setup, not the code under test
    accepted: list[list[dict]] = []

    class Gate:
        async def fetch_bytes(self, url, **_kwargs):
            return rest.Fetched(html.encode())

    async def state(_run):
        return SimpleNamespace(cursor=None)

    async def accept(_run, records, _before, _after, update=None):
        accepted.append(records)

    monkeypatch.setattr(collection, "_state", state)
    monkeypatch.setattr(collection, "_accept_generic", accept)
    src = SimpleNamespace(id=1, type="web", provider=None, generation=1, status="active",
                          configuration={"url": "https://a.example/", "max_pages": 1, "max_depth": 0})
    await collection._run_web(SimpleNamespace(attempt=SimpleNamespace(source=src), gate=Gate()))
    expected = collection._PageText()
    expected.feed(html)
    assert accepted[0][0]["content"] == "\n".join(expected.parts)[:200_000]
    loop_block_guard.assert_clean()


async def test_inline_page_parse_would_be_flagged(loop_block_guard):
    """Negative control for A1: the legacy inline parse of a big page exceeds the budget."""
    collection._parse_page(_big_html())
    await asyncio.sleep(0)
    assert loop_block_guard.slow
    loop_block_guard.slow.clear()


# ---------------------------------------------------------------- A2

def _usgs_body(count: int = 8_000) -> bytes:
    features = [{"id": f"id{n}", "properties": {"title": f"quake {n}", "time": 1_700_000_000_000 + n, "url": f"https://e.example/{n}"},
                 "geometry": {"coordinates": [10.0, 20.0, 1.0]}} for n in range(count)]
    return json.dumps({"features": features}).encode()


async def test_live_map_parse_is_offloaded_and_identical(monkeypatch, loop_block_guard):
    body = _usgs_body()
    await loop_block_guard.reset()

    async def fetch(*_a, **_k):
        return rest.Fetched(body)

    monkeypatch.setattr(live_map.rest, "fetch_bounded", fetch)

    async def before_send():
        return None

    got = await live_map.fetch_events("usgs", country=None, owner=None, repo=None, cik=None, secret=None,
                                      contact=None, before_send=before_send)
    assert got == live_map._parse("usgs", body)
    assert len(got[0]) == live_map.MAX_ITEMS and got[1] is True
    loop_block_guard.assert_clean()


async def test_live_map_malformed_rows_still_map_to_value_error(monkeypatch):
    async def fetch(*_a, **_k):
        return rest.Fetched(json.dumps({"features": [{"id": "x"}]}).encode())

    monkeypatch.setattr(live_map.rest, "fetch_bounded", fetch)

    async def before_send():
        return None

    with pytest.raises(ValueError, match="provider_response_invalid"):
        await live_map.fetch_events("usgs", country=None, owner=None, repo=None, cik=None, secret=None,
                                    contact=None, before_send=before_send)


# ---------------------------------------------------------------- A3

def _crawl_payload(count: int = 500) -> list[dict]:
    return [{"provider_id": f"p{n}", "content": "word " * 100, "observed_at": "2026-01-01T00:00:00+00:00",
             "metadata": {"url": f"https://a.example/{n}"}} for n in range(count)]


def test_prepare_crawl_records_matches_the_former_inline_block():
    response = httpx.Response(200, json=_crawl_payload(40))
    records, canonical, payload_hash, record_hashes = ingestion_worker._prepare_crawl_records(response)
    old_records = [ConnectorRecord.model_validate(item) for item in response.json()]
    old_canonical = [r.model_dump(mode="json") for r in old_records]
    old_hash = hashlib.sha256(json.dumps(old_canonical, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False).encode()).hexdigest()
    old_record_hashes = [hashlib.sha256(json.dumps(
        {"version": d.get("version"), "content": d["content"], "metadata": d["metadata"]},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest() for d in old_canonical]
    assert records == old_records and canonical == old_canonical
    assert payload_hash == old_hash and record_hashes == old_record_hashes


@pytest.mark.parametrize("body", [
    b"not json", b"[]", b"{}",
    json.dumps([{"provider_id": "", "content": "x", "observed_at": "2026-01-01T00:00:00Z"}]).encode(),
    json.dumps([{"provider_id": "p", "content": "x", "observed_at": "2026-01-01T00:00:00Z",
                 "metadata": {"world_data": 1}}]).encode(),
])
def test_prepare_crawl_records_keeps_the_error_contract(body):
    with pytest.raises(ValueError, match="Browser collector returned invalid records"):
        ingestion_worker._prepare_crawl_records(httpx.Response(200, content=body))


async def test_prepare_crawl_records_runs_off_the_loop(loop_block_guard):
    response = httpx.Response(200, json=_crawl_payload())
    await loop_block_guard.reset()
    records, *_ = await to_thread_joined(ingestion_worker._prepare_crawl_records, response)
    assert len(records) == 500
    loop_block_guard.assert_clean()


# ---------------------------------------------------------------- A4

def _page(count: int) -> dict:
    return {"data": {"items": [{"id": n, "title": f"t{n}", "body": "b" * 50, "updated": "2026-01-01T00:00:00Z"}
                               for n in range(count)]}}


async def test_collect_rest_offloads_json_and_mapping_with_identical_result(loop_block_guard):
    config = SimpleNamespace(url="https://api.example.com/items", items_path="data.items", id_field="id",
                             title_field="title", content_field="body", updated_field="updated")
    payload = _page(400)
    body = json.dumps(payload).encode()
    await loop_block_guard.reset()

    async def fetch(url, **_kwargs):
        return rest.Fetched(body)

    got = await rest.collect_rest(config, None, fetch=fetch)
    expected = rest.map_page(json.loads(body), config, None)
    assert [r["provider_id"] for r in got.records] == [r["provider_id"] for r, _ in expected]
    loop_block_guard.assert_clean()


async def test_collect_rest_non_json_is_still_a_schema_change():
    config = SimpleNamespace(url="https://api.example.com/items", items_path="data.items", id_field="id",
                             title_field="title", content_field="body", updated_field="updated")

    async def fetch(url, **_kwargs):
        return rest.Fetched(b"<html>")

    with pytest.raises(rest.RestSchemaChanged):
        await rest.collect_rest(config, None, fetch=fetch)


async def test_provider_json_helpers_match_their_sync_counterparts():
    body = b'{"a": 1.5, "b": [1, 2]}'
    assert await google_api._json_async(body) == google_api._json(body) == {"a": Decimal("1.5"), "b": [1, 2]}
    assert await local_services._decode_json_async(body) == local_services._decode_json(body)
    with pytest.raises(google_api.GoogleProviderError, match="response_not_json"):
        await google_api._json_async(b"nope")
    with pytest.raises(local_services.LocalServiceError, match="response_not_json"):
        await local_services._decode_json_async(b"nope")


# ---------------------------------------------------------------- A5

def _boom(kind: str):
    def raiser():
        raise ProviderPayloadError(kind)
    return raiser


@pytest.mark.parametrize("kind", ["rate_limited", "incomplete", "credential", "entitlement", "invalid"])
async def test_payload_async_translates_errors_like_the_sync_wrapper(kind):
    try:
        collection._payload(_boom(kind))
    except Exception as exc:  # noqa: BLE001  # capturing the legacy exception type for comparison
        legacy = type(exc)
    else:  # pragma: no cover
        raise AssertionError("expected an exception")
    with pytest.raises(legacy):
        await collection._payload_async(_boom(kind))


async def test_payload_async_returns_the_mapper_result():
    assert await collection._payload_async(lambda a, b: a + b, 1, 2) == 3


async def test_big_payload_decode_does_not_block_the_loop(loop_block_guard):
    from modules.connectors.providers.macro import decode_json

    body = json.dumps({"rows": [{"k": n, "v": "x" * 10} for n in range(25_000)]}).encode()
    await loop_block_guard.reset()
    result = await collection._payload_async(decode_json, body)
    assert len(result["rows"]) == 25_000
    loop_block_guard.assert_clean()


# ---------------------------------------------------------------- A6

async def test_small_text_parse_timeout_joins_the_thread_and_keeps_the_exception(monkeypatch, tmp_path):
    finished = threading.Event()

    def slow_parse(*_args):
        time.sleep(0.3)
        finished.set()
        return parsers.ParsedDocument(text="x", metadata={}, warnings=[])

    monkeypatch.setattr(parsers, "parse_file", slow_parse)
    path = tmp_path / "a.txt"
    path.write_text("hello")
    with pytest.raises(TimeoutError, match="File parser exceeded its time limit"):
        await parsers.parse_file_bounded(path, "text/plain", 0.05, 1, 1)  # type: ignore[arg-type]
    assert finished.is_set(), "heavy slot would be released while the parser thread still runs"


async def test_small_text_parse_success_and_error_mapping(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("hello world")
    parsed = await parsers.parse_file_bounded(path, "text/plain", 5, 1, 1)
    assert parsed.text == parsers.parse_file(path, "text/plain", 1, 1).text
    bad = tmp_path / "b.json"
    bad.write_text("{not json")
    with pytest.raises(ValueError):
        await parsers.parse_file_bounded(bad, "application/json", 5, 1, 1)


# ---------------------------------------------------------------- A7

@pytest.mark.parametrize("size", [0, 10, 65_536, 65_537, 3_000_000])
async def test_content_hash_async_equals_content_hash(size):
    text = ("é" * size)[:size]
    assert await documents.content_hash_async(text) == documents.content_hash(text)


async def test_content_hash_async_offloads_only_large_inputs(monkeypatch):
    calls: list[int] = []
    real = to_thread_joined

    async def spy(fn, *args):
        calls.append(len(args[0]))
        return await real(fn, *args)

    monkeypatch_documents(monkeypatch, "to_thread_joined", spy)
    await documents.content_hash_async("a" * 100)
    await documents.content_hash_async("a" * 70_000)
    assert calls == [70_000]


async def test_crawl_helpers_match_inline_expressions():
    from modules.connectors import crawl_digest as crawl

    body = b"<html>" + b"x" * 5000
    assert crawl.page_digest_and_raw(body) == (hashlib.sha256(body).hexdigest(), base64.b64encode(body).decode("ascii"))
    pages: list[dict[str, object]] = [{"final_url": "u", "raw_content": "r", "n": 1}, {"final_url": "v", "raw_content": "s"}]
    expected = hashlib.sha256(json.dumps([{k: v for k, v in p.items() if k != "raw_content"} for p in pages],
                                         sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    assert crawl.result_digest(pages) == expected
    assert crawl.wire_size(pages) == len(json.dumps(pages, ensure_ascii=False, separators=(",", ":")).encode())


# ---------------------------------------------------------------- A8

def test_get_auth_redis_is_a_coroutine_dependency():
    assert asyncio.iscoroutinefunction(get_auth_redis)
    sentinel = object()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=sentinel)))
    assert asyncio.run(get_auth_redis(request)) is sentinel  # type: ignore[arg-type]


# ---------------------------------------------------------------- A10

async def test_dns_lookup_runs_on_the_dedicated_pool_and_timeouts_spare_the_default_executor():
    names: list[str] = []

    def slow_lookup(*_args):
        names.append(threading.current_thread().name)
        time.sleep(0.3)
        return [("ok",)]

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(async_runtime.run_dns_lookup(slow_lookup, "h", 443), 0.05)
    # The hung lookup holds a DNS thread, while the default executor is still immediately available.
    assert await asyncio.wait_for(asyncio.to_thread(lambda: "free"), 1) == "free"
    assert names and names[0].startswith("umwelt-dns")
    assert await async_runtime.run_dns_lookup(lambda: "same") == "same"


async def test_resolve_public_still_uses_the_patchable_module_level_resolver(monkeypatch):
    monkeypatch.setattr(rest, "getaddrinfo", lambda *_a: [(0, 0, 0, "", ("93.184.216.34", 443))])
    assert await rest.resolve_public("a.example", 443) == ["93.184.216.34"]
    monkeypatch.setattr(rest, "getaddrinfo", lambda *_a: [(0, 0, 0, "", ("10.0.0.1", 443))])
    with pytest.raises(rest.UnsafeDestination, match="non_public_destination"):
        await rest.resolve_public("a.example", 443)


async def test_configure_default_executor_sets_an_explicit_size(monkeypatch):
    monkeypatch.setenv("THREAD_POOL_WORKERS", "7")
    loop = asyncio.get_running_loop()
    executor = async_runtime.configure_default_executor()
    try:
        assert executor._max_workers == 7
        assert await asyncio.to_thread(lambda: threading.current_thread().name.startswith("umwelt-default"))
    finally:
        loop.set_default_executor(ThreadPoolExecutor(max_workers=4))  # restore a normal pool for later tests
        executor.shutdown(wait=True)


@pytest.mark.parametrize(("raw", "expected"), [("", _CPU_DEFAULT), ("abc", _CPU_DEFAULT), ("0", _CPU_DEFAULT), ("12", 12)])
def test_default_executor_workers_parses_the_environment(monkeypatch, raw, expected):
    monkeypatch.setenv("THREAD_POOL_WORKERS", raw)
    assert async_runtime.default_executor_workers() == expected


def test_startup_hooks_configure_the_executor():
    import inspect

    from apps.api import main as api_main
    from apps.worker import main as worker_main

    # startup and cron_startup delegate to the shared _boot_runtime, which owns the executor configuration.
    for hook in (worker_main._boot_runtime, worker_main.chat_startup, worker_main.model_startup):
        assert "configure_default_executor()" in inspect.getsource(hook)
    assert "configure_default_executor()" in inspect.getsource(api_main)
    assert "_boot_runtime(" in inspect.getsource(worker_main.startup)
    assert "_boot_runtime(" in inspect.getsource(worker_main.cron_startup)


# ---------------------------------------------------------------- wiring guards (fail if an offload is reverted)
# Timing alone cannot guard small inputs, so these assert the call sites themselves: a spy on the
# module's ``to_thread_joined`` for runnable paths and an AST scan for paths needing heavy fixtures.

def _calls_in_async_functions(module: object, names: set[str]) -> list[str]:
    """Return ``func:name`` for every direct call to ``names`` (bare or attribute) inside an ``async def``."""
    tree = ast.parse(inspect.getsource(module))  # type: ignore[arg-type]
    found: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                target = node.func
                called = target.id if isinstance(target, ast.Name) else target.attr if isinstance(target, ast.Attribute) else ""
                qualified = ast.unparse(target)
                if called in names or qualified in names:
                    found.append(f"{fn.name}:{qualified}")
    return found


async def test_collect_rest_routes_json_and_mapping_through_to_thread_joined(monkeypatch):
    seen: list[object] = []
    real = rest.to_thread_joined

    async def spy(fn, *args):
        seen.append(fn)
        return await real(fn, *args)

    monkeypatch.setattr(rest, "to_thread_joined", spy)
    config = SimpleNamespace(url="https://api.example.com/items", items_path="data.items", id_field="id",
                             title_field="title", content_field="body", updated_field="updated")
    body = json.dumps(_page(5)).encode()

    async def fetch(url, **_kwargs):
        return rest.Fetched(body)

    await rest.collect_rest(config, None, fetch=fetch)
    assert json.loads in seen and rest.map_page in seen


@pytest.mark.parametrize("module", [rest, research, social, google_api, local_services])
def test_provider_async_paths_never_decode_json_inline(module):
    names = {"json.loads", "_json", "_decode_json", "map_page"}
    # The saved Home Assistant cursor (a few bytes of our own state) is parsed in the sync helper
    # ``_ha_resume_index``, so no async provider function decodes JSON inline any more.
    allowed: set[str] = set()
    assert set(_calls_in_async_functions(module, names)) == allowed


def test_collection_runners_only_reach_payload_through_the_async_wrapper():
    """Decoders and mappers of provider bodies must not call the sync ``_payload`` on the loop.

    The remaining direct calls build tiny requests from owner configuration (no provider bytes).
    """
    tree = ast.parse(inspect.getsource(collection))
    bodies = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and ast.unparse(n.func) == "_payload"]
    heavy = [ast.unparse(n.args[0]) for n in bodies if n.args and (
        "map" in ast.unparse(n.args[0]) or "decode_json" in ast.unparse(n.args[0]) or ast.unparse(n.args[0]) == "row.mapper")]
    # Only the lambda wrappers (mappers) and decode_json go through the async wrapper; none may remain sync.
    assert heavy == []


async def test_payload_async_goes_through_to_thread_joined(monkeypatch):
    seen: list[object] = []
    real = collection.to_thread_joined

    async def spy(fn, *args):
        seen.append(fn)
        return await real(fn, *args)

    monkeypatch.setattr(collection, "to_thread_joined", spy)
    assert await collection._payload_async(lambda: 7) == 7
    assert seen == [collection._payload]


def test_collect_web_job_runs_the_crawl_block_through_to_thread_joined():
    """The worker must offload via ``_prepare_crawl_records`` and keep decode/validate/hash off the loop."""
    tree = ast.parse(inspect.getsource(ingestion_worker._collect_web_job))
    offloaded = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and ast.unparse(n.func) == "to_thread_joined"
                 and n.args and ast.unparse(n.args[0]) == "_prepare_crawl_records"]
    assert len(offloaded) == 1
    inline = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not {"response.json", "ConnectorRecord.model_validate", "json.dumps", "hashlib.sha256"} & inline
    assert not any(c.endswith("model_dump") for c in inline)


def test_crawl_collector_offloads_its_large_digests():
    """crawl.py imports the optional crawlee extra, so its wiring is checked from source."""
    source = inspect.getsource(__import__("modules.connectors.crawl_digest", fromlist=["x"]))
    assert "def page_digest_and_raw" in source
    crawl_source = (__import__("pathlib").Path(__file__).parents[2] / "modules/connectors/crawl.py").read_text()
    for helper in ("page_digest_and_raw", "compute_result_digest", "wire_size"):
        assert f"to_thread_joined({helper}" in crawl_source
