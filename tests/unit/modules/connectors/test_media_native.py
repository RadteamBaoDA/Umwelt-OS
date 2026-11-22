import json
from datetime import UTC, datetime
from hashlib import sha1
from urllib.parse import parse_qs, urlsplit

import pytest

from modules.connectors.providers import rest
from modules.connectors.providers.media_native import collect_podcast_index, collect_youtube_api
from modules.connectors.service.schemas import ConnectorConfig

NOW = datetime(2026, 11, 18, tzinfo=UTC)
CHANNEL = "UC" + "a" * 22
UPLOADS = "UU" + "a" * 22
VIDEO = "v" * 11


@pytest.mark.asyncio
async def test_youtube_handle_then_uploads_then_metadata_and_conditional_snapshot():
    calls = []
    payloads = iter(
        [
            rest.Fetched(
                json.dumps(
                    {
                        "items": [
                            {
                                "id": CHANNEL,
                                "snippet": {"country": "VN"},
                                "contentDetails": {"relatedPlaylists": {"uploads": UPLOADS}},
                            }
                        ]
                    }
                ).encode()
            ),
            rest.Fetched(
                json.dumps({"items": [{"contentDetails": {"videoId": VIDEO}}]}).encode(),
                etag='"uploads"',
            ),
            rest.Fetched(
                json.dumps(
                    {
                        "items": [
                            {
                                "id": VIDEO,
                                "snippet": {
                                    "channelId": CHANNEL,
                                    "title": "Example",
                                    "defaultAudioLanguage": "vi",
                                    "publishedAt": NOW.isoformat(),
                                },
                                "contentDetails": {"duration": "PT1M3S"},
                            }
                        ]
                    }
                ).encode()
            ),
        ]
    )

    async def fetch(url, **kwargs):
        calls.append((urlsplit(url), kwargs))
        return next(payloads)

    cfg = ConnectorConfig(youtube_handles=("@example",))
    page = await collect_youtube_api(cfg, "mock-key", NOW, fetch, None)
    assert [url.path.rsplit("/", 1)[-1] for url, _ in calls] == [
        "channels",
        "playlistItems",
        "videos",
    ]
    assert parse_qs(calls[0][0].query)["forHandle"] == ["@example"]
    assert all("mock-key" not in url.geturl() and not kw["allow_redirects"] for url, kw in calls)
    assert all(kw["headers"]["X-Goog-Api-Key"] == "mock-key" for _, kw in calls)
    assert page.records[0].provider_id == f"youtube:{CHANNEL}:{VIDEO}"
    fields = page.records[0].metadata["provider_record"]["source_fields"]
    assert (fields["duration_seconds"], fields["country"], fields["language"]) == (63, "VN", "vi")

    async def conditional(url, **kwargs):
        if urlsplit(url).path.endswith("/channels"):
            return rest.Fetched(
                json.dumps(
                    {
                        "items": [
                            {
                                "id": CHANNEL,
                                "contentDetails": {"relatedPlaylists": {"uploads": UPLOADS}},
                            }
                        ]
                    }
                ).encode()
            )
        assert urlsplit(url).path.endswith("/playlistItems")
        assert kwargs["headers"]["If-None-Match"] == '"uploads"'
        return rest.Fetched(None)

    unchanged = await collect_youtube_api(cfg, "mock-key", NOW, conditional, page.cursor_after)
    assert not unchanged.records
    assert unchanged.cursor_after == page.cursor_after


@pytest.mark.asyncio
async def test_podcast_index_sha1_current_clock_and_recent_query():
    async def fetch(url, **kwargs):
        parsed = urlsplit(url)
        assert parsed.path == "/api/1.0/recent/feeds"
        assert parse_qs(parsed.query) == {
            "max": ["50"],
            "lang": ["vi,unknown"],
            "cat": ["102"],
            "notcat": ["sports"],
            "since": ["100"],
        }
        stamp = str(int(NOW.timestamp()))
        assert kwargs["headers"]["X-Auth-Date"] == stamp
        assert kwargs["headers"]["X-Auth-Key"] == "mock-key"
        assert (
            kwargs["headers"]["Authorization"]
            == sha1(f"mock-keymock-secret{stamp}".encode(), usedforsecurity=False).hexdigest()
        )
        assert "mock-" not in url
        assert kwargs["allow_redirects"] is False
        return rest.Fetched(b'{"status":"true","feeds":[]}')

    cfg = ConnectorConfig(
        podcast_index_languages=("vi", "unknown"),
        podcast_index_categories="102",
        podcast_index_excluded_categories="sports",
        podcast_index_since=100,
    )
    page = await collect_podcast_index(
        cfg, "mock-key", "mock-secret", NOW, fetch, None, clock=lambda: NOW
    )
    assert not page.records


@pytest.mark.asyncio
async def test_podcast_index_clock_skew_propagates_redacted_auth_error():
    async def fetch(_url, **_kwargs):
        raise rest.ProviderHttpError(401)

    with pytest.raises(rest.ProviderHttpError, match="^http_401$"):
        await collect_podcast_index(ConnectorConfig(), "mock-key", "mock-secret", NOW, fetch, None)


@pytest.mark.asyncio
async def test_youtube_resolves_no_more_than_two_pages_per_target():
    from modules.connectors.providers.media_native import _YOUTUBE_PAGE_SIZE

    sends = []

    async def fetch(url, **kwargs):
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        sends.append((parsed.path.rsplit("/", 1)[-1], query))
        assert kwargs["allow_redirects"] is False
        if parsed.path.endswith("/playlistItems"):
            page = 1 if "pageToken" in query else 0
            assert query["maxResults"] == [str(_YOUTUBE_PAGE_SIZE)]
            return rest.Fetched(
                json.dumps(
                    {
                        "items": [{"contentDetails": {"videoId": VIDEO}}],
                        "nextPageToken": f"page{page}",
                    }
                ).encode(),
                etag='"current"',
            )
        assert query["id"] == [VIDEO]
        return rest.Fetched(
            json.dumps(
                {"items": [{"id": VIDEO, "snippet": {"channelId": CHANNEL, "title": "Example"}}]}
            ).encode()
        )

    result = await collect_youtube_api(
        ConnectorConfig(youtube_playlist_ids=("PL0123456789",)), "key", NOW, fetch, None
    )
    assert [path for path, _ in sends] == ["playlistItems", "videos", "playlistItems", "videos"]
    assert result.coverage == "truncated"
    assert len(result.records) == 1


@pytest.mark.asyncio
async def test_podcast_value_search_uses_documented_route_and_maps_canonical_metadata():
    async def fetch(url, **_kwargs):
        parsed = urlsplit(url)
        assert parsed.path == "/api/1.0/search/byterm"
        assert parse_qs(parsed.query) == {
            "max": ["50"],
            "q": ["Culture and history"],
            "val": ["lightning"],
        }
        return rest.Fetched(
            json.dumps(
                {
                    "status": "true",
                    "feeds": [
                        {
                            "id": 17,
                            "title": "A podcast",
                            "language": "en-US",
                            "explicit": 1,
                            "categories": {"54": "History"},
                        }
                    ],
                }
            ).encode()
        )

    page = await collect_podcast_index(
        ConnectorConfig(podcast_index_query="Culture and history", podcast_index_value="lightning"),
        "key",
        "secret",
        NOW,
        fetch,
        None,
    )
    (record,) = page.records
    fields = record.metadata["provider_record"]["source_fields"]
    assert fields["language"] == "en-us"
    assert fields["genres"] == ["history"]
    assert fields["content_rating"] == "explicit"
    assert fields["external_ids"] == {"podcastindex": "17"}


@pytest.mark.asyncio
async def test_podcast_cursor_advances_only_for_complete_page_and_restarts_for_query_or_filter_change():
    seen = []
    response_size = 1
    now_stamp = int(NOW.timestamp())

    async def fetch(url, **_kwargs):
        seen.append(parse_qs(urlsplit(url).query))
        feeds = [
            {"id": i + 1, "title": "A podcast", "newestItemPublishTime": now_stamp - i}
            for i in range(response_size)
        ]
        return rest.Fetched(json.dumps({"status": "true", "feeds": feeds}).encode())

    cfg = ConnectorConfig(podcast_index_languages=("en",))
    first = await collect_podcast_index(cfg, "key", "secret", NOW, fetch, None)
    assert json.loads(first.cursor_after)["since"] == now_stamp - 1
    response_size = 50
    capped = await collect_podcast_index(cfg, "key", "secret", NOW, fetch, first.cursor_after)
    assert capped.coverage == "truncated"
    assert capped.cursor_after == first.cursor_after
    assert seen[-1]["since"] == [str(now_stamp - 1)]
    await collect_podcast_index(
        ConnectorConfig(podcast_index_languages=("vi",)),
        "key",
        "secret",
        NOW,
        fetch,
        capped.cursor_after,
    )
    assert "since" not in seen[-1]
    await collect_podcast_index(
        ConnectorConfig(podcast_index_query="different target"),
        "key",
        "secret",
        NOW,
        fetch,
        capped.cursor_after,
    )
    assert "since" not in seen[-1]


@pytest.mark.asyncio
async def test_media_native_runner_fences_all_sends_and_publishes_cursor_only_on_success(
    monkeypatch,
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from modules.connectors._collection import native

    cfg = {"youtube_playlist_ids": ["PL0123456789"]}
    call_count = 0
    fail_details = True

    async def fetch(url, **_kwargs):
        nonlocal call_count
        call_count += 1
        if urlsplit(url).path.endswith("/playlistItems"):
            return rest.Fetched(
                json.dumps({"items": [{"contentDetails": {"videoId": VIDEO}}]}).encode(),
                etag='"current"',
            )
        if fail_details:
            raise rest.ProviderHttpError(503)
        return rest.Fetched(
            json.dumps(
                {"items": [{"id": VIDEO, "snippet": {"channelId": CHANNEL, "title": "Example"}}]}
            ).encode()
        )

    class Gate:
        def bind_credential(self, _fingerprint):
            pass

        fetch_bytes = staticmethod(fetch)

    gate = Gate()
    source = SimpleNamespace(provider="youtube_api", configuration=cfg)
    run = SimpleNamespace(
        attempt=SimpleNamespace(source=source, credential_revision=7),
        gate=gate,
        settings=SimpleNamespace(
            connector_credential_encryption_key=SimpleNamespace(
                get_secret_value=lambda: "encryption"
            )
        ),
    )
    lease = SimpleNamespace(cursor_before=None)
    call = native.NativeCall(run, lease, NOW, None)
    accept = AsyncMock()
    monkeypatch.setattr(native, "_rest_secret", AsyncMock(return_value=("Authorization", "key")))
    monkeypatch.setattr(native, "_accept_native", accept)

    with pytest.raises(rest.ProviderHttpError, match="http_503"):
        await native._native_keyed_media(call)
    accept.assert_not_awaited()
    assert call_count == 2

    fail_details = False
    await native._native_keyed_media(call)
    accept.assert_awaited_once()
    assert accept.await_args.args[1] is lease
    assert accept.await_args.args[2][0].provider_id == f"youtube:{CHANNEL}:{VIDEO}"
    assert accept.await_args.kwargs["provider_credential_revision"] == 7
    assert any(
        validator["etag"] == '"current"'
        for validator in json.loads(accept.await_args.kwargs["cursor_after"])["etags"].values()
    )


def test_native_media_quota_uses_pacific_midnight_and_dashboard_shapes():
    from modules.connectors.catalog import get_catalog_entry
    from modules.connectors.provider_specs import get_quota_policies
    from modules.connectors.quota import window_bounds

    (policy,) = get_quota_policies("youtube_api")
    assert (policy.limit_units, policy.cost_per_send) == (10_000, 1)
    assert window_bounds(policy.window, datetime(2026, 3, 8, 18, tzinfo=UTC)) == (
        datetime(2026, 3, 8, 8, tzinfo=UTC),
        datetime(2026, 3, 9, 7, tzinfo=UTC),
    )
    youtube = get_catalog_entry("youtube_api")
    podcast = get_catalog_entry("podcast_index")
    assert set(youtube.data_shapes) >= {"media", "video"}
    assert set(podcast.data_shapes) >= {"media"}
    assert podcast.key_fields == ("api_key", "api_secret")


@pytest.mark.asyncio
async def test_youtube_quota_forbidden_is_rate_limited_and_credential_never_redirects():
    import httpx

    from modules.connectors.public import ProviderRateLimited

    authorized = 0
    status = 403

    async def before_send():
        nonlocal authorized
        authorized += 1

    async def resolve(_host, _port):
        return ["93.184.216.34"]

    def handler(request):
        assert request.headers["X-Goog-Api-Key"] == "mock-key"
        assert "mock-key" not in str(request.url)
        if status == 403:
            return httpx.Response(
                403,
                json={
                    "error": {"errors": [{"reason": "quotaExceeded", "message": "mock-key-echo"}]}
                },
            )
        return httpx.Response(302, headers={"location": "https://unrelated.example.org/endpoint"})

    async def fetch(url, **kwargs):
        return await rest.fetch_bounded(
            url,
            before_send=before_send,
            resolve=resolve,
            transport=httpx.MockTransport(handler),
            **kwargs,
        )

    config = ConnectorConfig(youtube_playlist_ids=("PL0123456789",))
    with pytest.raises(ProviderRateLimited) as caught:
        await collect_youtube_api(config, "mock-key", NOW, fetch, None)
    assert "mock-key" not in str(caught.value)
    assert authorized == 1

    status = 302
    with pytest.raises(rest.UnsafeDestination, match="redirect_blocked"):
        await collect_youtube_api(config, "mock-key", NOW, fetch, None)
    assert authorized == 2


@pytest.mark.parametrize(
    ("provider", "options"),
    [
        ("youtube_api", {"youtube_playlist_ids": ["PL0123456789"], "media_countries": ["VN"]}),
        ("podcast_index", {"content_ratings": ["explicit"]}),
    ],
)
def test_native_media_scope_refuses_filters_with_no_upstream_fields(provider, options):
    from modules.connectors.provider_options import validate_options

    with pytest.raises(ValueError):
        validate_options(provider, options)


@pytest.mark.asyncio
async def test_youtube_continues_capped_playlist_before_using_head_validator():
    sends = []

    async def fetch(url, **kwargs):
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        headers = kwargs["headers"]
        if parsed.path.endswith("/videos"):
            video = query["id"][0]
            return rest.Fetched(
                json.dumps(
                    {"items": [{"id": video, "snippet": {"channelId": CHANNEL, "title": video}}]}
                ).encode()
            )
        token = query.get("pageToken", [None])[0]
        sends.append((token, headers.get("If-None-Match")))
        if headers.get("If-None-Match"):
            return rest.Fetched(None)
        page = {None: 0, "next1": 1, "next2": 2}[token]
        body = {"items": [{"contentDetails": {"videoId": f"{page:011d}"}}]}
        if page < 2:
            body["nextPageToken"] = f"next{page + 1}"
        return rest.Fetched(json.dumps(body).encode(), etag='"head"' if page == 0 else '"page"')

    config = ConnectorConfig(youtube_playlist_ids=("PL0123456789",))
    first = await collect_youtube_api(config, "mock-key", NOW, fetch, None)
    assert first.coverage == "truncated" and len(first.records) == 2
    second = await collect_youtube_api(config, "mock-key", NOW, fetch, first.cursor_after)
    assert sends == [(None, None), ("next1", None), ("next2", None)]
    assert second.coverage == "returned_snapshot"
    assert [r.provider_id for r in second.records] == [f"youtube:{CHANNEL}:00000000002"]
    unchanged = await collect_youtube_api(config, "mock-key", NOW, fetch, second.cursor_after)
    assert sends[-1] == (None, '"head"')
    assert not unchanged.records and unchanged.cursor_after == second.cursor_after

    legacy = json.loads(second.cursor_after)
    (route,) = legacy["etags"]
    legacy["etags"][route]["truncated"] = True
    restarted = await collect_youtube_api(config, "mock-key", NOW, fetch, json.dumps(legacy))
    assert sends[-2:] == [(None, None), ("next1", None)]
    assert restarted.coverage == "truncated"
    assert json.loads(restarted.cursor_after)["etags"][route]["page_token"] == "next2"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "reason", "recovers"),
    [(400, "invalidPageToken", True), (400, "badRequest", False), (503, "backendError", False)],
)
async def test_youtube_only_restarts_for_the_rejected_saved_page_token(status, reason, recovers):
    import httpx

    requests = []
    restart = False

    async def gate():
        return None

    async def resolve(_host, _port):
        return ["93.184.216.34"]

    def respond(request):
        params = request.url.params
        token = params.get("pageToken")
        path = request.url.path.rsplit("/", 1)[-1]
        requests.append((path, token, request.headers.get("If-None-Match")))
        if path == "videos":
            video = params["id"]
            return httpx.Response(
                200,
                json={"items": [{"id": video, "snippet": {"channelId": CHANNEL, "title": video}}]},
            )
        if restart and token == "stale":
            return httpx.Response(status, json={"error": {"errors": [{"reason": reason}]}})
        index = int(token == "second") + (10 if restart else 0)
        item = {"contentDetails": {"videoId": f"{index:011d}"}}
        page = {"items": [item]}
        if not restart:
            page["nextPageToken"] = "stale" if token == "second" else "second"
        return httpx.Response(200, json=page, headers={"ETag": '"new"' if restart else '"old"'})

    async def fetch(url, **kwargs):
        return await rest.fetch_bounded(
            url, before_send=gate, resolve=resolve, transport=httpx.MockTransport(respond), **kwargs
        )

    cfg = ConnectorConfig(youtube_playlist_ids=("PL0123456789",))
    first = await collect_youtube_api(cfg, "mock-key", NOW, fetch, None)
    assert first.coverage == "truncated"
    restart = True
    requests.clear()
    if recovers:
        page = await collect_youtube_api(cfg, "mock-key", NOW, fetch, first.cursor_after)
        assert requests == [
            ("playlistItems", "stale", None),
            ("playlistItems", None, None),
            ("videos", None, None),
        ]
        assert [r.provider_id for r in page.records] == [f"youtube:{CHANNEL}:00000000010"]
        assert page.coverage == "returned_snapshot"
        (saved,) = json.loads(page.cursor_after)["etags"].values()
        assert saved == {"etag": '"new"'}
    else:
        with pytest.raises(rest.ProviderHttpError) as caught:
            await collect_youtube_api(cfg, "mock-key", NOW, fetch, first.cursor_after)
        assert caught.value.status_code == status
        assert requests == [("playlistItems", "stale", None)]
        assert "stale" in first.cursor_after
