"""Bounded mapping of a public Telegram channel web preview (``t.me/s/<username>``) to records.

No Bot API, token or admin right is used: the one public HTML page is fetched through the gate and
parsed in memory. Media bytes are never downloaded; only the first photo (else video thumbnail) URL is
kept as ``image_url`` after the shared feed-image URL shape check.
"""

# ValueError is the executor's stable classification for invalid provider payloads.

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Literal

from core.media_proxy import clean_image_url
from modules.connectors.providers.rest import Fetched
from modules.connectors.providers.social_walk import post_title
from modules.ingestion.public import IngestionRecord
from modules.knowledge.documents.public import ProviderRecordMetadata

_MAX_RECORDS = 100
_MAX_TEXT = 20_000
_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}")
_BG_IMAGE = re.compile(r"background-image:\s*url\(\s*['\"]?([^'\")\s]+)['\"]?\s*\)")
_EMOJI_PATH = "/img/emoji/"


@dataclass(frozen=True)
class TelegramPublicCollectionPage:
    """One bounded public channel snapshot; the preview has no continuation cursor."""

    records: tuple[IngestionRecord, ...]
    cursor_after: str | None
    coverage: Literal["returned_snapshot", "truncated"]


def _utc(value: object) -> datetime | None:
    """Parse an aware ISO timestamp from the page into UTC, or ``None`` when absent or naive."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _background_image(style: str | None) -> str | None:
    """Return the first non-emoji ``background-image`` URL in a style attribute that is a safe https URL."""
    for match in _BG_IMAGE.finditer(style or ""):
        if _EMOJI_PATH in match.group(1):
            continue
        cleaned = clean_image_url(match.group(1))
        if cleaned:
            return cleaned
    return None


@dataclass
class _Post:
    """Mutable scratch state for one ``tgme_widget_message`` while the page is parsed."""

    data_post: str
    text: list[str] = field(default_factory=list)
    time: str | None = None
    photos: list[str] = field(default_factory=list)
    videos: list[str] = field(default_factory=list)


class _PreviewParser(HTMLParser):
    """Single-pass extractor of channel title and post fields from the public preview HTML.

    Tolerant by construction (stdlib ``HTMLParser`` never raises on malformed markup); only the
    documented ``tgme_*`` class tokens are read and nothing is executed or fetched.
    """

    def __init__(self) -> None:
        """Start with no posts, no header and no open capture."""
        super().__init__(convert_charrefs=True)
        self.posts: list[_Post] = []
        self.title_parts: list[str] = []
        self.has_header = False
        self.og_title: str | None = None
        self._post: _Post | None = None
        self._capture: str | None = None  # "text" or "title"
        self._capture_tag = ""
        self._depth = 0
        self._in_date = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Route one start tag: new post, text/title capture, date time, or image style."""
        a = {k: v or "" for k, v in attrs}
        classes = a.get("class", "").split()
        if tag == "meta" and a.get("property") == "og:title":
            self.og_title = a.get("content") or None
        if tag == "div" and "tgme_widget_message" in classes and a.get("data-post"):
            self._post = _Post(a["data-post"])
            self.posts.append(self._post)
            self._capture = None
            return
        if self._capture is not None:
            if tag == self._capture_tag:
                self._depth += 1
            elif tag == "br" and self._capture == "text" and self._post is not None:
                self._post.text.append("\n")
            return
        if "tgme_channel_info_header_title" in classes:
            self.has_header = True
            self._start_capture("title", tag)
        elif self._post is None:
            return
        elif "tgme_widget_message_text" in classes:
            self._start_capture("text", tag)
        elif "tgme_widget_message_date" in classes:
            self._in_date = True
        elif tag == "time" and self._in_date and self._post.time is None:
            self._post.time = a.get("datetime")
        elif "tgme_widget_message_photo_wrap" in classes:
            self._post.photos.append(a.get("style", ""))
        elif "tgme_widget_message_video_thumb" in classes:
            self._post.videos.append(a.get("style", ""))

    def _start_capture(self, kind: str, tag: str) -> None:
        """Begin collecting text inside ``tag`` until its matching end tag."""
        self._capture, self._capture_tag, self._depth = kind, tag, 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Treat ``<br/>`` and friends like a start tag without opening a capture depth."""
        if tag == "br" and self._capture == "text" and self._post is not None:
            self._post.text.append("\n")
        elif tag != self._capture_tag:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        """Close the open capture at its matching end tag and the date anchor at ``</a>``."""
        if tag == "a":
            self._in_date = False
        if self._capture is not None and tag == self._capture_tag:
            self._depth -= 1
            if self._depth == 0:
                self._capture = None

    def handle_data(self, data: str) -> None:
        """Append text to the open title or post-text capture."""
        if self._capture == "title":
            self.title_parts.append(data)
        elif self._capture == "text" and self._post is not None:
            self._post.text.append(data)


def _first_image(post: _Post) -> str | None:
    """Return the first photo URL of a post, else its video thumbnail URL, else ``None``."""
    for style in (*post.photos, *post.videos):
        image = _background_image(style)
        if image:
            return image
    return None


def _post_record(
    message: _Post, username: str, title: str, collected_at: datetime
) -> IngestionRecord | None:
    """Map one parsed ``tgme_widget_message`` to a record, or ``None`` when it is unusable.

    A post is skipped when its ``data-post`` does not belong to ``username`` / lacks a numeric id or
    its time is missing or naive. Photo-only posts get a ``[photo]`` placeholder text so they stay
    non-empty. Output is bounded: text 20k characters, author 255.
    """
    channel, _, post_id = message.data_post.partition("/")
    if channel.lower() != username.lower() or not post_id.isdigit() or len(post_id) > 18:
        return None
    observed_at = _utc(message.time)
    if observed_at is None:
        return None
    # Collapse whitespace runs inside lines; keep the <br> line breaks.
    text = "\n".join(
        line for raw in "".join(message.text).split("\n") if (line := " ".join(raw.split()))
    )
    image = _first_image(message)
    if not text:
        text = "[photo]" if image else "[media]"
    truncated = len(text) > _MAX_TEXT
    author = title[:255]
    canonical_url = f"https://t.me/{username}/{post_id}"
    identity = f"telegram_public:{username}:{post_id}"
    source_fields: dict[str, object] = {
        "author": author,
        "canonical_url": canonical_url,
        "created_at": observed_at.isoformat(),
        "channel_username": username,
    }
    if image:
        source_fields["image_url"] = image
    metadata: dict[str, object] = {
        "title": post_title(text) or author,
        "provider": "telegram_public",
        "source_id": identity,
        "author": author,
        "canonical_url": canonical_url,
        # Top-level publish time feeds Document.published_at (gadget timestamps/ordering).
        "published_at": observed_at.isoformat(),
        "content_truncated": truncated,
        "provider_record": ProviderRecordMetadata(
            provider="telegram_public",
            identity=identity,
            timestamp_basis="provider_published",
            coverage="returned_snapshot",
            content_truncated=truncated,
            source_fields=source_fields,
        ).model_dump(mode="json", exclude_none=True),
    }
    return IngestionRecord(
        provider_id=identity,
        content=text[:_MAX_TEXT],
        observed_at=observed_at,
        metadata=metadata,
        collected_at=collected_at,
    )


def map_telegram_public_html(
    body: bytes, username: str, *, collected_at: datetime
) -> TelegramPublicCollectionPage:
    """Parse a ``t.me/s/<username>`` page into the newest (up to 100) records.

    Raises:
        ValueError: ``telegram_public_response_invalid`` when the body is not UTF-8 or has neither
            posts nor a channel header; ``telegram_public_channel_unavailable`` when the header
            exists but no post is visible (private, empty or non-previewable channel).
    """
    try:
        html = body.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("telegram_public_response_invalid") from None
    parser = _PreviewParser()
    parser.feed(html)
    parser.close()
    if not parser.posts:
        raise ValueError(
            "telegram_public_channel_unavailable"
            if parser.has_header
            else "telegram_public_response_invalid"
        )
    title = " ".join("".join(parser.title_parts).split()) or (parser.og_title or "").strip()
    title = title or username
    records = [
        record
        for post in parser.posts
        if (record := _post_record(post, username, title, collected_at)) is not None
    ]
    # The preview lists oldest first; keep the newest 100.
    return TelegramPublicCollectionPage(tuple(records[-_MAX_RECORDS:]), None, "returned_snapshot")


async def collect_telegram_public_channel(
    username: str,
    *,
    collected_at: datetime,
    fetch_bytes: Callable[[str], Awaitable[Fetched]],
) -> TelegramPublicCollectionPage:
    """Fetch ``https://t.me/s/<username>`` once through the gate and map it.

    Raises:
        ValueError: ``provider_scope_invalid`` for a naive clock or invalid username,
            plus the mapper's ``telegram_public_*`` codes. Transport errors propagate from the gate.
    """
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("provider_scope_invalid")
    if _USERNAME.fullmatch(username) is None:
        raise ValueError("provider_scope_invalid")
    response = await fetch_bytes(f"https://t.me/s/{username}")
    if response.body is None:
        raise ValueError("telegram_public_response_invalid")
    return map_telegram_public_html(response.body, username, collected_at=collected_at)
