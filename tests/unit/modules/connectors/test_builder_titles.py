"""Record builders put the human-facing title (or location label) in metadata so chat sees it.

Each builder is exercised with a minimal provider payload; the expected values are the titles a
document shows instead of its provider id.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from modules.connectors.providers import (
    filings,
    google_api,
    local_services,
    social_feeds,
    social_networks,
    workspace_apps,
    world_data,
)
from modules.connectors.providers.social_walk import post_title
from modules.connectors.public import ConnectorConfig
from tests.unit.modules.connectors.p3_helpers import NOW

COLLECTED = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def test_post_title_is_first_nonblank_line_capped_with_ellipsis() -> None:
    """Only the first non-blank line is kept, trimmed; longer lines are cut to 120 characters with an ellipsis."""
    assert post_title("\n  Hello there \nmore") == "Hello there"
    assert post_title("   \n\n") == ""
    long_line = "x" * 300
    title = post_title(long_line)
    assert len(title) == 120 and title.endswith("…")
    assert post_title("y" * 120) == "y" * 120


def test_gmail_title_is_subject_with_placeholder_when_missing() -> None:
    """The mail subject is the title; a message without one gets the fixed placeholder."""
    message = {
        "id": "m1",
        "threadId": "t1",
        "historyId": "42",
        "internalDate": "1700000000000",
        "payload": {
            "headers": [{"name": "Subject", "value": "Quarterly plan"}],
            "mimeType": "text/plain",
            "body": {},
        },
    }
    assert google_api._gmail_message_record(message, NOW).metadata["title"] == "Quarterly plan"
    message["payload"] = {"headers": [], "mimeType": "text/plain", "body": {}}
    assert google_api._gmail_message_record(message, NOW).metadata["title"] == "(no subject)"


def test_calendar_title_is_event_summary() -> None:
    """The calendar event summary is the title, trimmed."""
    event = {
        "id": "e1",
        "summary": "  Standup  ",
        "start": {"dateTime": "2026-03-01T10:00:00+00:00"},
    }
    record = google_api._calendar_event_record("cal", event, NOW)
    assert record.metadata["title"] == "Standup"


def test_drive_title_is_file_name() -> None:
    """The Drive file name is the title."""
    file = {"id": "f1", "name": "notes.txt", "modifiedTime": "2026-02-01T00:00:00Z"}
    record = google_api._drive_file_record(file, b"hello", "text/plain", NOW)
    assert record.metadata["title"] == "notes.txt"


def test_bluesky_title_is_post_first_line() -> None:
    """A Bluesky post's title is the first line of its text."""
    record = social_feeds._text_record(
        "at://did/post/1",
        "Shipping today\nsecond line",
        NOW,
        NOW,
        canonical_url=None,
        author=None,
    )
    assert record.metadata["title"] == "Shipping today"


def test_mastodon_title_is_post_first_line() -> None:
    """A Mastodon status title is the first line of its plain text."""
    status = {
        "id": "123",
        "visibility": "public",
        "content": "<p>Hello fediverse</p><p>body</p>",
        "url": "https://mastodon.example/@a/123",
        "created_at": "2026-03-01T10:00:00+00:00",
        "account": {"acct": "a"},
    }
    record = social_networks._mastodon_status_record(status, COLLECTED)
    assert record is not None
    assert record.metadata["title"] == "Hello fediverse body"


def test_x_title_is_tweet_first_line() -> None:
    """An X post title is the first line of its text."""
    tweet = {"id": "9", "text": "First line\nsecond", "created_at": "2026-03-01T10:00:00+00:00"}
    record = social_networks._x_tweet_record(tweet, "acc", COLLECTED)
    assert record.metadata["title"] == "First line"


def test_slack_title_is_message_first_line() -> None:
    """A Slack message title is the first line of its text."""
    record = workspace_apps._slack_message_record(
        {"ts": "1700000000.000100", "text": "Deploy done\nlogs", "user": "U1"},
        "C1",
        COLLECTED,
        0,
        set(),
    )
    assert record is not None
    assert record.metadata["title"] == "Deploy done"


def test_fhir_title_prefers_coding_display_then_code_text() -> None:
    """The observation title is the first coding display, else code.text; absent both, the id remains the fallback."""
    resource = {
        "resourceType": "Observation",
        "id": "obs-1",
        "subject": {"reference": "Patient/p1"},
        "valueQuantity": {"value": 98.6, "unit": "F"},
        "effectiveDateTime": "2026-03-01T10:00:00Z",
        "code": {"coding": [{"system": "loinc", "code": "8310-5", "display": "Body temperature"}]},
    }
    record = local_services.map_fhir_observation(resource, patient_id="p1", collected_at=COLLECTED)
    assert record is not None and record.metadata["title"] == "Body temperature"

    resource["code"] = {"text": "Heart rate"}
    record = local_services.map_fhir_observation(resource, patient_id="p1", collected_at=COLLECTED)
    assert record is not None and record.metadata["title"] == "Heart rate"

    resource["code"] = {}
    record = local_services.map_fhir_observation(resource, patient_id="p1", collected_at=COLLECTED)
    assert record is not None and record.metadata["title"] == ""


@pytest.mark.parametrize(
    ("attributes", "expected"),
    [
        ({"friendly_name": "Living room temperature"}, "Living room temperature"),
        ({}, "sensor.t"),
    ],
)
def test_home_assistant_title_is_friendly_name_else_entity_id(
    attributes: dict[str, str], expected: str
) -> None:
    """A Home Assistant state is titled by its friendly name, falling back to the entity id."""
    state = {
        "entity_id": "sensor.t",
        "state": "21.5",
        "last_updated": "2026-03-01T10:00:00+00:00",
        "attributes": attributes,
    }
    record = local_services._ha_state_record(state, "sensor.t", COLLECTED)
    assert record.metadata["title"] == expected


def test_sec_edgar_title_is_company_name_and_form() -> None:
    """With the company name in the submissions payload, the title is '<name> <form>'; without it, the form alone."""
    payload = {
        "cik": "320193",
        "name": "Apple Inc.",
        "filings": {
            "recent": {
                "accessionNumber": ["0000320193-26-000010"],
                "filingDate": ["2026-01-02"],
                "form": ["8-K"],
                "primaryDocument": ["a8k.htm"],
                "primaryDocDescription": ["CURRENT REPORT"],
            }
        },
    }
    (record,) = filings.map_sec_edgar(payload, NOW, "0000320193")
    assert record.metadata["title"] == "Apple Inc. 8-K"
    del payload["name"]
    (bare,) = filings.map_sec_edgar(payload, NOW, "0000320193")
    assert bare.metadata["title"] == "8-K"


def test_open_meteo_region_is_configured_coordinates_label() -> None:
    """Weather measurements carry the configured location as 'lat,lon' rounded to two decimals."""
    config = ConnectorConfig(
        weather_latitude=21.0345,
        weather_longitude=105.8512,
        weather_timezone="UTC",
        weather_metrics=("temperature_2m",),
    )
    payload = {
        "timezone": "UTC",
        "hourly_units": {"temperature_2m": "C"},
        "hourly": {"time": ["2026-10-22T00:00"], "temperature_2m": [23.0]},
    }
    source = SimpleNamespace(id=UUID(int=7))
    (record,) = world_data._weather_records(
        source,  # type: ignore[arg-type]
        config,
        payload,
        NOW,
        coverage="returned_snapshot",
    )
    world = record.metadata["provider_record"]["world_data"]
    assert world["region"] == "21.03,105.85"
