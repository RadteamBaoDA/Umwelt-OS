"""Unit tests for media_assets (ported from trip_images.py) and local_only privacy fences."""

from typing import Any

import httpx
import pytest

from modules.chat.generative_ui.tools.media_assets import (
    build_local_svg_data_uri,
    fetch_card,
    get_trip_stop_images,
    validate_image_url,
)


def _client_for(
    image: str = "https://upload.wikimedia.org/wikipedia/commons/a/ab/Test.jpg",
    license_name: str = "CC BY-SA 4.0",
    original: str | None = None,
) -> httpx.Client:
    """Create a mock httpx.Client simulating Wikipedia REST and Wikimedia Commons APIs."""
    def _handle(request: httpx.Request) -> httpx.Response:
        if "summary" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "thumbnail": {"source": image},
                    "originalimage": {"source": original or image},
                },
            )
        return httpx.Response(
            200,
            json={
                "query": {
                    "pages": {
                        "1": {
                            "imageinfo": [
                                {
                                    "extmetadata": {
                                        "Artist": {
                                            "value": '<a href="example">A Photographer</a>'
                                        },
                                        "LicenseShortName": {"value": license_name},
                                    }
                                }
                            ]
                        }
                    }
                }
            },
        )

    return httpx.Client(transport=httpx.MockTransport(_handle))


def test_returns_verified_host_and_plain_attribution() -> None:
    """Verify fetch_card validates Wikimedia Commons host and strips HTML from attribution."""
    with _client_for() as client:
        result = fetch_card("Half Moon Bay, California", client)
    assert result["status"] == "available"
    assert result["artist"] == "A Photographer"
    assert result["license"] == "CC BY-SA 4.0"
    assert result["credit_url"].startswith("https://commons.wikimedia.org/wiki/File:")


def test_rejects_untrusted_image_host_and_unknown_license() -> None:
    """Verify fetch_card rejects non-Wikimedia hosts and non-reusable licenses."""
    with (
        _client_for(image="https://untrusted.example/photo.jpg") as client,
        pytest.raises(ValueError, match="supported"),
    ):
        fetch_card("Place", client)
    with (
        _client_for(license_name="All rights reserved") as client,
        pytest.raises(ValueError, match="attribution"),
    ):
        fetch_card("Place", client)


def test_bounds_photo_requests() -> None:
    """Verify get_trip_stop_images enforces the 1..8 title bound."""
    with pytest.raises(ValueError, match="1–8"):
        get_trip_stop_images.invoke({"wikipedia_titles": ["Place"] * 9})


def test_original_image_can_itself_be_a_resized_thumbnail() -> None:
    """Verify commons_filename handles thumbnail paths in originalimage."""
    image = "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Coastal_view.jpg/3840px-Coastal_view.jpg"
    with _client_for(image=image) as client:
        result = fetch_card("Coast", client)
    assert result["credit_url"].endswith("File:Coastal_view.jpg")


def test_rejects_local_wikipedia_repository_images() -> None:
    """Verify non-Commons /wikipedia/en/ files are rejected."""
    with (
        _client_for(image="https://upload.wikimedia.org/wikipedia/en/a/ab/Test.jpg") as client,
        pytest.raises(ValueError, match="Wikimedia Commons"),
    ):
        fetch_card("Place", client)


def test_rejects_mismatched_photo_and_attribution_file() -> None:
    """Verify mismatched thumbnail and original filenames are rejected."""
    with _client_for(
        original="https://upload.wikimedia.org/wikipedia/commons/a/ab/Other.jpg"
    ) as client, pytest.raises(ValueError, match="different files"):
        fetch_card("Place", client)


def test_local_only_fence_returns_inline_svg_without_network_calls() -> None:
    """Verify local_only=True generates inline SVG data URIs and rejects external tracking URLs."""
    def _fail_if_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected network request under local_only fence: {request.url}")

    with httpx.Client(transport=httpx.MockTransport(_fail_if_called)) as client:
        card = fetch_card("Kyoto", client, local_only=True)
    assert card["status"] == "available"
    assert card["image_url"].startswith("data:image/svg+xml;utf8,")

    batch: dict[str, Any] = get_trip_stop_images.invoke(
        {"wikipedia_titles": ["Kyoto", "Osaka"], "local_only": True}
    )
    assert batch["local_only"] is True
    assert len(batch["stops"]) == 2
    assert all(s["image_url"].startswith("data:image/svg+xml;utf8,") for s in batch["stops"])

    with pytest.raises(ValueError, match="local_only"):
        validate_image_url("https://upload.wikimedia.org/wikipedia/commons/a/ab/Test.jpg", local_only=True)
    assert validate_image_url(build_local_svg_data_uri("Safe"), local_only=True).startswith(
        "data:image/svg+xml"
    )
