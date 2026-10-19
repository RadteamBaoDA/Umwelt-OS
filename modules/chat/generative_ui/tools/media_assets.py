"""Resolve destination imagery and local inline SVG data-URI assets under privacy fences."""

import re
from concurrent.futures import ThreadPoolExecutor
from html import escape, unescape
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx

HEADERS: dict[str, str] = {
    "User-Agent": "OpenGenerativeUI/1.0 (https://github.com/CopilotKit/OpenIntelligentUI)"
}
IMAGE_HOSTS: set[str] = {"upload.wikimedia.org", "thumb.wikimedia.org"}


class _ToolDescriptor:
    """Callable tool wrapper exposing `.name`, `.description`, `.func`, and `.invoke`."""

    def __init__(self, func: Any) -> None:
        """Bind a Python function with tool metadata."""
        self.func = func
        self.name = func.__name__
        self.description = (func.__doc__ or "").strip()
        self.__doc__ = func.__doc__
        self.__name__ = func.__name__

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Invoke the underlying function directly."""
        return self.func(*args, **kwargs)

    def invoke(self, input_data: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        """Invoke the underlying function using a dictionary of keyword arguments."""
        merged = {**(input_data or {}), **kwargs}
        return self.func(**merged)


def build_local_svg_data_uri(title: str, subtitle: str = "Local-only preview") -> str:
    """Generate a self-contained inline SVG `data:image/svg+xml` URI without external network access.

    Args:
        title: Primary label rendered inside the SVG card.
        subtitle: Secondary badge text.

    Returns:
        URL-encoded `data:image/svg+xml;utf8,...` string safe for `img-src data:`.
    """
    safe_title = escape((title or "Location").strip()[:36])
    safe_sub = escape((subtitle or "Local-only preview").strip()[:48])
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 140" width="320" height="140">'
        '<rect width="320" height="140" rx="12" fill="#E1F5EE" stroke="#0F6E56" stroke-width="1"/>'
        '<circle cx="48" cy="70" r="22" fill="#0F6E56" opacity="0.15"/>'
        '<circle cx="48" cy="70" r="8" fill="#0F6E56"/>'
        f'<text x="86" y="64" font-family="system-ui, sans-serif" font-size="14" font-weight="600" fill="#085041">{safe_title}</text>'
        f'<text x="86" y="86" font-family="system-ui, sans-serif" font-size="12" fill="#0F6E56">{safe_sub}</text>'
        "</svg>"
    )
    return "data:image/svg+xml;utf8," + quote(svg, safe=" ,:;=/()")


def validate_image_url(url: str, *, local_only: bool = False) -> str:
    """Validate that an image URL honors the local_only fence and trusted host allowlist.

    Args:
        url: Candidate image URL or `data:image/svg+xml` URI.
        local_only: When True, rejects all `http`/`https` external tracking or image URLs.

    Returns:
        The validated URL string.

    Raises:
        ValueError: When `local_only=True` and the URL is external, or when the host is untrusted.
    """
    cleaned = (url or "").strip()
    if cleaned.startswith("data:image/svg+xml"):
        return cleaned
    if local_only:
        raise ValueError(
            "External image URLs are forbidden when local_only=True; use inline SVG data URIs."
        )
    parsed = urlsplit(cleaned)
    if parsed.scheme != "https" or parsed.hostname not in IMAGE_HOSTS:
        raise ValueError("No supported destination image was available.")
    return cleaned


def fetch_card(
    title: str,
    client: httpx.Client,
    *,
    local_only: bool = False,
) -> dict[str, Any]:
    """Fetch a verified Wikimedia Commons image card or return a local SVG card when fenced.

    Args:
        title: English Wikipedia article title.
        client: Configured `httpx.Client`.
        local_only: When True, never performs network requests and returns an inline SVG data-URI card.

    Returns:
        Dictionary with `status`, `place`, `image_url`, `artist`, `license`, `credit_url`,
        `article_url`, and `note`.

    Raises:
        ValueError: When external image hosts, filenames, or licenses fail verification.
    """
    if local_only:
        return {
            "status": "available",
            "place": title,
            "image_url": build_local_svg_data_uri(title),
            "artist": "Umwelt-OS Local SVG Generator",
            "license": "CC0 1.0",
            "credit_url": "about:blank",
            "article_url": "about:blank",
            "note": "Generated locally via inline SVG data URI under local_only privacy fence.",
        }

    summary = client.get(
        "https://en.wikipedia.org/api/rest_v1/page/summary/" + quote(title, safe="")
    )
    summary.raise_for_status()
    data = summary.json()
    image = data.get("thumbnail", {}).get("source", "")
    original = data.get("originalimage", {}).get("source", "")
    if any(
        urlsplit(url).scheme != "https" or urlsplit(url).hostname not in IMAGE_HOSTS
        for url in [image, original]
    ):
        raise ValueError("No supported destination image was available.")

    def commons_filename(url: str) -> str:
        match = re.fullmatch(
            r"/wikipedia/commons/(?:thumb/)?[0-9a-f]/[0-9a-f]{2}/([^/]+)(?:/[^/]+)?",
            urlsplit(url).path,
        )
        if not match:
            raise ValueError("Image is not backed by Wikimedia Commons.")
        return unquote(match.group(1)).replace(" ", "_")

    filename = commons_filename(original)
    if commons_filename(image) != filename:
        raise ValueError("Photo and attribution refer to different files.")

    metadata = client.get(
        "https://commons.wikimedia.org/w/api.php",
        params={
            "action": "query",
            "format": "json",
            "prop": "imageinfo",
            "iiprop": "extmetadata",
            "titles": "File:" + filename,
        },
    )
    metadata.raise_for_status()
    pages: dict[str, Any] = metadata.json().get("query", {}).get("pages", {})
    first_page: dict[str, Any] = next(iter(pages.values()), {})
    imageinfo_list: list[dict[str, Any]] = first_page.get("imageinfo") or [{}]
    info: dict[str, Any] = imageinfo_list[0].get("extmetadata", {})

    def plain(key: str) -> str:
        raw_val = str(info.get(key, {}).get("value", ""))
        return unescape(re.sub(r"<[^>]*>", "", raw_val)).strip()[:500]

    license_name = plain("LicenseShortName")
    artist = plain("Artist")
    if not artist or not license_name or not (
        license_name.startswith("CC") or license_name == "Public domain"
    ):
        raise ValueError("Reusable image attribution was unavailable.")
    return {
        "status": "available",
        "place": title,
        "image_url": image,
        "artist": artist,
        "license": license_name,
        "credit_url": "https://commons.wikimedia.org/wiki/File:" + quote(filename, safe=""),
        "article_url": "https://en.wikipedia.org/wiki/" + quote(title, safe=""),
        "note": (
            "Photo from this Wikipedia article; confirm the pictured subject fits the destination. "
            "It is not a live camera view."
        ),
    }


def _get_trip_stop_images(
    wikipedia_titles: list[str],
    *,
    local_only: bool = False,
) -> dict[str, Any]:
    """Fetch destination photos with creator, license and source links for itinerary cards.

    Pass 1–8 precise English Wikipedia article titles (for example Half Moon Bay, California).
    When `local_only=True`, returns safe inline SVG data-URI cards without any external network calls.
    """
    if not 1 <= len(wikipedia_titles) <= 8 or any(
        not t.strip() or len(t) > 160 for t in wikipedia_titles
    ):
        raise ValueError("Provide 1–8 nonempty Wikipedia titles, at most 160 characters each.")

    if local_only:
        return {
            "source": "Local inline SVG generator (local_only)",
            "local_only": True,
            "stops": [
                {
                    "status": "available",
                    "place": title.strip(),
                    "image_url": build_local_svg_data_uri(title.strip()),
                    "artist": "Umwelt-OS Local SVG Generator",
                    "license": "CC0 1.0",
                    "credit_url": "about:blank",
                    "article_url": "about:blank",
                    "note": "Inline SVG preview generated locally under privacy fence.",
                }
                for title in wikipedia_titles
            ],
        }

    def fetch(title: str) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=12, headers=HEADERS) as client:
                return fetch_card(title.strip(), client)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
            return {
                "status": "unavailable",
                "place": title,
                "reason": (
                    "The photo source or reusable attribution could not be retrieved. "
                    "Use a text card for this stop."
                ),
            }

    with ThreadPoolExecutor(max_workers=4) as pool:
        return {
            "source": "Wikipedia / Wikimedia Commons",
            "local_only": False,
            "stops": list(pool.map(fetch, wikipedia_titles)),
        }


get_trip_stop_images = _ToolDescriptor(_get_trip_stop_images)
