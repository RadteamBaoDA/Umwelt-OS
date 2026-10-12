from datetime import UTC, datetime

import pytest

from modules.connectors.providers.hazards import map_nasa_eonet
from modules.connectors.providers.macro import ProviderPayloadError
from tests.unit.modules.connectors.p3_helpers import jfixture, wd

NOW = datetime(2026, 10, 12, 0, 0, tzinfo=UTC)


def test_nasa_eonet_events_use_latest_geometry():
    records = map_nasa_eonet(jfixture("nasa_eonet_open.json"), NOW)
    assert [r.provider_id for r in records] == ["EONET_25238", "EONET_25001"]
    fire = records[0]
    data = wd(fire)
    assert fire.observed_at == datetime(2026, 10, 10, 14, 58, tzinfo=UTC)
    assert data["value"] == 2000.0 and data["unit"] == "acres" and data["latitude"] == 42.337217
    assert data["provider_fields"]["categories"] == "Wildfires" and data["provider_fields"]["closed"] is None
    assert "irwin.doi.gov" in fire.content and "Wildfires" in fire.content


def test_nasa_eonet_closed_event_without_magnitude_is_missing_and_unsafe_link_dropped():
    ice = map_nasa_eonet(jfixture("nasa_eonet_open.json"), NOW)[1]
    data = wd(ice)
    assert data["value"] is None and data["quality"] == "missing" and data["unit"] == "event"
    assert data["provider_fields"]["closed"] == "2026-10-11T00:00:00Z" and "javascript" not in ice.content


@pytest.mark.parametrize("mutate", [
    lambda p: p.pop("events"),
    lambda p: [e.update(geometry=[]) for e in p["events"]],
    lambda p: [e["geometry"][-1].update(coordinates=[500, 1]) for e in p["events"]],
    lambda p: p["events"].append(dict(p["events"][0])),
    lambda p: p["events"].extend([p["events"][0]] * 51),
])
def test_nasa_eonet_hostile_payloads_rejected(mutate):
    payload = jfixture("nasa_eonet_open.json")
    mutate(payload)
    with pytest.raises(ProviderPayloadError):
        map_nasa_eonet(payload, NOW)


def _cov(record):
    return record.metadata["provider_record"]["coverage"]


def test_nasa_eonet_full_page_is_truncated_not_snapshot():
    payload = jfixture("nasa_eonet_open.json")
    base = payload["events"][0]
    payload["events"] = [{**base, "id": f"EONET_{i}"} for i in range(50)]
    records = map_nasa_eonet(payload, NOW)
    assert len(records) == 50 and {_cov(r) for r in records} == {"truncated"}


def test_nasa_eonet_short_page_is_snapshot():
    assert {_cov(r) for r in map_nasa_eonet(jfixture("nasa_eonet_open.json"), NOW)} == {"returned_snapshot"}


def test_nasa_eonet_one_malformed_event_is_skipped_and_batch_truncated():
    payload = jfixture("nasa_eonet_open.json")
    payload["events"][1]["geometry"] = []
    payload["events"].append({"id": "bad id!", "title": "x"})
    records = map_nasa_eonet(payload, NOW)
    assert [r.provider_id for r in records] == ["EONET_25238"] and _cov(records[0]) == "truncated"


def test_nasa_eonet_all_malformed_raises():
    payload = {"events": [{"id": "EONET_1", "title": "x", "geometry": []}]}
    with pytest.raises(ProviderPayloadError):
        map_nasa_eonet(payload, NOW)
