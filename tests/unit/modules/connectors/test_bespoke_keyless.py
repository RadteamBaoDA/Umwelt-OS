"""Bespoke keyless mappers (NOAA Kp, Open ER API, OECD SDMX-JSON, ISS, disease.sh, IODA, NVD): fixtures only."""

import copy
import json
from datetime import UTC, datetime

import pytest

from modules.connectors.providers.hazards import map_noaa_kp
from modules.connectors.providers.macro import (
    ProviderPayloadError,
    map_oecd_cli,
    map_open_er_api,
    map_sdmx_json,
)
from modules.connectors.providers.security import (
    ioda_request,
    map_disease_sh,
    map_ioda,
    map_nvd,
    nvd_request,
)
from modules.connectors.providers.space import map_iss_position
from modules.connectors.providers.world_data import PURE_ADAPTERS
from tests.unit.modules.connectors.p3_helpers import jfixture, wd

NOW = datetime(2026, 10, 22, 0, 0, tzinfo=UTC)


def test_noaa_kp_maps_three_hour_bins_as_utc():
    records = map_noaa_kp(jfixture("noaa_kp.json"), NOW)
    assert len(records) == 6 and all(wd(r)["unit"] == "kp" for r in records)
    assert records[0].observed_at.tzinfo is UTC and records[0].observed_at < records[-1].observed_at
    assert records[0].provider_id.startswith("noaa_kp:2026-")


def test_noaa_kp_rejects_out_of_range_index():
    bad = jfixture("noaa_kp.json")
    bad[0]["Kp"] = 11
    with pytest.raises(ProviderPayloadError, match="noaa_kp_value_invalid"):
        map_noaa_kp(bad, NOW)


def test_open_er_api_maps_fixed_quotes_and_flags_quota_error():
    records = map_open_er_api(jfixture("open_er_api.json"), NOW)
    assert {wd(r)["currency"] for r in records} == {"VND", "EUR", "JPY", "GBP", "CNY"}
    assert all(wd(r)["quality"] == "reference" and r.provider_id.startswith("open_er_api:USD:") for r in records)
    with pytest.raises(ProviderPayloadError) as err:
        map_open_er_api({"result": "error", "error-type": "quota-reached"}, NOW)
    assert err.value.kind == "rate_limited"


def test_oecd_cli_sdmx_one_record_per_area_and_shape_change_fails():
    payload = jfixture("oecd_cli.json")
    records = map_oecd_cli(payload, NOW)
    assert {wd(r)["region"] for r in records} == {"JPN", "USA", "CHN", "DEU", "GBR"}
    assert all(r.provider_id.endswith(":2026-09") and wd(r)["unit"] == "index" for r in records)
    broken = copy.deepcopy(payload)
    broken["data"]["dataSets"][0]["observations"]["9:9:9"] = [1.0]
    with pytest.raises(ProviderPayloadError, match="oecd_cli_response_invalid"):
        map_oecd_cli(broken, NOW)
    with pytest.raises(ProviderPayloadError):
        map_sdmx_json("oecd_cli", {"data": {}}, NOW, metric="m", unit="u", title="t")


def test_iss_position_typed_coordinates_and_units_guard():
    payload = jfixture("iss_position.json")
    (record,) = map_iss_position(payload, NOW)
    measurement = wd(record)
    assert -90 <= measurement["latitude"] <= 90 and -180 <= measurement["longitude"] <= 180
    assert measurement["unit"] == "km" and record.provider_id.startswith("iss_position:25544:")
    with pytest.raises(ProviderPayloadError, match="iss_position_units_invalid"):
        map_iss_position({**payload, "units": "miles"}, NOW)


def test_disease_sh_one_record_per_metric_keyed_in_place():
    records = map_disease_sh(jfixture("disease_sh.json"), NOW)
    assert [r.provider_id for r in records] == [f"disease_sh:global:{m}" for m in ("cases", "deaths", "recovered", "active")]
    with pytest.raises(ProviderPayloadError, match="disease_sh_value_invalid"):
        map_disease_sh({**jfixture("disease_sh.json"), "cases": -1}, NOW)


def test_ioda_events_skip_malformed_and_error_body_fails():
    payload = jfixture("ioda_events.json")
    payload["data"].append({"location": "bad id", "datasource": "bgp", "start": 1})
    records = map_ioda(payload, NOW)
    assert len(records) == 3 and all(wd(r)["unit"] == "ioda_score" for r in records)
    with pytest.raises(ProviderPayloadError):
        map_ioda({**payload, "error": "'from' timestamp must be set", "data": None}, NOW)
    with pytest.raises(ProviderPayloadError) as err:
        map_ioda({"error": None, "data": []}, NOW)
    assert err.value.kind == "incomplete"
    assert "from=" in ioda_request().url and "until=" in ioda_request().url


def test_nvd_maps_scored_and_unscored_cves_and_truncation():
    payload = jfixture("nvd_cves.json")
    records = map_nvd(payload, NOW)
    assert len(records) == 3 and all(r.provider_id.startswith("CVE-") for r in records)
    assert all(r.metadata["provider_record"]["coverage"] == "truncated" for r in records)  # totalResults > page
    assert json.dumps(wd(records[0])["provider_fields"])  # bounded, JSON-safe
    assert "pubStartDate=" in nvd_request().url


def test_all_registered_as_pure_adapters():
    for provider in ("noaa_kp", "open_er_api", "oecd_cli", "iss_position", "disease_sh", "ioda", "nvd"):
        assert provider in PURE_ADAPTERS
