"""Live-map response models keep the exact JSON the handlers already build."""

from modules.dashboard.live_credentials import LiveCredentials, public_live_credentials
from modules.dashboard.live_schemas import LiveConfigResponse, LiveMapEventsResponse


def test_events_response_preserves_shape_and_unknown_keys() -> None:
    """Declared event fields round-trip unchanged and undeclared provider keys are not dropped."""
    payload = {
        "items": [
            {
                "id": "1",
                "category": "usgs",
                "title": "Quake",
                "observed_at": None,
                "latitude": 1.5,
                "longitude": None,
                "url": None,
                "source": "USGS",
                "summary": None,
                "sources": [{"id": None, "url": "https://example.com"}],
                "extra_key": 3,
            }
        ],
        "fetched_at": "2026-11-12T00:00:00+00:00",
        "truncated": False,
    }
    assert LiveMapEventsResponse.model_validate(payload).model_dump() == payload


def test_config_response_matches_public_live_credentials() -> None:
    """The config model accepts the real ``public_live_credentials`` output without changing it."""
    value = public_live_credentials(LiveCredentials(revision=3), is_operator=False)
    assert LiveConfigResponse.model_validate(value).model_dump() == value
