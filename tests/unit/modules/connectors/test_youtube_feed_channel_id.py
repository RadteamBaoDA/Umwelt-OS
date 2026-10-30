"""YouTube Atom mapper accepts both ``UC``-prefixed and bare ``yt:channelId`` forms."""
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from modules.connectors.providers.feed_catalog import _records_from_feed

BARE = "a" * 22
CONFIGURED = "UC" + BARE
OTHER_BARE = "b" * 22


def _feed(channel_id: str) -> bytes:
    """Build a one-entry YouTube Atom feed whose ``yt:channelId`` is ``channel_id``."""
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:yt="http://www.youtube.com/xml/schemas/2015"><entry>'
        f"<yt:videoId>vid1</yt:videoId><yt:channelId>{channel_id}</yt:channelId>"
        "<title>T</title><published>2026-01-01T00:00:00+00:00</published>"
        "<updated>2026-01-01T00:00:00+00:00</updated></entry></feed>"
    ).encode()


def _records(channel_id: str) -> list:
    """Return the records the mapper yields for a feed carrying ``channel_id``."""
    source = SimpleNamespace(provider="youtube", configuration={"youtube_channel_id": CONFIGURED})
    page = _records_from_feed(source, _feed(channel_id), datetime.now(UTC))  # type: ignore[arg-type]
    return page.records


@pytest.mark.parametrize(("feed_id", "expected"), [
    (CONFIGURED, 1),
    (BARE, 1),
    ("UC" + OTHER_BARE, 0),
    (OTHER_BARE, 0),
])
def test_channel_id_forms(feed_id: str, expected: int) -> None:
    """Prefixed and bare forms of the configured channel match; other channels are rejected."""
    records = _records(feed_id)
    assert len(records) == expected
    # Identity is scoped by the configured channel, never the feed's (possibly bare) channel id.
    assert [r.provider_id for r in records] == [f"youtube:{CONFIGURED}:vid1"] * expected
