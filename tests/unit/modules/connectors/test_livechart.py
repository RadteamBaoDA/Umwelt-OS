"""LiveChart RSS mapping and conditional transport tests without network access."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from modules.connectors.provider_specs import get_provider_spec
from modules.connectors.providers.livechart import collect_livechart_feed, map_livechart_feed
from modules.connectors.providers.rest import Fetched


def test_livechart_maps_episode_identity_metadata_and_filters() -> None:
    body = b'''<rss><channel><item><title>Series episode 2</title><link>https://www.livechart.me/episodes/2</link><guid>ep-2</guid><pubDate>Wed, 18 Nov 2026 20:00:00 GMT</pubDate><description><![CDATA[<p>Episode <b>summary</b></p>]]></description><category>Drama</category></item></channel></rss>'''
    now = datetime(2026, 11, 17, tzinfo=UTC)
    records = map_livechart_feed(body, collected_at=now, config={"media_genres": ["drama"]})
    assert len(records) == 1
    record = records[0]
    fields = record.metadata["provider_record"]["source_fields"]
    assert record.provider_id == "livechart:ep-2"
    assert fields["media_type"] == "anime" and fields["release_status"] == "upcoming"
    assert fields["genres"] == ["drama"] and "<p>" not in record.content
    assert map_livechart_feed(body, collected_at=now, config={"release_year": 2025}) == []


def test_livechart_rejects_unsafe_xml_and_noncanonical_links() -> None:
    now = datetime(2026, 11, 17, tzinfo=UTC)
    with pytest.raises(ValueError):
        map_livechart_feed(b"<!DOCTYPE rss><rss/>", collected_at=now, config={})
    body = b'''<rss><channel><item><title>Episode</title><link>https://attacker.example/episode</link><guid>x</guid><pubDate>Wed, 18 Nov 2026 20:00:00 GMT</pubDate></item></channel></rss>'''
    assert map_livechart_feed(body, collected_at=now, config={}) == []


def test_livechart_is_registered_as_fixed_keyless_media_feed() -> None:
    from modules.connectors.backends import PROVIDER_SOURCE_TYPES
    from modules.connectors.provider_options import option_fields
    from modules.connectors.public import source_data_shapes

    spec = get_provider_spec("livechart_episodes")
    assert spec is not None and not spec.key_required and spec.code_implemented
    assert PROVIDER_SOURCE_TYPES["livechart_episodes"] == "rss"
    assert source_data_shapes("livechart_episodes", "rss") == {"media"}
    assert option_fields("livechart_episodes") == {"media_genres", "release_year"}


@pytest.mark.asyncio
async def test_livechart_uses_conditional_sendgate_transport() -> None:
    fetch = AsyncMock(return_value=Fetched(body=None))
    page = await collect_livechart_feed(
        fetch,
        collected_at=datetime(2026, 11, 17, tzinfo=UTC),
        config={},
        etag='"feed-v1"',
    )
    assert page.not_modified and page.records == ()
    assert fetch.await_args.args[0] == "https://www.livechart.me/feeds/episodes"
    assert fetch.await_args.kwargs["headers"]["If-None-Match"] == '"feed-v1"'
    assert fetch.await_args.kwargs["max_bytes"] == 1024 * 1024
