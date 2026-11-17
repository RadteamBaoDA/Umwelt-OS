"""Dependency-free CPU helpers for the browser collector (hashing and JSON sizing of large page bodies).

Kept apart from ``crawl`` (which needs the optional ``crawlee`` extra) so they are importable and
testable everywhere; ``crawl`` runs them through ``asyncio.to_thread`` to keep the loop free.
"""

import base64


def page_digest_and_raw(body: bytes) -> tuple[str, str]:
    """Return ``(sha256 hex, base64 text)`` of one fetched page body (CPU-bound for up to ~10 MB; run in a thread)."""
    import hashlib

    return hashlib.sha256(body).hexdigest(), base64.b64encode(body).decode("ascii")


def result_digest(pages: list[dict[str, object]]) -> str:
    """Return the canonical SHA-256 of the page records without ``raw_content`` (large json.dumps; run in a thread)."""
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(
            [{k: v for k, v in page.items() if k != "raw_content"} for page in pages],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def wire_size(pages: list[dict[str, object]]) -> int:
    """Return the UTF-8 byte length of the compact JSON response envelope body (run in a thread when large)."""
    import json

    return len(json.dumps(pages, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
