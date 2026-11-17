"""Origin pinning, FHIR observation and Home Assistant state mapping for local-service providers."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from modules.connectors.providers import local_services as ls

COLLECTED = datetime(2026, 4, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://Host.Local", ("http", "host.local", 80)),
        ("https://host.local", ("https", "host.local", 443)),
        ("http://host.local:8123/api", ("http", "host.local", 8123)),
    ],
)
def test_origin_normalizes_scheme_host_and_default_port(
    url: str, expected: tuple[str, str, int]
) -> None:
    assert ls._origin(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "ftp://host",
        "http://",
        "http://u:p@host",
        "http://host:0",
        "http://host:99999",
        "http://host:abc",
        "host.local",
    ],
)
def test_origin_rejects_unsafe_origins(url: str) -> None:
    with pytest.raises(ls.LocalServiceError, match="configured_origin_invalid"):
        ls._origin(url)


def test_url_for_origin_keeps_path_and_query_but_drops_fragment_rules() -> None:
    assert ls._url_for_origin("http://h:8123", "http://h:8123/api/states?x=1") == (
        "http://h:8123/api/states?x=1",
        "http",
        "h",
        8123,
    )
    assert ls._url_for_origin("http://h:8123", "http://h:8123")[0] == "http://h:8123/"


@pytest.mark.parametrize(
    "configured,request_url,error",
    [
        ("http://h:8123", "http://h:9000/x", "origin_changed"),
        ("http://h:8123", "https://h:8123/x", "origin_changed"),
        ("http://h:8123?q=1", "http://h:8123/x", "configured_origin_invalid"),
        ("http://h:8123", "http://h:8123/x#frag", "request_url_invalid"),
    ],
)
def test_url_for_origin_rejects_origin_drift_and_ambiguous_urls(
    configured: str, request_url: str, error: str
) -> None:
    with pytest.raises(ls.LocalServiceError, match=error):
        ls._url_for_origin(configured, request_url)


def test_decode_json_keeps_decimals_and_sanitizes_errors() -> None:
    assert ls._decode_json(b'{"v": 1.10}') == {"v": Decimal("1.10")}
    for bad in (b"{", b"\xff"):
        with pytest.raises(ls.LocalServiceError, match="response_not_json"):
            ls._decode_json(bad)


def test_instant_requires_aware_iso_timestamps() -> None:
    assert ls._instant("2026-01-01T02:00:00+02:00") == datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ls.LocalServiceError, match="observation_time_missing"):
        ls._instant(None)
    with pytest.raises(ls.LocalServiceError, match="observation_time_invalid"):
        ls._instant("2026-01-01T00:00:00")
    with pytest.raises(ls.LocalServiceError, match="observation_time_invalid"):
        ls._instant("yesterday")


def _observation(**over: Any) -> dict[str, Any]:
    resource: dict[str, Any] = {
        "resourceType": "Observation",
        "id": "obs-1",
        "subject": {"reference": "Patient/p1"},
        "valueQuantity": {"value": Decimal("98.60"), "unit": "F"},
        "effectiveDateTime": "2026-03-01T10:00:00Z",
        "meta": {"versionId": "3"},
        "code": {
            "coding": [
                {"system": "loinc", "code": "8310-5", "display": "Temp", "extra": "drop"},
                "junk",
            ]
        },
    }
    resource.update(over)
    return resource


def _fhir(resource: object):
    return ls.map_fhir_observation(resource, patient_id="p1", collected_at=COLLECTED)


def test_fhir_observation_preserves_exact_value_unit_and_coding() -> None:
    record = _fhir(_observation())
    assert record is not None
    assert record.provider_id == "obs-1"
    assert record.content == "98.60 F"
    assert record.version == "3"
    assert record.observed_at == datetime(2026, 3, 1, 10, tzinfo=UTC)
    assert record.metadata["coding"] == [{"system": "loinc", "code": "8310-5", "display": "Temp"}]
    assert record.metadata["provider_record"]["provider"] == "health"


def test_fhir_observation_time_falls_back_to_period_then_issued() -> None:
    period = _observation(effectiveDateTime=None, effectivePeriod={"start": "2026-02-01T00:00:00Z"})
    assert _fhir(period).observed_at == datetime(2026, 2, 1, tzinfo=UTC)  # type: ignore[union-attr]
    issued = _observation(effectiveDateTime=None, issued="2026-01-01T00:00:00Z")
    assert _fhir(issued).observed_at == datetime(2026, 1, 1, tzinfo=UTC)  # type: ignore[union-attr]
    with pytest.raises(ls.LocalServiceError, match="observation_time_missing"):
        _fhir(_observation(effectiveDateTime=None))


@pytest.mark.parametrize(
    "resource",
    [
        "x",
        {"resourceType": "Patient"},
        _observation(subject={"reference": "Patient/other"}),
        _observation(subject=None),
        _observation(id=""),
        _observation(valueQuantity=None),
        _observation(valueQuantity={"value": True, "unit": "F"}),
        _observation(valueQuantity={"value": "5", "unit": "F"}),
        _observation(valueQuantity={"value": 5, "unit": None}),
        _observation(valueQuantity={"value": float("nan"), "unit": "F"}),
    ],
)
def test_fhir_observation_skips_other_patients_and_unsupported_values(resource: object) -> None:
    assert _fhir(resource) is None


def test_fhir_observation_requires_aware_collection_time() -> None:
    with pytest.raises(ls.LocalServiceError, match="collection_time_invalid"):
        ls.map_fhir_observation(_observation(), patient_id="p1", collected_at=datetime(2026, 1, 1))


def _cursor(**over: Any) -> str:
    cursor = {"v": 1, "provider": "home_assistant", "scope_hash": "h", "index": 2}
    cursor.update(over)
    return json.dumps(cursor)


@pytest.mark.parametrize(
    "cursor,expected",
    [
        (None, 0),
        (_cursor(), 2),
        (_cursor(index=5), 0),
        (_cursor(index=-1), 0),
        (_cursor(index=True), 0),
        (_cursor(scope_hash="other"), 0),
        (_cursor(provider="x"), 0),
        (_cursor(v=2), 0),
        (_cursor(v=True), 0),
        ("not json", 0),
        ("[]", 0),
        ("x" * (ls.MAX_HA_CURSOR_BYTES + 1), 0),
    ],
)
def test_home_assistant_resume_index_only_honors_exact_scope(
    cursor: str | None, expected: int
) -> None:
    assert ls._ha_resume_index(cursor, "h", 5) == expected


def test_home_assistant_state_record_keeps_selected_bounded_attributes() -> None:
    state = {
        "entity_id": "sensor.t",
        "state": "21.5",
        "last_updated": "2026-03-01T10:00:00+00:00",
        "attributes": {
            "unit_of_measurement": "C",
            "friendly_name": "N" * 600,
            "latitude": 1.5,
            "secret": "no",
            "longitude": True,
        },
    }
    record = ls._ha_state_record(state, "sensor.t", COLLECTED)
    assert record.content == "21.5"
    assert record.version == "2026-03-01T10:00:00+00:00"
    assert record.metadata["unit_of_measurement"] == "C"
    assert len(record.metadata["friendly_name"]) == 512
    assert record.metadata["latitude"] == "1.5"
    assert "secret" not in record.metadata and "longitude" not in record.metadata


@pytest.mark.parametrize("state", ["x", {"entity_id": "other"}])
def test_home_assistant_state_record_rejects_wrong_entity(state: object) -> None:
    with pytest.raises(ls.LocalServiceError, match="home_assistant_response_invalid"):
        ls._ha_state_record(state, "sensor.t", COLLECTED)


def test_home_assistant_state_record_requires_observation_time() -> None:
    with pytest.raises(ls.LocalServiceError, match="observation_time_missing"):
        ls._ha_state_record({"entity_id": "sensor.t", "state": None}, "sensor.t", COLLECTED)
