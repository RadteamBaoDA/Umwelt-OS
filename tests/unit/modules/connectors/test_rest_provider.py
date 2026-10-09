"""Pure tests for the pinned REST transport and the no-slice-and-advance mapping rules."""

from types import SimpleNamespace

import httpx
import pytest

from modules.connectors.providers import rest
from modules.connectors.public import ProviderRateLimited


def cfg(**kw):
    base = {"url": "https://api.example.com/items", "items_path": "data.items", "id_field": "id",
            "title_field": "title", "content_field": "body", "updated_field": "updated"}
    return SimpleNamespace(**{**base, **kw})


async def public(host, port):
    return ["93.184.216.34"]


def fetcher(handler, sent=None):
    async def fetch(url):
        async def before():
            if sent is not None:
                sent.append(url)
        return await rest.fetch_bounded(
            url, before_send=before, resolve=public, transport=httpx.MockTransport(handler))
    return fetch


def page(n, start=0, next_url=None):
    body = {"data": {"items": [
        {"id": start + i, "title": f"t{start + i}", "body": "b", "updated": f"2026-01-{(i % 28) + 1:02d}T00:00:00Z"}
        for i in range(n)]}}
    if next_url:
        body["next_url"] = next_url
    return body


@pytest.mark.asyncio
async def test_connection_is_pinned_to_validated_ip_with_host_and_sni():
    seen = {}

    def handler(request):
        seen["url"], seen["host"], seen["sni"] = str(request.url), request.headers["host"], request.extensions.get("sni_hostname")
        return httpx.Response(200, json={})

    await rest.fetch_bounded("https://api.example.com/x", before_send=_noop, resolve=public,
                             transport=httpx.MockTransport(handler))
    assert seen == {"url": "https://93.184.216.34/x", "host": "api.example.com", "sni": "api.example.com"}


async def _noop():
    return None


@pytest.mark.asyncio
async def test_any_private_resolution_is_refused_before_send():
    async def mixed(host, port):
        return ["93.184.216.34", "10.0.0.5"]

    sent = []

    async def before():
        sent.append(1)

    with pytest.raises(rest.UnsafeDestination):
        await rest.fetch_bounded("https://a.example/x", before_send=before, resolve=mixed,
                                 transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    assert sent == []


@pytest.mark.asyncio
async def test_redirect_is_not_followed_and_429_keeps_full_retry_after():
    with pytest.raises(rest.UnsafeDestination):
        await rest.fetch_bounded("https://a.example/x", before_send=_noop, resolve=public,
                                 transport=httpx.MockTransport(lambda r: httpx.Response(302, headers={"location": "http://127.0.0.1/"})))
    with pytest.raises(ProviderRateLimited) as exc:
        await rest.fetch_bounded("https://a.example/x", before_send=_noop, resolve=public,
                                 transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={"retry-after": "7200"})))
    assert (exc.value.next_eligible_at - __import__("datetime").datetime.now(__import__("datetime").UTC)).total_seconds() > 3600


@pytest.mark.asyncio
async def test_decoded_body_cap_and_advertised_length_cap():
    big = httpx.Response(200, content=b"x" * 2000)
    with pytest.raises(rest.CollectionIncomplete):
        await rest.fetch_bounded("https://a.example/x", before_send=_noop, resolve=public, max_bytes=1000,
                                 transport=httpx.MockTransport(lambda r: big))
    with pytest.raises(rest.CollectionIncomplete):
        await rest.fetch_bounded("https://a.example/x", before_send=_noop, resolve=public, max_bytes=1000,
                                 transport=httpx.MockTransport(lambda r: httpx.Response(200, headers={"content-length": "5000"}, content=b"x")))


@pytest.mark.asyncio
async def test_five_hundred_records_pass_but_the_501st_is_incomplete():
    ok = await rest.collect_rest(cfg(), None, fetch=fetcher(lambda r: httpx.Response(200, json=page(500))))
    assert len(ok.records) == 500 and ok.cursor_after is not None
    with pytest.raises(rest.CollectionIncomplete):
        await rest.collect_rest(cfg(), None, fetch=fetcher(lambda r: httpx.Response(200, json=page(501))))


@pytest.mark.asyncio
async def test_page_cap_with_more_pages_yields_an_exact_continuation_and_keeps_the_cursor():
    def handler(request):
        n = int(request.url.params.get("p", "0"))
        return httpx.Response(200, json=page(1, n, f"/items?p={n + 1}"))

    partial = await rest.collect_rest(cfg(), "2025-12-01T00:00:00+00:00", fetch=fetcher(handler))
    assert partial.pages == 10 and len(partial.records) == 10
    assert partial.continuation_url == "https://api.example.com/items?p=10"
    assert partial.cursor_after == "2025-12-01T00:00:00+00:00"  # never advanced on a partial walk
    # exactly ten pages with no continuation is complete
    def last(request):
        n = int(request.url.params.get("p", "0"))
        return httpx.Response(200, json=page(1, n, f"/items?p={n + 1}" if n < 9 else None))

    done = await rest.collect_rest(cfg(), None, fetch=fetcher(last))
    assert done.pages == 10 and len(done.records) == 10


@pytest.mark.asyncio
async def test_cross_origin_and_looping_next_urls_are_rejected():
    for nxt, error in (("https://evil.example/items", rest.UnsafeDestination), ("/items", rest.UnsafeDestination)):
        with pytest.raises(error):
            await rest.collect_rest(cfg(), None, fetch=fetcher(lambda r, n=nxt: httpx.Response(200, json=page(1, 0, n))))


@pytest.mark.asyncio
async def test_non_json_and_wrong_shape_are_schema_changes():
    with pytest.raises(rest.RestSchemaChanged):
        await rest.collect_rest(cfg(), None, fetch=fetcher(lambda r: httpx.Response(200, content=b"<html>")))
    with pytest.raises(rest.RestSchemaChanged):
        await rest.collect_rest(cfg(), None, fetch=fetcher(lambda r: httpx.Response(200, json={"data": {"items": {}}})))


@pytest.mark.asyncio
async def test_overlap_floor_keeps_untimed_rows_and_cursor_is_unchanged_when_nothing_new():
    body = {"data": {"items": [{"id": 1, "updated": "2026-01-01T00:00:00Z"}, {"id": 2}]}}
    got = await rest.collect_rest(cfg(), "2026-03-01T00:00:00+00:00", fetch=fetcher(lambda r: httpx.Response(200, json=body)))
    assert [r["provider_id"] for r in got.records] == ["2"]
    assert got.cursor_after == "2026-03-01T00:00:00+00:00"


@pytest.mark.asyncio
async def test_each_page_is_one_gated_send():
    sent: list[str] = []

    def handler(request):
        return httpx.Response(200, json=page(1, 0, "/items?p=1") if request.url.params.get("p") is None else page(1, 1))

    out = await rest.collect_rest(cfg(), None, fetch=fetcher(handler, sent))
    assert out.pages == len(sent) == 2


@pytest.mark.asyncio
async def test_resume_continues_exactly_without_duplicates_or_gaps():
    def handler(request):
        n = int(request.url.params.get("p", "0"))
        # page n holds ids 2n, 2n+1; the feed shifted so page 3 repeats the last id of page 2
        body = page(2, 2 * n if n < 3 else 2 * n - 1, f"/items?p={n + 1}" if n < 4 else None)
        return httpx.Response(200, json=body)

    def capped(request):
        return handler(request)

    old_max = rest.MAX_PAGES
    rest.MAX_PAGES = 3
    try:
        first = await rest.collect_rest(cfg(), None, fetch=fetcher(capped))
        assert first.continuation_url.endswith("p=3")
        cp = rest.Checkpoint(url=first.continuation_url, revision=1, cursor=None, max_time=first.max_time,
                             ids=[rest.id_hash(r["provider_id"]) for r in first.records])
        rt = rest.Checkpoint.decode(cp.encode(), revision=1, cursor=None, origin_url="https://api.example.com/items")
        second = await rest.collect_rest(cfg(), None, fetch=fetcher(capped), resume=rt)
    finally:
        rest.MAX_PAGES = old_max
    ids = [r["provider_id"] for r in first.records + second.records]
    assert second.continuation_url is None
    assert len(ids) == len(set(ids))  # shifted boundary record is not delivered twice
    assert set(ids) == {str(i) for i in range(9)}  # and nothing is skipped


@pytest.mark.asyncio
async def test_checkpoint_is_rejected_when_revision_cursor_or_origin_differ():
    cp = rest.Checkpoint(url="https://api.example.com/items?p=3", revision=2, cursor="c")
    raw = cp.encode()
    ok = {"revision": 2, "cursor": "c", "origin_url": "https://api.example.com/items"}
    assert rest.Checkpoint.decode(raw, **ok) is not None
    assert rest.Checkpoint.decode(raw, **{**ok, "revision": 3}) is None
    assert rest.Checkpoint.decode(raw, **{**ok, "cursor": "d"}) is None
    assert rest.Checkpoint.decode(raw, **{**ok, "origin_url": "https://other.example/items"}) is None
    assert rest.Checkpoint.decode("not json", **ok) is None


@pytest.mark.asyncio
async def test_conditional_headers_only_on_a_fresh_first_page_and_304_is_not_modified():
    seen = []

    def handler(request):
        seen.append(request.headers.get("if-none-match"))
        return httpx.Response(304)

    async def fetch(url, headers=None):
        async def before():
            return None
        return await rest.fetch_bounded(
            url, headers=headers, before_send=before, resolve=public, transport=httpx.MockTransport(handler))

    got = await rest.collect_rest(cfg(), "2026-01-01T00:00:00+00:00", fetch=fetch, conditional={"If-None-Match": '"v1"'})
    assert got.not_modified and got.records == [] and got.cursor_after == "2026-01-01T00:00:00+00:00"
    assert seen == ['"v1"']


@pytest.mark.asyncio
async def test_validators_come_from_the_first_page_response():
    def handler(request):
        return httpx.Response(200, json=page(1), headers={"etag": '"v2"', "last-modified": "Wed, 01 Jan 2026 00:00:00 GMT"})

    got = await rest.collect_rest(cfg(), None, fetch=fetcher(handler))
    assert got.etag == '"v2"' and got.last_modified.startswith("Wed")
