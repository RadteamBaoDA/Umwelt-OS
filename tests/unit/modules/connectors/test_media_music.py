"""Fixture-based ListenBrainz, MusicBrainz and Cover Art Archive contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from modules.connectors.backends import PROVIDER_SCOPE_FIELDS
from modules.connectors.provider_options import native_fingerprint
from modules.connectors.providers import media_music
from modules.connectors.providers.media_music import (
    build_listenbrainz_url,
    build_musicbrainz_search_url,
    collect_listenbrainz_releases,
    normalize_musicbrainz_language,
)
from modules.connectors.providers.rest import Fetched, ProviderHttpError


def test_listenbrainz_url_only_uses_its_documented_scope_filters():
    url = build_listenbrainz_url(
        {
            "listenbrainz_days": 45,
            "listenbrainz_sort": "release_date",
            "listenbrainz_username": "owner",
            "media_countries": ("US",),
            "media_genres": ("music",),
            "media_languages": ("en",),
        }
    )
    parts = urlsplit(url)
    # Per-user endpoint: user_name must never reach the sitewide explore endpoint.
    assert parts.path == "/1/user/owner/fresh_releases"
    assert parse_qs(parts.query) == {"past": ["true"], "future": ["true"]}
    sitewide = urlsplit(build_listenbrainz_url({"listenbrainz_days": 45}))
    assert sitewide.path == "/1/explore/fresh-releases/"
    assert parse_qs(sitewide.query) == {"days": ["45"], "sort": ["release_date"]}
    with pytest.raises(ValueError):
        build_listenbrainz_url({"listenbrainz_username": "../x"})


@pytest.mark.asyncio
async def test_cover_url_accepts_archive_subdomains_and_rejects_lookalikes():
    from modules.connectors.providers.media_music import _cover_url

    async def final(url: str):
        async def fetch(*_a, **_k) -> Fetched:
            return Fetched(b"x", final_url=url)

        return await _cover_url("f3a5b6b7-62ec-4be7-bfc8-62ac2c2ea3d9", "ua", fetch)

    assert await final("https://ia800000.us.archive.org/1/items/mbid/front-500.jpg")
    assert await final("https://evilarchive.org/x.jpg") is None
    assert await final("https://archive.org.evil.example/x.jpg") is None


def test_listenbrainz_filter_changes_reset_query_fingerprint_without_changing_target_scope():
    original = {"listenbrainz_days": 14, "listenbrainz_tags": ("indie",)}
    changed = {"listenbrainz_days": 30, "listenbrainz_tags": ("indie",)}
    assert native_fingerprint("listenbrainz_releases", original) != native_fingerprint(
        "listenbrainz_releases", changed
    )
    assert PROVIDER_SCOPE_FIELDS["listenbrainz_releases"] == frozenset({"listenbrainz_username"})


def test_musicbrainz_lucene_query_maps_supported_filters_and_three_letter_language():
    url = build_musicbrainz_search_url(
        "f3a5b6b7-62ec-4be7-bfc8-62ac2c2ea3d9",
        {
            "media_countries": ("US",),
            "media_languages": ("en",),
            "media_formats": ("album",),
            "release_year": 2025,
            "listenbrainz_tags": ("indie rock",),
            "listenbrainz_status": "official",
        },
    )
    query = parse_qs(urlsplit(url).query)["query"][0]
    assert "rgid:f3a5b6b7-62ec-4be7-bfc8-62ac2c2ea3d9" in query
    assert 'country:"US"' in query and 'lang:"eng"' in query
    assert 'primarytype:"Album"' in query and "date:2025" in query
    assert 'tag:"indie rock"' in query and "status:official" in query


@pytest.mark.parametrize(
    ("raw", "expected"), [("eng", "en"), ("jpn", "ja"), ("fre", "fr"), ("vie", "vi")]
)
def test_musicbrainz_language_uses_iso_639_2_b_to_canonical_iso_639_1(raw, expected):
    assert normalize_musicbrainz_language(raw) == expected


@pytest.mark.asyncio
async def test_musicbrainz_pacer_waits_a_full_second_between_dispatches(monkeypatch):
    import modules.connectors.providers._media_common as media_common

    now = [10.0]

    async def advance(delay: float) -> None:
        now[0] += delay

    monkeypatch.setattr(media_common.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(media_common.asyncio, "sleep", advance)
    pacer = media_common.MediaRatePacer()
    assert await pacer.pace("musicbrainz.org", 1.0) == 0.0
    assert await pacer.pace("musicbrainz.org", 1.0) == 1.0
    assert await pacer.pace("musicbrainz.org", 1.0) == 1.0


@pytest.mark.asyncio
async def test_musicbrainz_requests_are_spaced_and_cover_redirect_is_published_safely(monkeypatch):
    import modules.connectors.providers.media_music as media_music

    sleeps: list[float] = []
    original_pace = media_music.PACER.pace

    async def record_pace(key: str, interval: float) -> float:
        assert key == "musicbrainz.org" and interval == 1.0
        sleeps.append(interval)
        return await original_pace(key, 0)

    monkeypatch.setattr(media_music.PACER, "pace", record_pace)
    group_id = "f3a5b6b7-62ec-4be7-bfc8-62ac2c2ea3d9"
    calls: list[tuple[str, dict[str, str]]] = []

    async def fetch(url: str, **kwargs) -> Fetched:
        calls.append((url, kwargs.get("headers", {})))
        if "listenbrainz.org" in url:
            return Fetched(
                json.dumps(
                    {
                        "payload": {
                            "releases": [
                                {
                                    "release_mbid": "26a9db3c-1242-48c7-bc6c-0c2fb66ad49b",
                                    "release_group_mbid": group_id,
                                    "release_name": "New Album",
                                    "artist_credit_name": "Example Artist",
                                    "release_date": "2025-03",
                                }
                            ]
                        }
                    }
                ).encode()
            )
        if "musicbrainz.org" in url:
            return Fetched(
                json.dumps(
                    {
                        "release-groups": [
                            {
                                "id": group_id,
                                "title": "New Album",
                                "first-release-date": "2025-03",
                                "primary-type": "Album",
                                "tags": [{"name": "music", "count": 4}],
                                "artist-credit": [{"name": "Example Artist"}],
                                "releases": [
                                    {"country": "US", "text-representation": {"language": "eng"}}
                                ],
                            }
                        ]
                    }
                ).encode()
            )
        assert url == f"https://coverartarchive.org/release-group/{group_id}/front-500.jpg"
        assert kwargs.get("allow_redirects") is True
        return Fetched(b"cover", final_url="https://archive.org/download/cover/front-500.jpg")

    page = await collect_listenbrainz_releases(
        {"listenbrainz_days": 14},
        cursor=None,
        collected_at=datetime(2025, 3, 2, tzinfo=UTC),
        fetch_bytes=fetch,
    )
    assert sleeps == [1.0]
    assert len(calls) == 3
    assert calls[1][1]["User-Agent"].startswith("Umwelt-OS/1.0 (")
    media = page.records[0].metadata["provider_record"]["source_fields"]
    assert media["language"] == "en" and media["country"] == "US"
    assert media["image_url"] == "https://archive.org/download/cover/front-500.jpg"
    assert media["release_date"] == "2025-03" and "release_status" not in media
    assert page.cursor_after


@pytest.mark.asyncio
async def test_failed_musicbrainz_enrichment_returns_no_partial_page():
    group_id = "f3a5b6b7-62ec-4be7-bfc8-62ac2c2ea3d9"

    async def fetch(url: str, **kwargs) -> Fetched:
        if "listenbrainz.org" in url:
            return Fetched(
                json.dumps(
                    {
                        "payload": {
                            "releases": [
                                {
                                    "release_mbid": "26a9db3c-1242-48c7-bc6c-0c2fb66ad49b",
                                    "release_group_mbid": group_id,
                                    "release_name": "New Album",
                                    "artist_credit_name": "Example Artist",
                                    "release_date": "2025",
                                }
                            ]
                        }
                    }
                ).encode()
            )
        raise ProviderHttpError(503)

    with pytest.raises(ProviderHttpError):
        await collect_listenbrainz_releases(
            {"listenbrainz_days": 14},
            cursor="old-cursor",
            collected_at=datetime(2025, 3, 2, tzinfo=UTC),
            fetch_bytes=fetch,
        )


@pytest.mark.asyncio
async def test_older_release_and_empty_enrichment_are_retried_by_id(monkeypatch):
    from unittest.mock import AsyncMock

    newest = "f3a5b6b7-62ec-4be7-bfc8-62ac2c2ea3d9"
    older = "25a3f868-3fb5-443d-b169-ec223e88d524"
    missed = "d903e73f-67ab-449d-b764-2a01aa60fddd"
    candidates = [(newest, "2025-03-10"), (missed, "2025-03-10")]
    fetched_ids = []
    no_match = {missed}
    monkeypatch.setattr(media_music.PACER, "pace", AsyncMock())

    async def fetch(url: str, **kwargs) -> Fetched:
        if "listenbrainz.org" in url:
            return Fetched(
                json.dumps(
                    {
                        "payload": {
                            "releases": [
                                {
                                    "release_group_mbid": group_id,
                                    "release_name": "Album",
                                    "release_date": date,
                                }
                                for group_id, date in candidates
                            ]
                        }
                    }
                ).encode()
            )
        if "musicbrainz.org" in url:
            group_id = next(group_id for group_id, _ in candidates if group_id in url)
            fetched_ids.append(group_id)
            groups = [] if group_id in no_match else [{"id": group_id, "title": "Album"}]
            return Fetched(json.dumps({"release-groups": groups}).encode())
        raise ProviderHttpError(404)

    now = datetime(2025, 3, 11, tzinfo=UTC)
    first = await collect_listenbrainz_releases(
        {}, cursor=None, collected_at=now, fetch_bytes=fetch
    )
    assert fetched_ids == [newest, missed]
    candidates.append((older, "2025-03-06"))
    no_match.clear()
    fetched_ids.clear()
    second = await collect_listenbrainz_releases(
        {}, cursor=first.cursor_after, collected_at=now, fetch_bytes=fetch
    )
    assert fetched_ids == [missed, older]
    assert len(second.records) == 2


@pytest.mark.asyncio
async def test_updated_discovery_for_the_same_group_is_enriched_again(monkeypatch):
    from unittest.mock import AsyncMock

    group = "f3a5b6b7-62ec-4be7-bfc8-62ac2c2ea3d9"
    date = "2025-03-11"
    enrichment = []
    monkeypatch.setattr(media_music.PACER, "pace", AsyncMock())

    async def fetch(url, **kwargs):
        if "listenbrainz.org" in url:
            return Fetched(
                json.dumps(
                    {
                        "payload": {
                            "releases": [
                                {
                                    "release_group_mbid": group,
                                    "release_name": "Album",
                                    "release_date": date,
                                }
                            ]
                        }
                    }
                ).encode()
            )
        if "musicbrainz.org" in url:
            enrichment.append(date)
            return Fetched(
                json.dumps({"release-groups": [{"id": group, "title": "Album"}]}).encode()
            )
        raise ProviderHttpError(404)

    now = datetime(2025, 3, 12, tzinfo=UTC)
    initial = await collect_listenbrainz_releases(
        {}, cursor=None, collected_at=now, fetch_bytes=fetch
    )
    date = "2025-03-09"
    updated = await collect_listenbrainz_releases(
        {}, cursor=initial.cursor_after, collected_at=now, fetch_bytes=fetch
    )
    assert enrichment == ["2025-03-11", "2025-03-09"]
    (record,) = updated.records
    assert record.metadata["provider_record"]["source_fields"]["release_date"] == date
    repeat = await collect_listenbrainz_releases(
        {}, cursor=updated.cursor_after, collected_at=now, fetch_bytes=fetch
    )
    assert not repeat.records
    assert enrichment == ["2025-03-11", "2025-03-09"]


@pytest.mark.asyncio
async def test_oversized_fresh_release_page_keeps_newest_rows_and_reports_truncated(monkeypatch):
    """The real endpoint returns thousands of rows: keep the newest MAX_RELEASES and flag truncation."""
    from unittest.mock import AsyncMock

    monkeypatch.setattr(media_music.PACER, "pace", AsyncMock())
    total = media_music.MAX_RELEASES + 5
    ids = [f"00000000-0000-4000-8000-{index:012d}" for index in range(total)]
    # Row i has date 2025-01-(i+1), so the newest rows are the highest indexes.
    rows = [
        {
            "release_group_mbid": group,
            "release_name": "Album",
            "release_date": f"2025-01-{index + 1:02d}",
        }
        for index, group in enumerate(ids)
    ]
    fetched: list[str] = []

    async def fetch(url: str, **kwargs) -> Fetched:
        if "listenbrainz.org" in url:
            return Fetched(json.dumps({"payload": {"releases": rows}}).encode())
        if "musicbrainz.org" in url:
            group = next(group for group in ids if group in url)
            fetched.append(group)
            return Fetched(
                json.dumps({"release-groups": [{"id": group, "title": "Album"}]}).encode()
            )
        raise ProviderHttpError(404)

    page = await collect_listenbrainz_releases(
        {}, cursor=None, collected_at=datetime(2025, 2, 1, tzinfo=UTC), fetch_bytes=fetch
    )
    assert page.coverage == "truncated"
    assert fetched == ids[:4:-1]  # the 20 newest, newest first
    assert len(page.records) == media_music.MAX_RELEASES
    assert len(page.cursor_after) <= 4096
