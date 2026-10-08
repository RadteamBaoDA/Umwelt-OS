import copy

import pytest

from modules.connectors.providers.disasters import map_usgs
from modules.connectors.providers.macro import ProviderPayloadError
from tests.unit.modules.connectors.p3_helpers import NOW, jfixture, wd


def test_usgs_events():
    records = map_usgs(jfixture("usgs_4_5_day.geojson"), NOW)
    assert [r.provider_id for r in records] == ["usgs:us7000abcd", "usgs:us7000abce"]
    first = records[0]
    data = wd(first)
    assert data["value"] == 5.1 and data["unit"] == "magnitude" and data["latitude"] == 38.25 and data["longitude"] == 142.5
    assert data["provider_fields"]["decimal_mag"] == "5.1" and data["provider_fields"]["depth_km"] == 35.1
    assert data["provider_fields"]["updated"] == 1759893000000 and data["provider_fields"]["event_time"] == 1759890000000
    assert "detail" not in str(first.metadata)
    meta = first.metadata["provider_record"]
    assert meta["timestamp_basis"] == "provider_modified" and meta["provider_modified_at"] == "2025-10-08T03:10:00Z"
    assert first.observed_at.isoformat() == "2025-10-08T02:20:00+00:00" and first.collected_at == NOW


def test_usgs_null_magnitude_and_place_are_missing_not_invented():
    missing = map_usgs(jfixture("usgs_4_5_day.geojson"), NOW)[1]
    data = wd(missing)
    assert data["value"] is None and data["quality"] == "missing" and data["missing_reason"] == "provider_null"
    assert data["region"] is None and data["provider_fields"]["place"] is None


def test_usgs_version_changes_with_update_only():
    base = jfixture("usgs_4_5_day.geojson")
    first = map_usgs(base, NOW)[0]
    assert map_usgs(base, NOW.replace(hour=9))[0].version == first.version
    changed = copy.deepcopy(base)
    changed["features"][0]["properties"]["updated"] += 60_000
    assert map_usgs(changed, NOW)[0].version != first.version


def _mutated(fn):
    payload = copy.deepcopy(jfixture("usgs_4_5_day.geojson"))
    fn(payload)
    return payload


@pytest.mark.parametrize("payload", [
    [], "x", {}, {"type": "Feature"}, {"type": "FeatureCollection"}, {"type": "FeatureCollection", "features": {}},
    _mutated(lambda p: p["metadata"].update(count=5)),
    _mutated(lambda p: p["metadata"].update(status=500)),
    _mutated(lambda p: p["features"].__setitem__(0, "x")),
    _mutated(lambda p: p["features"][0].update(id=None)),
    _mutated(lambda p: p["features"][0].update(id="a/b")),
    _mutated(lambda p: p["features"][1].update(id="us7000abcd")),
    _mutated(lambda p: p["features"][0]["geometry"].update(coordinates=[1, 2])),
    _mutated(lambda p: p["features"][0]["geometry"].update(coordinates=[200, 0, 1])),
    _mutated(lambda p: p["features"][0]["geometry"].update(coordinates=[0, 95, 1])),
    _mutated(lambda p: p["features"][0]["geometry"].update(coordinates=["x", 0, 1])),
    _mutated(lambda p: p["features"][0]["geometry"].update(coordinates=[0, 0, True])),
    _mutated(lambda p: p["features"][0]["geometry"].update(coordinates=None)),
    _mutated(lambda p: p["features"][0]["properties"].update(time="x")),
    _mutated(lambda p: p["features"][0]["properties"].update(time=None)),
    _mutated(lambda p: p["features"][0]["properties"].update(time=32503680000000)),
    _mutated(lambda p: p["features"][0]["properties"].update(updated=1)),
    _mutated(lambda p: p["features"][0]["properties"].update(mag="NaN")),
    _mutated(lambda p: p["features"][0]["properties"].update(mag=99)),
    _mutated(lambda p: p["features"][0]["properties"].update(mag=True)),
    _mutated(lambda p: p["features"][0].update(properties=None)),
])
def test_usgs_hostile(payload):
    with pytest.raises(ProviderPayloadError):
        map_usgs(payload, NOW)


def test_usgs_feature_limit_is_incomplete_not_truncated():
    payload = copy.deepcopy(jfixture("usgs_4_5_day.geojson"))
    template = payload["features"][0]
    payload["features"] = [{**copy.deepcopy(template), "id": f"id{n}"} for n in range(501)]
    payload["metadata"]["count"] = 501
    with pytest.raises(ProviderPayloadError) as err:
        map_usgs(payload, NOW)
    assert err.value.kind == "incomplete"


def test_usgs_empty_day_is_valid_empty_collection_but_count_must_match():
    payload = {"type": "FeatureCollection", "metadata": {"status": 200, "count": 0}, "features": []}
    assert map_usgs(payload, NOW) == []
    payload["metadata"]["count"] = 3
    with pytest.raises(ProviderPayloadError):
        map_usgs(payload, NOW)


def test_usgs_hostile_place_is_bounded_and_cleaned():
    payload = copy.deepcopy(jfixture("usgs_4_5_day.geojson"))
    payload["features"][0]["properties"]["place"] = "\x00<script>" + "A" * 1000
    data = wd(map_usgs(payload, NOW)[0])
    assert len(data["provider_fields"]["place"]) <= 200 and len(data["region"]) <= 80 and "\x00" not in data["region"]
