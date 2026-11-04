"""Content hashing, with an off-loop variant for large inputs."""

import hashlib

from core.heavy_work import to_thread_joined


def content_hash(content: str) -> str:
    """Return the SHA-256 digest of UTF-8 encoded document content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


_INLINE_HASH_MAX_CHARS = 65_536


async def content_hash_async(content: str) -> str:
    """Return ``content_hash(content)`` without blocking the loop on very large documents.

    Content up to 64 Ki characters is hashed inline (a thread handoff would cost more than the
    ~50 us hash); larger content, up to the multi-million-character document bound, is hashed in a
    joined worker thread. The digest is identical in both paths; no SQL or ordering is involved.
    """
    if len(content) <= _INLINE_HASH_MAX_CHARS:
        return content_hash(content)
    return await to_thread_joined(content_hash, content)
