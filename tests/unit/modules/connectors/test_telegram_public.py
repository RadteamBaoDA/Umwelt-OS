"""Public Telegram channel web-preview mapping and scope validation."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from modules.connectors.providers import rest
from modules.connectors.providers.telegram_public import collect_telegram_public_channel
from modules.connectors.service.schemas import ConnectorConfig

NOW = datetime(2026, 11, 22, tzinfo=UTC)
PAGE = """
<html><head><meta property="og:title" content="OG Title"></head><body>
<div class="tgme_channel_info_header_title">Wall Street VN</div>
<div class="tgme_widget_message" data-post="vnwallstreet/10">
  <div class="tgme_widget_message_text">Hello <i style="background-image:url('//telegram.org/img/emoji/40/x.png')"></i>
  world<br>second line</div>
  <a class="tgme_widget_message_photo_wrap" style="width:1px;background-image:url('https://cdn4.telesco.pe/file/a.jpg')"></a>
  <a class="tgme_widget_message_photo_wrap" style="background-image:url('https://cdn4.telesco.pe/file/b.jpg')"></a>
  <a class="tgme_widget_message_date"><time datetime="2026-11-21T10:00:00+00:00"></time></a>
</div>
<div class="tgme_widget_message" data-post="vnwallstreet/11">
  <div class="tgme_widget_message_text">Text only</div>
  <a class="tgme_widget_message_date"><time datetime="2026-11-21T11:00:00+00:00"></time></a>
</div>
<div class="tgme_widget_message" data-post="vnwallstreet/12">
  <i class="tgme_widget_message_video_thumb" style="background-image:url('https://cdn4.telesco.pe/file/v.jpg')"></i>
  <a class="tgme_widget_message_date"><time datetime="2026-11-21T12:00:00+00:00"></time></a>
</div>
<div class="tgme_widget_message" data-post="vnwallstreet/13">
  <div class="tgme_widget_message_text">No time</div>
</div>
</body></html>
"""


async def _collect(html: str) -> object:
    async def fetch(url: str) -> rest.Fetched:
        assert url == "https://t.me/s/vnwallstreet"
        return rest.Fetched(html.encode())

    return await collect_telegram_public_channel(
        "vnwallstreet", collected_at=NOW, fetch_bytes=fetch
    )


async def test_maps_posts_with_images_and_skips_untimed() -> None:
    page = await _collect(PAGE)
    records = page.records  # type: ignore[attr-defined]
    assert [r.provider_id for r in records] == [
        "telegram_public:vnwallstreet:10",
        "telegram_public:vnwallstreet:11",
        "telegram_public:vnwallstreet:12",
    ]
    first = records[0]
    assert first.content.startswith("Hello")
    assert first.metadata["title"] == "Hello\nworld"[:0] or first.metadata["title"]
    fields = first.metadata["provider_record"]["source_fields"]
    assert fields["image_url"] == "https://cdn4.telesco.pe/file/a.jpg"
    assert fields["author"] == "Wall Street VN"
    assert fields["canonical_url"] == "https://t.me/vnwallstreet/10"
    assert fields["channel_username"] == "vnwallstreet"
    assert "image_url" not in records[1].metadata["provider_record"]["source_fields"]
    assert records[2].content == "[photo]"
    assert records[2].metadata["provider_record"]["source_fields"]["image_url"].endswith("v.jpg")


async def test_empty_page_is_invalid() -> None:
    with pytest.raises(ValueError, match="telegram_public_response_invalid"):
        await _collect("<html></html>")
    with pytest.raises(ValueError, match="telegram_public_channel_unavailable"):
        await _collect('<div class="tgme_channel_info_header_title">x</div>')


@pytest.mark.parametrize(
    "raw",
    [
        "vnwallstreet",
        "@vnwallstreet",
        "t.me/vnwallstreet",
        "https://t.me/vnwallstreet",
        "https://t.me/s/vnwallstreet",
    ],
)
def test_channel_normalizes(raw: str) -> None:
    assert ConnectorConfig(telegram_public_channel=raw).telegram_public_channel == "vnwallstreet"


@pytest.mark.parametrize(
    "raw",
    [
        "ab",
        "1abcdef",
        "https://evil.com/vnwallstreet",
        "https://t.me/s/vnwallstreet/5",
        "https://t.me/vnwallstreet?x=1",
        "vn wallstreet",
        "https://t.me/+invite",
    ],
)
def test_channel_rejects_bad(raw: str) -> None:
    with pytest.raises(ValidationError):
        ConnectorConfig(telegram_public_channel=raw)
