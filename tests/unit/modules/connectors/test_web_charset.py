"""Web page decoding honours the response charset and never raises."""

import pytest

from modules.connectors._collection.web import _decode_page


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("text/html; charset=iso-8859-1", "café"),
        ('text/html; charset="ISO-8859-1"', "café"),
        ("text/html", "caf�"),  # no charset: UTF-8 with replacement
        ("text/html; charset=bogus-enc", "caf�"),  # unknown charset falls back, not raises
        (None, "caf�"),
    ],
)
def test_decode_page_charset(content_type, expected):
    assert _decode_page("café".encode("latin-1"), content_type) == expected


def test_decode_page_utf8_default():
    assert _decode_page("café".encode(), "text/html; charset=utf-8") == "café"
