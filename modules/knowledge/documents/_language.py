"""Feed language normalization shared by projections and normalized writes."""

# Keep in sync with the language options in apps/web feed-gadget.tsx.
FEED_LANGUAGES = frozenset(
    {"en", "vi", "fr", "de", "es", "pt", "it", "ru", "ja", "ko", "zh", "id", "th"}
)


def normalize_document_language(raw: object) -> str | None:
    """Return the allowlisted primary language subtag ("en-US" -> "en") or None when unknown/invalid."""
    if not isinstance(raw, str):
        return None
    primary = raw.strip().lower().replace("_", "-").split("-", 1)[0]
    return primary if primary in FEED_LANGUAGES else None


def _provenance_language(provenance: object) -> str | None:
    """Extract the normalized language from ingestion provenance ``metadata.language``."""
    metadata = provenance.get("metadata") if isinstance(provenance, dict) else None
    return normalize_document_language(
        metadata.get("language") if isinstance(metadata, dict) else None
    )
