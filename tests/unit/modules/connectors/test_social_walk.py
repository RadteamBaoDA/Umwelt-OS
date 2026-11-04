"""Catch-up walk (cursor v2) contract for Bluesky, Reddit, Mastodon and X using fake feeds."""

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from modules.connectors.providers import rest, social_feeds, social_networks

NOW = datetime(2026, 11, 2, tzinfo=UTC)
PAGE = 2


def _stamp(n: int) -> datetime:
    return datetime(2026, 10, 1, n, tzinfo=UTC)


def _ok(value: object) -> rest.Fetched:
    return rest.Fetched(json.dumps(value).encode())


class Feed:
    """Newest-first fake timeline of post numbers; pages by the previous page's last post number."""

    def __init__(self, numbers: list[int]) -> None:
        self.numbers = sorted(numbers, reverse=True)
        self.fetches = 0

    def add(self, number: int) -> None:
        self.numbers = sorted([*self.numbers, number], reverse=True)

    def page(self, after: str | None) -> tuple[list[int], str | None]:
        self.fetches += 1
        rest_ = [n for n in self.numbers if after is None or n < int(after)]
        chunk = rest_[:PAGE]
        more = len(rest_) > PAGE
        return chunk, (str(chunk[-1]) if more else None)


def _reddit(feed: Feed) -> Callable[[str | None], Awaitable[social_networks.SocialCollectionResult]]:
    async def fetch(url: str, **_: object) -> rest.Fetched:
        after = parse_qs(urlsplit(url).query).get("after", [None])[0]
        chunk, nxt = feed.page(after)
        return _ok({"data": {"after": nxt, "children": [{"data": {
            "id": str(n), "permalink": f"/r/py/c/{n}/", "title": f"t{n}",
            "created_utc": _stamp(n).timestamp()}} for n in chunk]}})

    async def run(cursor: str | None) -> social_networks.SocialCollectionResult:
        return await social_networks.collect_reddit_subreddit(
            "python", "tok", cursor=cursor, collected_at=NOW, fetch_bytes=fetch)
    return run


def _mastodon(feed: Feed) -> Callable[[str | None], Awaitable[social_networks.SocialCollectionResult]]:
    # Mastodon adapter only pages onward on a full 40-row page; drive it with PAGE-sized pages by patching below.
    async def fetch(url: str, **_: object) -> rest.Fetched:
        after = parse_qs(urlsplit(url).query).get("max_id", [None])[0]
        chunk, _nxt = feed.page(after)
        return _ok([{"id": str(n), "visibility": "public", "url": f"https://m.example/@a/{n}",
                     "created_at": _stamp(n).isoformat(), "content": f"<p>p{n}</p>",
                     "account": {"acct": "a"}} for n in chunk])

    async def run(cursor: str | None) -> social_networks.SocialCollectionResult:
        return await social_networks.collect_mastodon_scope(
            "https://m.example", account=None, hashtag="osint", access_token=None,
            cursor=cursor, collected_at=NOW, fetch_bytes=fetch)
    return run


def _x(feed: Feed) -> Callable[[str | None], Awaitable[social_networks.SocialCollectionResult]]:
    async def fetch(url: str, **_: object) -> rest.Fetched:
        after = parse_qs(urlsplit(url).query).get("next_token", [None])[0]
        chunk, nxt = feed.page(after)
        meta = {"next_token": nxt} if nxt else {}
        return _ok({"data": [{"id": str(n), "text": f"p{n}", "created_at": _stamp(n).isoformat()} for n in chunk],
                    "meta": meta})

    async def run(cursor: str | None) -> social_networks.SocialCollectionResult:
        return await social_networks.collect_x_scope(
            account=None, query="climate", access_token="b", cursor=cursor, collected_at=NOW, fetch_bytes=fetch)
    return run


def _bluesky(feed: Feed) -> Callable[[str | None], Awaitable[social_feeds.BlueskyCollectionPage]]:
    async def fetch(url: str) -> rest.Fetched:
        after = parse_qs(urlsplit(url).query).get("cursor", [None])[0]
        chunk, nxt = feed.page(after)
        payload: dict[str, object] = {"feed": [{"post": {
            "uri": f"at://did:plc:a/app.bsky.feed.post/{n}", "cid": f"c{n}",
            "record": {"text": f"p{n}", "createdAt": _stamp(n).isoformat()},
            "author": {"handle": "a.bsky.social"}}} for n in chunk]}
        if nxt:
            payload["cursor"] = nxt
        return _ok(payload)

    async def run(cursor: str | None) -> social_feeds.BlueskyCollectionPage:
        return await social_feeds.collect_bluesky_profile(
            "a.bsky.social", fetch_bytes=fetch, collected_at=NOW, cursor=cursor)
    return run


FACTORIES = {"reddit": _reddit, "x": _x, "bluesky": _bluesky, "mastodon": _mastodon}


@pytest.fixture(autouse=True)
def _small_mastodon_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the Mastodon adapter treat a PAGE-sized page as full so the fake can paginate."""
    monkeypatch.setattr(social_networks, "MASTODON_PAGE", PAGE, raising=False)


def _ids(result: object) -> list[str]:
    return [r.provider_id.rsplit("/", 1)[-1].removeprefix("t3_") for r in result.records]  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", list(FACTORIES))
async def test_backlog_walk_reaches_high_water_then_new_posts_appear_first(provider: str):
    feed = Feed([5, 4, 3, 2, 1])
    run = FACTORIES[provider](feed)
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(3):  # 5 posts / page of 2 => three syncs to finish the backlog
        result = await run(cursor)
        seen += _ids(result)
        cursor = result.cursor_after
    assert sorted(seen) == ["1", "2", "3", "4", "5"] and len(seen) == len(set(seen))
    state = json.loads(cursor)  # type: ignore[arg-type]
    assert state["v"] == 2 and "page" not in state and state["high_water"] == _stamp(5).isoformat()

    feed.add(6)
    feed.fetches = 0
    result = await run(cursor)
    assert "6" in _ids(result) and "4" not in _ids(result)  # first sync after completion carries the new post
    cursor = result.cursor_after
    for _ in range(3):  # drain until the walk reports caught up; it must stop paging at high water
        if "page" not in json.loads(cursor):  # type: ignore[arg-type]
            break
        cursor = (await run(cursor)).cursor_after
    assert json.loads(cursor)["high_water"] == _stamp(6).isoformat()  # type: ignore[arg-type]
    assert feed.fetches <= 2  # never pages past the old high-water mark
    nothing = await run(cursor)
    assert set(_ids(nothing)) <= {"6"} and "page" not in json.loads(nothing.cursor_after)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", list(FACTORIES))
async def test_v1_and_malformed_cursors_restart_safely(provider: str):
    for raw in (
        json.dumps({"v": 1, "provider": provider, "actor": "a.bsky.social", "page": "4"}),
        "not json", "[]", '{"v":9}', '{"v":2,"high_water":"nope","page":5}', "x" * 4000,
    ):
        feed = Feed([5, 4, 3])
        result = await FACTORIES[provider](feed)(raw)
        assert _ids(result)[0] == "5"  # restarted from the top, no exception
        assert json.loads(result.cursor_after)["v"] == 2  # type: ignore[arg-type]
