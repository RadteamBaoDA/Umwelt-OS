"""Focused provider contract tests for the bounded Steam and IGDB collectors."""

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from modules.connectors.providers import media_games, rest
from modules.connectors.service.schemas import ConnectorConfig


@pytest.mark.asyncio
async def test_steam_featured_requests_one_appdetails_per_id_and_keeps_coming_soon_date():
    """Steam storefront and language reach each request, with one detail call per app and fuzzy price/date intact."""
    seen: list[tuple[str, str, dict[str, list[str]]]] = []

    async def fetch(url: str, **kwargs: object) -> rest.Fetched:
        parsed = urlsplit(url)
        params = parse_qs(parsed.query)
        seen.append((parsed.path, params.get("appids", [""])[0], params))
        if parsed.path.endswith("featuredcategories"):
            payload = {
                "status": 1,
                "new_releases": {"items": [{"id": 10}]},
                "coming_soon": {"items": [{"id": 20}]},
            }
        else:
            appid = params["appids"][0]
            payload = {
                appid: {
                    "success": True,
                    "data": {
                        "name": f"Game {appid}",
                        "release_date": {"coming_soon": appid == "20", "date": "Q4 2026"},
                        "price_overview": {"final": 999},
                        "genres": [{"description": "Action"}],
                        "metacritic": {"score": 80},
                    },
                }
            }
        return rest.Fetched(json.dumps(payload).encode())

    page = await media_games.collect_steam(
        ConnectorConfig(steam_country="VN", steam_language="vietnamese"),
        datetime(2026, 11, 18, tzinfo=UTC),
        fetch,
    )
    details = [appid for path, appid, _ in seen if path.endswith("appdetails")]
    assert details == ["10", "20"]
    assert len(details) == len(set(details))
    assert seen[0][2]["cc"] == ["VN"]
    assert seen[0][2]["l"] == ["vietnamese"]
    soon = next(record for record in page.records if record.provider_id == "steam:20")
    source_fields = soon.metadata["provider_record"]["source_fields"]
    assert source_fields["release_date_text"] == "Q4 2026"
    assert source_fields["final_price"] is None
    assert "release_date" not in source_fields
    assert all(
        params["cc"] == ["VN"] and params["l"] == ["vietnamese"]
        for path, _, params in seen
        if path.endswith("appdetails")
    )


@pytest.mark.asyncio
async def test_igdb_uses_form_oauth_then_text_apicalypse_and_maps_taxonomy():
    """IGDB credentials stay in the OAuth form body, and the v4 query uses current fields and canonical mapping."""
    seen: list[tuple[str, dict[str, object]]] = []

    async def fetch(url: str, **kwargs: object) -> rest.Fetched:
        seen.append((url, kwargs))
        if url == "https://id.twitch.tv/oauth2/token":
            assert kwargs["request_content_type"] == "application/x-www-form-urlencoded"
            body = kwargs["request_body"]
            assert isinstance(body, bytes) and b"client_secret=secret-value" in body
            return rest.Fetched(b'{"access_token":"short-lived-token","expires_in":3600}')
        assert url == "https://api.igdb.com/v4/games"
        query = kwargs["request_body"]
        assert isinstance(query, bytes)
        assert b"game_type = (0)" in query
        assert b"age_ratings.rating_category.rating,age_ratings.organization.name" in query
        assert b"external_game_source" in query
        assert b"age_ratings.category" not in query
        assert b"release_dates.release_region = (8)" in query
        assert kwargs["request_content_type"] == "text/plain"
        return rest.Fetched(
            json.dumps(
                [
                    {
                        "id": 123,
                        "name": "Game",
                        "summary": "A game",
                        "first_release_date": 1794960000,
                        "genres": [{"id": 31, "name": "Adventure"}],
                        "themes": [{"id": 18, "name": "Science fiction"}],
                        "game_type": {"id": 0, "name": "Main Game"},
                        "rating": 91.0,
                        "age_ratings": [
                            {"organization": {"name": "ESRB"}, "rating_category": {"rating": "T"}}
                        ],
                        "external_games": [{"uid": "570", "external_game_source": 1}],
                    }
                ]
            ).encode()
        )

    page = await media_games.collect_igdb(
        ConnectorConfig(igdb_game_types=("0",), igdb_region="world"),
        "client-id",
        "secret-value",
        datetime(2026, 11, 18, tzinfo=UTC),
        fetch,
    )
    game = page.records[0]
    source_fields = game.metadata["provider_record"]["source_fields"]
    assert source_fields["genres"] == ["adventure", "sci_fi"]
    assert source_fields["format"] == "main_game"
    assert source_fields["rating"] == 91.0
    assert source_fields["content_rating"] == "pg_13"
    assert source_fields["external_ids"]["steam"] == "570"
    assert "short-lived-token" not in page.cursor_after
    assert "secret-value" not in seen[0][0]


def test_igdb_age_rating_v4_mapping():
    def row(org, rating):
        return {"organization": {"name": org}, "rating_category": {"rating": rating}}

    assert media_games._igdb_age_rating(row("ESRB", "E10+")) == "pg"
    assert media_games._igdb_age_rating(row("PEGI", "Eighteen")) == "mature"
    assert media_games._igdb_age_rating(row("ESRB", "AO")) == "explicit"
    assert media_games._igdb_age_rating(row("ACB", "M")) == "pg_13"
    assert media_games._igdb_age_rating(row("ESRB", "M")) == "mature"
    assert media_games._igdb_age_rating(row("X", "Nope")) is None


def test_igdb_apicalypse_splits_genre_and_theme_filters():
    """Canonical game genres compile to the matching IGDB genres or themes fields."""
    query = media_games._igdb_apicalypse({"media_genres": ["action", "adventure"]})
    assert "genres = (31)" in query
    assert "themes = (1)" in query
    assert " where category " not in query
    assert "game_type" in query
    assert "release_region" not in query


def test_steam_news_requires_bounded_app_scope_and_rejects_duplicates():
    """News fanout is constrained to eight distinct owner-selected app IDs."""
    with pytest.raises(ValueError):
        media_games.validate_steam_options({"steam_mode": "news"})
    with pytest.raises(ValueError):
        media_games.validate_steam_options({"steam_mode": "news", "steam_appids": [1, 1]})
    with pytest.raises(ValueError):
        ConnectorConfig(steam_appids=tuple(range(1, 10)))


@pytest.mark.asyncio
async def test_steam_news_fetches_each_selected_feed_and_detail_separately():
    """News mode fetches one RSS and one non-batched appdetails request for each selected app."""
    seen: list[tuple[str, str]] = []

    async def fetch(url: str, **kwargs: object) -> rest.Fetched:
        parsed = urlsplit(url)
        appids = parse_qs(parsed.query).get("appids", [""])[0]
        seen.append((parsed.path, appids))
        if parsed.path.endswith("news.xml"):
            return rest.Fetched(
                b"<rss><channel><item><title>Update</title><link>https://steamcommunity.com/games/10/announcements/detail/99</link><guid>99</guid><pubDate>Tue, 18 Nov 2026 00:00:00 GMT</pubDate><description>Patch</description></item></channel></rss>"
            )
        return rest.Fetched(
            json.dumps(
                {
                    "10": {
                        "success": True,
                        "data": {
                            "name": "Game 10",
                            "release_date": {"coming_soon": False, "date": "2026"},
                        },
                    }
                }
            ).encode()
        )

    page = await media_games.collect_steam(
        ConnectorConfig(steam_mode="news", steam_appids=(10,)),
        datetime(2026, 11, 18, tzinfo=UTC),
        fetch,
    )
    assert seen == [
        ("/feeds/news.xml", "10"),
        ("/api/appdetails", "10"),
    ]
    assert any(record.provider_id.startswith("steam-news:10:") for record in page.records)


@pytest.mark.asyncio
async def test_igdb_rejects_a_token_that_expires_before_response_acceptance(
    monkeypatch: pytest.MonkeyPatch,
):
    """A slow IGDB request cannot produce records or a cursor after the Twitch token expires."""
    start = datetime(2026, 11, 18, tzinfo=UTC)

    class MutableDateTime(datetime):
        current = start

        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return cls.current

    monkeypatch.setattr(media_games, "datetime", MutableDateTime)

    async def fetch(url: str, **kwargs: object) -> rest.Fetched:
        if url == "https://id.twitch.tv/oauth2/token":
            return rest.Fetched(b'{"access_token":"short-lived-token","expires_in":31}')
        MutableDateTime.current += timedelta(seconds=32)
        return rest.Fetched(b"[]")

    with pytest.raises(rest.CollectionIncomplete):
        await media_games.collect_igdb(
            ConnectorConfig(igdb_region="world"),
            "client-id",
            "secret",
            start,
            fetch,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [None, {}, {"items": None}, {"items": {}}, {"items": [None]}])
async def test_steam_featured_rejects_malformed_present_category_before_any_details(malformed):
    async def fetch(url, **kwargs):
        assert urlsplit(url).path.endswith("featuredcategories")
        return rest.Fetched(
            json.dumps(
                {
                    "new_releases": {"items": [{"id": 10}]},
                    "coming_soon": malformed,
                }
            ).encode()
        )

    with pytest.raises(rest.RestSchemaChanged, match="steam_featured_invalid"):
        await media_games.collect_steam(
            ConnectorConfig(), datetime(2026, 11, 18, tzinfo=UTC), fetch
        )
