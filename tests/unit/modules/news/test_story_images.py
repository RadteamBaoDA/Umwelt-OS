"""Story image signing: owner flag, local_only, publisher host and absent image (no network)."""

from types import SimpleNamespace

from core.media_proxy import FEED_KIND, PROXY_PATH, make_image_signer, verify_media_claims
from modules.news.stories._live import _signed_image
from tests.unit.modules.chat.test_media import SECRET, _config

PUBLISHER = "https://ichef.bbci.co.uk/news/976/a.jpg"


def _current(url: str | None, *, local_only: bool = False) -> SimpleNamespace:
    fields = {"image_url": url} if url else {}
    return SimpleNamespace(
        local_only=local_only, provider_metadata=SimpleNamespace(source_fields=fields)
    )


def _sign(url: str | None, *, flag: bool = True, local_only: bool = False) -> str | None:
    signer = make_image_signer(_config(flag), SECRET, "w:1")
    return _signed_image(_current(url, local_only=local_only), signer)


def test_story_image_gating() -> None:
    """Null when flag off, local_only, no image, no signer or no metadata."""
    assert _sign(PUBLISHER, flag=False) is None
    assert _sign(PUBLISHER, local_only=True) is None
    assert _sign(None) is None
    assert _signed_image(_current(PUBLISHER), None) is None
    assert _signed_image(SimpleNamespace(local_only=False, provider_metadata=None), None) is None


def test_story_image_is_signed_feed_proxy_path() -> None:
    """A publisher host yields a feed-kind proxy path bound to the actor."""
    signed = _sign(PUBLISHER)
    assert signed is not None and signed.startswith(f"{PROXY_PATH}?token=")
    token = signed.split("token=", 1)[1]
    assert verify_media_claims(SECRET, token, subject="w:1") == (PUBLISHER, FEED_KIND)
