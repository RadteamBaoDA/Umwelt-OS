"""Feed thumbnail extraction, YouTube thumbnails and the dashboard DTO's signed ``image_url`` (no network)."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.media_proxy import PROXY_PATH, make_image_signer
from modules.chat import media
from modules.connectors.providers import news
from modules.knowledge.documents import _news
from tests.unit.modules.chat.test_media import SECRET, _config

NOW = datetime(2026, 1, 1, tzinfo=UTC)
OK = "https://ichef.example.com/a.jpg"


def _feed(inner: str) -> bytes:
    return (
        '<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel>'
        f"<item><title>T</title><link>https://x.example/1</link>{inner}</item></channel></rss>"
    ).encode()


def _fields(inner: str) -> dict:
    record = news.map_feed("bbc_world", _feed(inner), NOW)[0]
    return record.metadata["provider_record"]["source_fields"]


@pytest.mark.parametrize(
    "inner",
    [
        f'<media:thumbnail url="{OK}"/>',
        f'<media:content url="{OK}" medium="image"/>',
        f'<media:content url="{OK}" type="image/png"/>',
        f'<enclosure url="{OK}" type="image/jpeg" length="1"/>',
    ],
)
def test_each_tag_form(inner: str) -> None:
    assert _fields(inner)["image_url"] == OK


@pytest.mark.parametrize(
    "inner",
    [
        '<media:thumbnail url="http://ichef.example.com/a.jpg"/>',
        f'<media:thumbnail url="https://ichef.example.com/{"a" * 2100}.jpg"/>',
        '<media:thumbnail url="https://u:p@ichef.example.com/a.jpg"/>',
        '<media:thumbnail url="https://[bad/a.jpg"/>',
        "<media:thumbnail/>",
        f'<media:content url="{OK}" medium="video"/>',
        f'<enclosure url="{OK}" type="audio/mpeg"/>',
        "",
    ],
)
def test_invalid_or_missing_image_is_dropped_not_fatal(inner: str) -> None:
    assert "image_url" not in _fields(inner)


def test_invalid_first_candidate_falls_through_to_next() -> None:
    inner = f'<media:thumbnail url="http://x/a.jpg"/><enclosure url="{OK}" type="image/jpeg"/>'
    assert _fields(inner)["image_url"] == OK


def test_youtube_id_validation() -> None:
    from modules.connectors.providers import feed_catalog as fc

    assert fc._YT_VIDEO_ID.fullmatch("dQw4w9WgXcQ")
    assert not fc._YT_VIDEO_ID.fullmatch("short")
    assert not fc._YT_VIDEO_ID.fullmatch("dQw4w9WgXc/../")
    assert media.check_upstream_url("https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg")


def _item(url: str | None, local_only: bool = False) -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        document_id=uuid4(),
        document_version_id=uuid4(),
        version_number=1,
        source_id=uuid4(),
        title="T",
        canonical_url=None,
        published_at=None,
        observed_at=now,
        chunks=[],
        metadata_is_version_snapshot=True,
        local_only=local_only,
        provider_metadata=SimpleNamespace(
            provider="bbc_world", source_fields={"image_url": url} if url else {}, telegram=None
        ),
    )


def _dto(url: str | None, *, flag: bool = True, local_only: bool = False) -> str | None:
    signer = make_image_signer(_config(flag), SECRET, "w:1")
    return _news._as_gadget_document_projection(_item(url, local_only), None, signer).image_url


def test_dto_image_url_gating() -> None:
    allowed = "https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg"
    assert _dto(allowed, flag=False) is None
    assert _dto(allowed, local_only=True) is None
    assert _dto("https://evil.example.com/a.jpg") is None
    assert _dto(None) is None
    signed = _dto(allowed)
    assert signed is not None and signed.startswith(f"{PROXY_PATH}?token=")
    token = signed.split("token=", 1)[1]
    assert media.verify_media_token(SECRET, token, subject="w:1") == allowed
