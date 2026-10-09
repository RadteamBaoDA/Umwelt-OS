"""Native web + MCP adapters and the gzip-bomb cap (fakes only, no network, no database)."""

import gzip
import zlib
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from modules.connectors import backends, collection
from modules.connectors import mcp as mcp_collection
from modules.connectors.providers import rest


async def public(host, port):
    return ["93.184.216.34"]


async def _noop():
    return None


def source(type_, provider=None, configuration=None):
    return SimpleNamespace(id=uuid4(), type=type_, provider=provider, generation=1, status="active",
                           configuration=configuration or {})


@pytest.mark.parametrize("type_", sorted(backends.GENERIC_SOURCE_TYPES))
def test_dispatch_and_executor_agree_for_every_generic_type(type_):
    cfg = {"url": "https://a.example/"} if type_ == "web" else {}
    assert backends.native_dispatch_supported(type_, None)
    assert collection.supports_native(source(type_, configuration=cfg))


def test_browser_rendered_web_is_not_native():
    assert not collection.supports_native(source("web", configuration={"url": "https://a.example/", "js_render": True}))


# ---- gzip bomb cap

@pytest.mark.asyncio
@pytest.mark.parametrize("compress", [gzip.compress, zlib.compress])
async def test_compressed_bomb_is_cut_at_the_decompressed_cap(compress):
    bomb = compress(b"\0" * 50_000_000)  # tiny on the wire, huge inflated
    assert len(bomb) < 100_000
    transport = httpx.MockTransport(lambda r: httpx.Response(
        200, stream=httpx.ByteStream(bomb), headers={"content-encoding": "gzip" if compress is gzip.compress else "deflate"}))
    with pytest.raises(rest.CollectionIncomplete):
        await rest.fetch_bounded("https://a.example/x", before_send=_noop, resolve=public,
                                 max_bytes=1_000_000, transport=transport)


@pytest.mark.asyncio
async def test_small_gzip_body_is_decoded_and_unknown_encoding_refused():
    ok = httpx.MockTransport(lambda r: httpx.Response(
        200, stream=httpx.ByteStream(gzip.compress(b'{"a":1}')), headers={"content-encoding": "gzip"}))
    got = await rest.fetch_bounded("https://a.example/x", before_send=_noop, resolve=public, transport=ok)
    assert got.body == b'{"a":1}'
    bad = httpx.MockTransport(lambda r: httpx.Response(
        200, stream=httpx.ByteStream(b"x"), headers={"content-encoding": "br"}))
    with pytest.raises(rest.ProviderHttpError):
        await rest.fetch_bounded("https://a.example/x", before_send=_noop, resolve=public, transport=bad)


# ---- web adapter

class FakeGate:
    def __init__(self, pages, calls):
        self.pages, self.calls = pages, calls

    async def fetch_bytes(self, url, **kwargs):
        self.calls.append(url)
        if url.split("//")[1].split("/")[0] != "a.example":
            raise AssertionError("cross-origin fetch")
        return rest.Fetched(self.pages[url].encode())

    async def __call__(self, *_a):
        self.calls.append("gate")


def web_run(gate, config):
    src = source("web", configuration=config)
    return SimpleNamespace(attempt=SimpleNamespace(source=src), gate=gate)


@pytest.mark.asyncio
async def test_web_adapter_stays_same_origin_bounded_and_hands_records_to_accept(monkeypatch):
    pages = {
        "https://a.example/": '<p>home</p><script>x()</script><a href="/b#f">b</a><a href="https://evil.test/">e</a>',
        "https://a.example/b": "<p>bee</p><a href='/c'>c</a>",
        "https://a.example/c": "<p>cee</p>",
    }
    calls, accepted = [], []

    async def state(run):
        return SimpleNamespace(cursor=None)

    async def accept(run, records, before, after, update=None):
        accepted.append(records)

    monkeypatch.setattr(collection, "_state", state)
    monkeypatch.setattr(collection, "_accept_generic", accept)
    run = web_run(FakeGate(pages, calls), {"url": "https://a.example/", "max_pages": 10, "max_depth": 1})
    await collection._run_web(run)
    assert calls == ["https://a.example/", "https://a.example/b"]  # depth 1 only, evil.test never fetched
    assert [r["content"] for r in accepted[0]] == ["home"+chr(10)+"b"+chr(10)+"e", "bee"+chr(10)+"c"]


# ---- MCP adapter

@pytest.mark.asyncio
async def test_mcp_adapter_gates_each_call_and_ingests_normalized_records(monkeypatch):
    from modules.tools import public as tools

    grant, conn = uuid4(), uuid4()
    src = source("mcp", configuration={"connection_id": str(conn), "calls": [{"grant_id": str(grant)}]})
    gate_calls, accepted = [], []

    async def gate(*_a):
        gate_calls.append(1)

    async def read_capability(runtime, *, authorize_extra, **kw):
        assert await authorize_extra() is True
        return SimpleNamespace()

    async def state(run):
        return SimpleNamespace(cursor=None)

    async def accept(run, records, before, after, update=None):
        accepted.append((records, after))

    monkeypatch.setattr(tools, "read_collection_capability", read_capability)
    monkeypatch.setattr(mcp_collection, "normalize", lambda read, args, at: [{"provider_id": "p"}])
    monkeypatch.setattr(collection, "_state", state)
    monkeypatch.setattr(collection, "_accept_generic", accept)
    run = SimpleNamespace(attempt=SimpleNamespace(source=src, scope=None, multi=False), gate=gate,
                          ctx={"agent_mcp_runtime": object()})
    await collection._run_mcp(run)
    assert gate_calls == [1] and accepted[0][0] == [{"provider_id": "p"}] and accepted[0][1].startswith("mcp:")


@pytest.mark.asyncio
async def test_mcp_adapter_fails_without_runtime_or_when_every_call_fails(monkeypatch):
    from modules.tools import public as tools

    src = source("mcp", configuration={"connection_id": str(uuid4()), "calls": [{"grant_id": str(uuid4())}]})
    run = SimpleNamespace(attempt=SimpleNamespace(source=src, scope=None, multi=False), gate=None, ctx={})
    with pytest.raises(mcp_collection.McpCollectionError):
        await collection._run_mcp(run)

    async def boom(*_a, **_k):
        raise RuntimeError("down")

    async def state(run):
        return SimpleNamespace(cursor=None)

    monkeypatch.setattr(tools, "read_collection_capability", boom)
    monkeypatch.setattr(collection, "_state", state)
    run.ctx = {"agent_mcp_runtime": object()}
    with pytest.raises(mcp_collection.McpCollectionError):
        await collection._run_mcp(run)
