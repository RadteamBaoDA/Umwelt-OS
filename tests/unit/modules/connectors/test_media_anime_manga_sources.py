"""Offline contracts for ANN, MangaUpdates, and MangaDex media sources."""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from modules.connectors.providers.media_anime_manga_sources import (
    ANN_NEWS_URL,
    CursorPage,
    build_ann_encyclopedia_url,
    build_ann_news_url,
    build_mangadex_chapters_url,
    build_mangadex_manga_url,
    collect_ann_encyclopedia,
    collect_ann_news,
    collect_mangadex_releases,
    collect_mangaupdates_releases,
    mangaupdates_search_body,
    map_ann_encyclopedia,
    map_ann_news,
    map_mangadex_chapters,
    map_mangadex_manga,
    map_mangaupdates_releases,
)
from modules.connectors.providers.rest import CollectionIncomplete, Fetched

NOW = datetime(2026, 11, 17, tzinfo=UTC)


def test_ann_atom_maps_media_provenance_and_filters() -> None:
    body = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>tag:ann.example,2026:news/5</id><title>New manga adaptation announced</title><published>2026-11-16T10:00:00Z</published><link href="https://www.animenewsnetwork.com/news/2026-11-16/test"/><category term="Manga"/><category term="Drama"/><summary>Studio announced its adaptation.</summary></entry></feed>"""
    records = map_ann_news(body, collected_at=NOW, config={"media_genres": ["drama"]})
    assert len(records) == 1
    record = records[0]
    fields = record.metadata["provider_record"]["source_fields"]
    assert record.provider_id == "ann_news:tag:ann.example,2026:news/5"
    assert fields["media_type"] == "manga"
    assert fields["genres"] == ["drama"]
    assert fields["release_date"] == "2026-11-16T10:00:00Z"
    assert fields["attribution"] == "Data via Anime News Network"


def test_ann_rejects_hostile_xml_and_unsafe_links() -> None:
    with pytest.raises(ValueError):
        map_ann_news(b"<!DOCTYPE feed><feed/>", collected_at=NOW, config={})
    body = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>id-1</id><title>Title</title><link href="https://attacker.example/item"/></entry></feed>"""
    assert map_ann_news(body, collected_at=NOW, config={}) == []


def test_mangaupdates_search_body_is_bounded_and_normalized() -> None:
    body = mangaupdates_search_body(
        {
            "media_formats": ["manhwa"],
            "media_genres": ["fantasy"],
            "licensed_only": True,
            "release_year": 2025,
        }
    )
    assert (
        body
        == b'{"page":1,"perpage":100,"stype":"title","type":["Manhwa"],"filter_types":["Manhwa"],"licensed":true,"year":2025,"genre":["fantasy"]}'
    )


def test_mangaupdates_maps_chapter_identity_country_and_rating() -> None:
    payload = {
        "results": [
            {
                "record": {
                    "id": 41,
                    "title": "Example Webtoon",
                    "chapter": "12",
                    "release_date": "2026-11-15",
                },
                "metadata": {
                    "series": {
                        "series_id": 9,
                        "title": "Example Webtoon",
                        "type": "Manhwa",
                        "bayesian_rating": 8.2,
                    }
                },
            }
        ],
    }
    records = map_mangaupdates_releases(payload, collected_at=NOW, config={"series_ids": ["9"]})
    assert len(records) == 1
    fields = records[0].metadata["provider_record"]["source_fields"]
    assert fields["media_type"] == "manga" and fields["format"] == "manhwa"
    assert fields["country"] == "KR" and fields["rating"] == 82.0
    assert fields["chapter"] == "12" and fields["release_year"] == 2026
    assert map_mangaupdates_releases(payload, collected_at=NOW, config={"series_ids": ["10"]}) == []


def test_mangadex_urls_bound_and_encode_provider_filters() -> None:
    manga_id = "123e4567-e89b-12d3-a456-426614174000"
    excluded_tag = "123e4567-e89b-12d3-a456-426614174001"
    manga_url = build_mangadex_manga_url(
        {
            "manga_ids": [manga_id],
            "original_languages": ["zh-hk"],
            "available_translated_languages": ["en"],
            "content_ratings": ["safe"],
            "publication_demographic": ["shounen"],
            "excluded_tags": [excluded_tag],
        }
    )
    assert f"ids%5B%5D={manga_id}" in manga_url
    assert "originalLanguage%5B%5D=zh-hk" in manga_url
    assert "contentRating%5B%5D=safe" in manga_url
    assert "publicationDemographic%5B%5D=shounen" in manga_url
    assert f"excludedTags%5B%5D={excluded_tag}" in build_mangadex_chapters_url(
        {"excluded_tags": [excluded_tag]}, {manga_id: {"title": "Series", "attrs": {}}}
    )
    manga = {manga_id: {"title": "Series", "attrs": {}}}
    chapter_url = build_mangadex_chapters_url(
        {"media_languages": ["en"], "media_genres": ["drama", "fantasy"]}, manga
    )
    assert f"manga%5B%5D={manga_id}" in chapter_url
    assert "translatedLanguage%5B%5D=en" in chapter_url
    assert "includedTagsMode=OR" in chapter_url
    assert "availableTranslatedLanguage%5B%5D=en" in manga_url
    assert "availableTranslatedLanguage%5B%5D=en" not in chapter_url
    with pytest.raises(ValueError):
        build_mangadex_manga_url({"original_languages": ["xx"]})


def test_mangadex_maps_chapter_and_preserves_zh_hk_country() -> None:
    manga_id = "123e4567-e89b-12d3-a456-426614174000"
    chapter_id = "123e4567-e89b-12d3-a456-426614174001"
    manga = map_mangadex_manga(
        {
            "data": [
                {
                    "id": manga_id,
                    "attributes": {
                        "title": {"en": "Series"},
                        "originalLanguage": "zh-hk",
                        "contentRating": "safe",
                        "tags": [
                            {
                                "id": "b9af3a63-f058-46de-a9a0-e0c13906197a",
                                "attributes": {"name": {"en": "Drama"}},
                            }
                        ],
                    },
                    "relationships": [
                        {
                            "id": "123e4567-e89b-12d3-a456-426614174002",
                            "type": "cover_art",
                            "attributes": {"fileName": "cover.jpg"},
                        }
                    ],
                }
            ]
        }
    )
    records = map_mangadex_chapters(
        {
            "data": [
                {
                    "id": chapter_id,
                    "attributes": {
                        "chapter": "3",
                        "volume": "1",
                        "readableAt": "2026-11-16T10:00:00Z",
                        "contentRating": "safe",
                        "title": "A chapter",
                    },
                    "relationships": [{"id": manga_id, "type": "manga"}],
                }
            ]
        },
        manga=manga,
        collected_at=NOW,
        config={},
    )
    assert len(records) == 1
    fields = records[0].metadata["provider_record"]["source_fields"]
    assert fields["country"] == "TW" and fields["language"] == "zh-hk"
    assert fields["genres"] == ["drama"]
    assert fields["chapter"] == "3" and fields["content_rating"] == "safe"
    assert fields["external_ids"] == {"mangadex": manga_id}


@pytest.mark.asyncio
async def test_ann_uses_injected_sendgate_transport() -> None:
    fetch = AsyncMock(return_value=Fetched(body=b"<feed xmlns='http://www.w3.org/2005/Atom'/>"))
    assert await collect_ann_news(fetch, collected_at=NOW, config={}) == []
    assert fetch.await_args.args[0] == ANN_NEWS_URL


@pytest.mark.asyncio
async def test_mangaupdates_uses_search_then_bounded_releases() -> None:
    fetch = AsyncMock(return_value=Fetched(body=b'{"results":[],"total_hits":0,"per_page":100}'))
    page = await collect_mangaupdates_releases(fetch, collected_at=NOW, config={})
    assert page == CursorPage((), None)
    assert fetch.await_args.args[0] == "https://api.mangaupdates.com/v1/releases/search"
    assert (
        fetch.await_args.kwargs["json_body"]
        == b'{"page":0,"perpage":100,"search_type":"regular","orderby":"date","asc":"desc","include_metadata":true}'
    )


@pytest.mark.asyncio
async def test_mangaupdates_reports_next_page_without_skipping_and_rejects_empty_gap() -> None:
    release = {"series_id": 9, "title": "Series", "type": "Manga"}
    releases = {
        "results": [
            {
                "record": {"id": index, "title": "Series", "chapter": str(index)},
                "metadata": {"series": release},
            }
            for index in range(100)
        ],
        "total_hits": 200,
        "per_page": 100,
    }
    fetch = AsyncMock(return_value=Fetched(body=json.dumps(releases).encode()))
    page = await collect_mangaupdates_releases(fetch, collected_at=NOW, config={}, page=0)
    assert page.cursor == "1"
    assert fetch.await_args.args[0] == "https://api.mangaupdates.com/v1/releases/search"
    assert json.loads(fetch.await_args.kwargs["json_body"])["page"] == 0

    short = {
        "results": [
            {
                "record": {"id": 1, "title": "Series", "chapter": "1"},
                "metadata": {"series": release},
            }
        ],
        "total_hits": 200,
        "per_page": 100,
    }
    short_fetch = AsyncMock(return_value=Fetched(body=json.dumps(short).encode()))
    assert (
        await collect_mangaupdates_releases(short_fetch, collected_at=NOW, config={}, page=0)
    ).cursor == "1"

    broken = AsyncMock(return_value=Fetched(body=b'{"results":[],"total_hits":200,"per_page":100}'))
    with pytest.raises(CollectionIncomplete):
        await collect_mangaupdates_releases(broken, collected_at=NOW, config={}, page=0)


@pytest.mark.asyncio
async def test_mangadex_uses_two_bounded_requests() -> None:
    fetch = AsyncMock(
        side_effect=[
            Fetched(
                body=b'{"data":[{"id":"123e4567-e89b-12d3-a456-426614174000","attributes":{"title":{"en":"Series"}},"relationships":[]}]}'
            ),
            Fetched(body=b'{"data":[]}'),
        ]
    )
    page = await collect_mangadex_releases(fetch, collected_at=NOW, config={})
    assert page == CursorPage((), None)
    assert fetch.await_count == 2
    assert "/manga?" in fetch.await_args_list[0].args[0]
    assert "/chapter?" in fetch.await_args_list[1].args[0]


@pytest.mark.asyncio
async def test_mangadex_returns_cursor_for_more_manga_pages() -> None:
    manga_response = {
        "data": [
            {
                "id": "123e4567-e89b-12d3-a456-426614174000",
                "attributes": {"title": {"en": "Series"}},
                "relationships": [],
            }
        ],
        "total": 101,
    }
    fetch = AsyncMock(
        side_effect=[
            Fetched(body=json.dumps(manga_response).encode()),
            Fetched(body=b'{"data":[],"total":0}'),
        ]
    )
    page = await collect_mangadex_releases(fetch, collected_at=NOW, config={})
    assert page.cursor == "1:0"
    assert "offset=0" in fetch.await_args_list[0].args[0]

    fetch_next = AsyncMock(
        side_effect=[
            Fetched(
                body=b'{"data":[{"id":"123e4567-e89b-12d3-a456-426614174003","attributes":{"title":{"en":"Series 2"}},"relationships":[]}],"total":101}'
            ),
            Fetched(body=b'{"data":[],"total":0}'),
        ]
    )
    await collect_mangadex_releases(fetch_next, collected_at=NOW, config={}, cursor=page.cursor)
    assert "offset=1" in fetch_next.await_args_list[0].args[0]


def test_ann_category_url_is_fixed_host_and_validated() -> None:
    assert (
        build_ann_news_url({"ann_category": "review", "ann_edition": "us"})
        == "https://www.animenewsnetwork.com/news/review/atom.xml?ann-edition=us"
    )
    with pytest.raises(ValueError):
        build_ann_news_url({"ann_category": "../../evil"})


def test_ann_encyclopedia_maps_report_and_bounds_query() -> None:
    xml = b"""<report><item id="4658"><name>Example Anime</name><year>2004</year><genre>Drama</genre><description>A bounded synopsis.</description></item></report>"""
    records = map_ann_encyclopedia(xml, media_type="anime", collected_at=NOW, config={})
    assert len(records) == 1
    fields = records[0].metadata["provider_record"]["source_fields"]
    assert fields["media_type"] == "anime"
    assert fields["release_year"] == 2004 and fields["genres"] == ["drama"]
    assert (
        fields["canonical_url"] == "https://www.animenewsnetwork.com/encyclopedia/anime.php?id=4658"
    )
    assert "type=anime" in build_ann_encyclopedia_url("anime", offset=100)
    assert "nskip=100" in build_ann_encyclopedia_url("anime", offset=100)
    with pytest.raises(ValueError):
        build_ann_encyclopedia_url("person")


@pytest.mark.asyncio
async def test_ann_encyclopedia_uses_bounded_cursor_transport() -> None:
    xml = b'<report><item id="1"><name>Anime</name></item></report>'
    fetch = AsyncMock(return_value=Fetched(body=xml))
    page = await collect_ann_encyclopedia(
        fetch, media_type="anime", collected_at=NOW, config={}, offset=100
    )
    assert len(page.records) == 1 and page.cursor is None
    assert "nskip=100" in fetch.await_args.args[0]
    assert fetch.await_args.kwargs["max_bytes"] > len(xml)


def test_mangadex_default_content_rating_excludes_adult() -> None:
    manga_id = "123e4567-e89b-12d3-a456-426614174000"
    for url in (
        build_mangadex_manga_url({}),
        build_mangadex_chapters_url({}, {manga_id: {"title": "S", "attrs": {}}}),
    ):
        assert "contentRating%5B%5D=safe" in url
        assert "contentRating%5B%5D=suggestive" in url
        assert "erotica" not in url and "pornographic" not in url
