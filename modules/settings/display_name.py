"""Owner display-name rules shared by preferences, first-run setup and the 0008 migration backfill."""

import re

MAX_DISPLAY_NAME = 80
_NAME_SPLIT = re.compile(r"[._+\-]+")


def normalize_display_name(value: str | None) -> str | None:
    """Trim a user-supplied name; blank becomes None.

    Raises ValueError for control characters or more than 80 characters, so callers (pydantic
    validators) surface a 422 instead of silently truncating a typed name.
    """
    if value is None:
        return None
    value = value.strip()
    if any(ord(c) < 32 or ord(c) == 127 or 0x80 <= ord(c) <= 0x9F for c in value):
        raise ValueError("Display name must not contain control characters")
    if len(value) > MAX_DISPLAY_NAME:
        raise ValueError("Display name must be at most 80 characters")
    return value or None


def derive_display_name(email: str | None) -> str:
    """Derive a never-empty name from an email local part ("vpacs.demo@x" -> "Vpacs Demo").

    Split on . _ - +, capitalise each part, join with spaces, truncate to 80; no usable email -> "Owner".
    The 0008 migration mirrors this in SQL; keep both in step.
    """
    local = (email or "").split("@", 1)[0]
    parts = [part.capitalize() for part in _NAME_SPLIT.split(local) if part]
    return " ".join(parts)[:MAX_DISPLAY_NAME].rstrip() or "Owner"
