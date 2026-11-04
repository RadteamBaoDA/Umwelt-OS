"""Tests for the owner display-name derivation and normalization helpers."""
import pytest

from modules.settings.display_name import derive_display_name, normalize_display_name


@pytest.mark.parametrize(("email", "expected"), [
    ("vpacs.demo@x.com", "Vpacs Demo"),
    ("jo_ann-lee+tag@x.com", "Jo Ann Lee Tag"),
    ("..a@x.com", "A"),
    (None, "Owner"),
    ("", "Owner"),
    ("@x.com", "Owner"),
])
def test_derive_display_name(email: str | None, expected: str) -> None:
    """Derivation splits on . _ - +, capitalises, and never returns empty."""
    assert derive_display_name(email) == expected


def test_derive_display_name_truncates() -> None:
    """Long local parts are cut to 80 characters."""
    assert len(derive_display_name("a" * 200 + "@x.com")) == 80


def test_normalize_display_name() -> None:
    """Names are trimmed, blank maps to None, control chars and overlength are rejected."""
    assert normalize_display_name("  Ann ") == "Ann"
    assert normalize_display_name("   ") is None
    with pytest.raises(ValueError):
        normalize_display_name("a\x00b")
    with pytest.raises(ValueError):
        normalize_display_name("a" * 81)
