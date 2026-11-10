"""Signed same-origin media proxy tokens: upstream host allowlist, signing and verification.

Moved from ``modules.chat.media`` (re-exported there) so Documents can issue proxy URLs for feed thumbnails
without importing another module. Pure helpers: no I/O, no logging of URLs or tokens.
"""

import base64
import hashlib
import hmac
import json
import re
import time
from collections.abc import Callable
from urllib.parse import urlsplit

from core.auth.dependencies import derive_key
from core.model_gateway.schemas import AIExecutionConfig
from core.pagination import b64url_encode

PROXY_PATH = "/api/v1/media/proxy"
PROXY_HOSTS = frozenset(
    {
        "upload.wikimedia.org",
        "basemap.nationalmap.gov",
        "i.ytimg.com",
    }  # i.ytimg.com: YouTube feed thumbnails
)
# Search-engine thumbnail CDNs (SearXNG image results): Bing ``ts1.mm.bing.net``/``tse2.mm.bing.net``, Brave and
# Wikimedia thumbnails. Fixed shapes only: never an arbitrary host. Bing URLs carry a query string, which the
# signed token preserves because it binds the whole URL.
SEARCH_THUMB_HOST = re.compile(
    r"(?:tse?\d{1,3}\.mm\.bing\.net|imgs\.search\.brave\.com|thumb\.wikimedia\.org)"
)

TOKEN_TTL_SECONDS = 3600
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


class MediaError(Exception):
    """Media fetch/token failure; ``code`` is a short non-sensitive reason (never a URL or token)."""

    def __init__(self, code: str) -> None:
        """Store the machine-readable reason code."""
        super().__init__(code)
        self.code = code


def media_permitted(config: AIExecutionConfig, *, local_only: bool) -> bool:
    """Whether remote widget media may be fetched this turn: owner flag on and the turn not local-only."""
    return config.privacy.allow_widget_remote_media and not local_only


def _key(secret: str) -> bytes:
    """Purpose-bound HMAC key; an unset secret fails closed (no unsigned tokens)."""
    if not secret:
        raise MediaError("not_configured")
    return derive_key(secret, "media-proxy-hmac")


def check_upstream_url(url: str) -> str:
    """Return ``url`` if it is https on an allowlisted host (``PROXY_HOSTS`` or a search-thumbnail CDN) with no
    userinfo/port/fragment, else raise MediaError."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise MediaError("host_denied") from None
    if (
        parts.scheme != "https"
        or not parts.hostname
        or (parts.hostname not in PROXY_HOSTS and not SEARCH_THUMB_HOST.fullmatch(parts.hostname))
        or parts.username
        or parts.password
        or port not in (None, 443)
        or parts.fragment
        or _CTRL.search(url)
    ):
        raise MediaError("host_denied")
    return url


def sign_media_url(secret: str, url: str, *, now: float | None = None, subject: str = "") -> str:
    """Issue a short-lived token binding exactly ``url`` (allowlist checked at signing and again at verify).

    ``subject`` (``"<workspace_id>:<user_id>"``) binds the token to the actor it was issued for, so a leaked
    token cannot be replayed by another account.
    """
    check_upstream_url(url)
    body = json.dumps(
        {"u": url, "e": int((now or time.time()) + TOKEN_TTL_SECONDS), "s": subject},
        separators=(",", ":"),
    )
    payload = b64url_encode(body.encode())
    sig = hmac.new(_key(secret), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_media_token(
    secret: str, token: str, *, now: float | None = None, subject: str = ""
) -> str:
    """Return the bound upstream URL for a valid, unexpired token issued for ``subject``; raise MediaError otherwise."""
    payload, _, sig = token.partition(".")
    if not hmac.compare_digest(
        hmac.new(_key(secret), payload.encode(), hashlib.sha256).hexdigest(), sig
    ):
        raise MediaError("bad_token")
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        url, expires, bound = data["u"], int(data["e"]), data.get("s", "")
    except (ValueError, KeyError, TypeError, AttributeError):
        raise MediaError("bad_token") from None
    if not isinstance(bound, str) or not hmac.compare_digest(bound.encode(), subject.encode()):
        raise MediaError("bad_token")
    if (now or time.time()) > expires:
        raise MediaError("expired")
    return check_upstream_url(str(url))


def clean_image_url(value: str | None) -> str | None:
    """Return ``value`` stripped when it is an https URL of at most 2048 chars without userinfo, else ``None``.

    Never raises: a malformed feed image must not fail its record. Host allowlisting is separate
    (``check_upstream_url`` at signing time).
    """
    if not value:
        return None
    value = value.strip()
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError:
        return None
    if (
        len(value) > 2048
        or parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or _CTRL.search(value)
    ):
        return None
    return value


def make_image_signer(
    config: AIExecutionConfig, secret: str, subject: str
) -> Callable[[str, bool], str | None]:
    """Build ``(url, local_only) -> signed proxy path | None`` for one actor.

    ``None`` when the owner flag is off, the record is ``local_only``, the host is not allowlisted or signing is
    unconfigured: a thumbnail is optional and must never fail a page.
    """

    def sign(url: str, local_only: bool) -> str | None:
        """Sign ``url`` for ``subject`` under the permission/allowlist rules above."""
        if not media_permitted(config, local_only=local_only):
            return None
        try:
            return f"{PROXY_PATH}?token={sign_media_url(secret, url, subject=subject)}"
        except MediaError:
            return None

    return sign
