"""Wave D per-type options: URL goldens, validation, allowlists, region and client filters (pure; no I/O)."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from modules.connectors import provider_options as po
from modules.connectors.provider_specs import applicable_options
from modules.connectors.providers.disasters import usgs_request
from modules.connectors.providers.hazards import nasa_eonet_request
from modules.connectors.providers.json_feed import JSON_FEEDS, feed_request
from modules.connectors.providers.news import hn_top_request
from modules.connectors.providers.security import ioda_request, nvd_request
from modules.connectors.public import ConnectorConfig
from modules.connectors.registry import validate
from modules.ingestion.schemas import IngestionRecord
from modules.sources.schemas import PROVIDER_SOURCE_TYPES, ConnectorSource

NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)
BASE = {"history_mode": "returned_snapshot", "schedule_interval_minutes": 30, "timeout_seconds": 30}


def _src(provider: str, configuration: dict) -> ConnectorSource:
    """Active built-in source row carrying ``configuration``."""
    return ConnectorSource(
        id=uuid4(), workspace_id=uuid4(), type=PROVIDER_SOURCE_TYPES[provider], status="active",
        generation=1, configuration=configuration, local_only=False, provider=provider)


def test_defaults_build_no_override_for_every_option_provider():
    # None means "use the provider's untouched builder", i.e. today's URL byte for byte.
    for provider in ("hn_top", "gdelt_economy", "usgs", "emsc_quakes", "nasa_eonet", "gdacs_events", "nws_alerts",
                     "nvd", "cisa_kev", "ioda", "launch_library"):
        assert po.build_url(provider, {}, cursor=None, now=NOW) is None, provider


def test_hn_list_url_and_default():
    assert po.build_url("hn_top", {"hn_list": "top"}, cursor=None, now=NOW) is None
    assert po.build_url("hn_top", {"hn_list": "show"}, cursor=None, now=NOW) == (
        "https://hacker-news.firebaseio.com/v0/showstories.json")
    assert hn_top_request().url.endswith("/topstories.json")
    assert po.hn_limit({}) == 10 and po.hn_limit({"max_items": 25}) == 25 and po.hn_limit({"max_items": 90}) == 30


def test_gdelt_url_golden():
    url = po.build_url("gdelt_economy", {"max_items": 7}, cursor=None, now=NOW)
    assert url.endswith("?query=economy&mode=artlist&format=json&maxrecords=7&timespan=24h")
    url = po.build_url("gdelt_economy", {
        "gdelt_query": "inflation rate", "gdelt_timespan": "3d", "gdelt_source_lang": "vietnamese",
        "gdelt_source_country": "VN"}, cursor=None, now=NOW)
    assert url == ("https://api.gdeltproject.org/api/v2/doc/doc?query=inflation+rate+sourcelang%3Avietnamese"
                   "+sourcecountry%3AVM&mode=artlist&format=json&maxrecords=5&timespan=3d")


def test_gdelt_cursor_becomes_native_startdatetime_without_timespan():
    url = po.build_url("gdelt_economy", {}, cursor="2026-11-02T10:00:00+00:00", now=NOW)
    assert "startdatetime=20261102095000" in url and "timespan" not in url  # cursor minus the 10 min skew


def test_usgs_url_matrix():
    assert po.build_url("usgs", {"usgs_min_magnitude": "significant", "usgs_period": "week"}, cursor=None, now=NOW) == (
        "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_week.geojson")
    assert po.build_url("usgs", {"usgs_period": "hour"}, cursor=None, now=NOW).endswith("/4.5_hour.geojson")
    assert usgs_request().url.endswith("/4.5_day.geojson")


def test_emsc_url_golden_and_clamp():
    assert po.build_url("emsc_quakes", {"min_magnitude": 5.5, "max_items": 20}, cursor=None, now=NOW) == (
        "https://www.seismicportal.eu/fdsnws/event/1/query?limit=20&format=json&minmag=5.5")
    assert po.build_url("emsc_quakes", {"min_magnitude": 4.0}, cursor=None, now=NOW).endswith("limit=50&format=json&minmag=4")
    assert po.build_url("emsc_quakes", {"max_items": 80}, cursor=None, now=NOW).startswith(
        "https://www.seismicportal.eu/fdsnws/event/1/query?limit=50&")  # never raises the 50 cap
    assert feed_request(JSON_FEEDS["emsc_quakes"]).url.endswith("?limit=50&format=json&minmag=4")


def test_eonet_url_golden():
    assert po.build_url("nasa_eonet", {"eonet_categories": ["wildfires", "floods"], "eonet_status": "all",
                                       "eonet_days": 7}, cursor=None, now=NOW) == (
        "https://eonet.gsfc.nasa.gov/api/v3/events?status=all&limit=50&category=floods,wildfires&days=7")
    assert nasa_eonet_request().url.endswith("events?status=open&limit=50")


def test_gdacs_url_golden_is_order_independent():
    expected = ("https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH?eventlist=EQ;FL"
                "&alertlevel=Red")
    assert po.build_url("gdacs_events", {"gdacs_event_types": ["FL", "EQ"], "gdacs_alert_levels": ["Red"]},
                        cursor=None, now=NOW) == expected
    assert po.build_url("gdacs_events", {"gdacs_alert_levels": ["Green", "Orange", "Red"]}, cursor=None, now=NOW).endswith(
        "eventlist=EQ;TC;FL;VO;WF;DR&alertlevel=Green;Orange;Red")
    assert feed_request(JSON_FEEDS["gdacs_events"]).url.endswith("eventlist=EQ;TC;FL;VO;WF;DR&alertlevel=Orange;Red")


def test_nws_url_golden():
    assert po.build_url("nws_alerts", {"nws_severity": ["Minor", "Extreme"], "nws_area": "CA"}, cursor=None, now=NOW) == (
        "https://api.weather.gov/alerts/active?status=actual&message_type=alert&severity=Extreme,Minor&area=CA")
    assert feed_request(JSON_FEEDS["nws_alerts"]).url.endswith("severity=Extreme,Severe")


def test_nvd_url_golden_and_default_shape():
    assert nvd_request().url.count("resultsPerPage=20&pubStartDate=") == 1
    url = po.build_url("nvd", {"max_items": 5, "nvd_window_days": 3, "nvd_severity": ["HIGH"],
                               "nvd_keyword": "log4j & rce"}, cursor=None, now=NOW)
    assert url == ("https://services.nvd.nist.gov/rest/json/cves/2.0?resultsPerPage=5"
                   "&pubStartDate=2026-10-30T12:00:00.000&pubEndDate=2026-11-02T12:00:00.000"
                   "&cvssV3Severity=HIGH&keywordSearch=log4j%20%26%20rce")
    multi = po.build_url("nvd", {"nvd_severity": ["HIGH", "CRITICAL"]}, cursor=None, now=NOW)
    assert "cvssV3Severity" not in multi  # NVD takes one value; several are banded client-side


def test_ioda_url_golden():
    url = po.build_url("ioda", {"ioda_country": "VN"}, cursor=None, now=NOW)
    assert "entityType=country&entityCode=VN&limit=50" in url
    assert "entityCode" not in ioda_request().url


def test_launch_library_url_golden_and_native_resume():
    assert po.build_url("launch_library", {"max_items": 25}, cursor=None, now=NOW) == (
        "https://ll.thespacedevs.com/2.2.0/launch/upcoming/?limit=25&mode=list")
    assert po.build_url("launch_library", {}, cursor="2026-11-02T10:00:00+00:00", now=NOW) == (
        "https://ll.thespacedevs.com/2.2.0/launch/upcoming/?limit=10&mode=list&last_updated__gte=2026-11-02T10%3A00%3A00%2B00%3A00")
    assert feed_request(JSON_FEEDS["launch_library"]).url.endswith("?limit=10&mode=list")


@pytest.mark.parametrize("provider, bad", [
    ("usgs", {"usgs_period": "year"}), ("usgs", {"usgs_min_magnitude": "7"}),
    ("emsc_quakes", {"min_magnitude": 9}), ("emsc_quakes", {"min_magnitude": 1.5}),
    ("nasa_eonet", {"eonet_days": 31}), ("nasa_eonet", {"eonet_categories": ["aliens"]}),
    ("gdacs_events", {"gdacs_event_types": ["XX"]}), ("nws_alerts", {"nws_area": "ZZ"}),
    ("nvd", {"nvd_window_days": 8}), ("nvd", {"max_items": 21}), ("hn_top", {"hn_list": "random"}),
    ("gdelt_economy", {"gdelt_timespan": "30d"}), ("gdelt_economy", {"gdelt_query": "x site:evil.com"}),
    ("ioda", {"ioda_country": "vietnam"}), ("usgs", {"region": "mars"}),
    ("gdacs_events", {"gdacs_alert_levels": ["Red", "Red"]}),
])
def test_invalid_value_is_rejected(provider, bad):
    with pytest.raises(ValueError):
        validate(_src(provider, {**bad, **BASE}))


@pytest.mark.parametrize("provider, option", [
    ("binance", {"hn_list": "new"}), ("usgs", {"nvd_keyword": "x"}), ("hn_top", {"usgs_period": "day"}),
    ("tuoitre_news", {"region": "vietnam"}), ("nvd", {"region": "vietnam"}), ("cisa_kev", {"max_items": 5}),
])
def test_option_rejected_on_wrong_provider(provider, option):
    with pytest.raises(ValueError, match="unsupported fields"):
        validate(_src(provider, {**option, **BASE}))


def test_valid_options_pass_registry_and_round_trip():
    validate(_src("nvd", {"nvd_severity": ["HIGH", "CRITICAL"], "nvd_window_days": 2, "nvd_keyword": "chrome", "max_items": 10, **BASE}))
    validate(_src("usgs", {"usgs_min_magnitude": "2.5", "usgs_period": "week", "region": "vietnam", **BASE}))
    validate(_src("gdelt_economy", {"gdelt_query": "  oil  prices ", "max_items": 30, **BASE}))
    assert ConnectorConfig(gdelt_query="  oil\n prices ").gdelt_query == "oil prices"


def test_applicable_options_lists_per_type_and_common_only_rows():
    assert {"hn_list", "max_items"} <= set(applicable_options("hn_top"))
    assert {"usgs_min_magnitude", "usgs_period", "region"} <= set(applicable_options("usgs"))
    assert "nvd_keyword" in applicable_options("nvd") and "max_items" in applicable_options("nvd")
    assert "max_items" not in applicable_options("cisa_kev") and "kev_ransomware_only" in applicable_options("cisa_kev")
    assert "region" in applicable_options("bmkg_quakes")
    for common_only in ("aljazeera_world", "gdacs_rss", "noaa_tsunami", "jma_eqvol"):
        assert {"max_items", "include_keywords", "exclude_keywords", "max_age_days"} <= set(applicable_options(common_only))
        assert not set(applicable_options(common_only)) & (po.ALL_OPTION_NAMES - {"max_items"})


def _geo(identity: str, lat: float | None, lon: float | None, text: str = "") -> IngestionRecord:
    """Record whose location is only in typed world data (measurements) or in summary text (feeds)."""
    world = {"latitude": lat, "longitude": lon, "value": 5.0} if lat is not None and not text else None
    return IngestionRecord(provider_id=identity, content=text or identity, observed_at=NOW,
                           metadata={"provider_record": {"world_data": world}})


def test_region_filter_drops_outside_box_and_keeps_unlocated():
    hanoi, tokyo, nowhere = _geo("hanoi", 21.0, 105.8), _geo("tokyo", 35.7, 139.7), _geo("nowhere", None, None)
    kept = po.filter_records("usgs", {"region": "vietnam"}, [hanoi, tokyo, nowhere])
    assert [r.provider_id for r in kept] == ["hanoi", "nowhere"]
    assert len(po.filter_records("usgs", {"region": "world"}, [hanoi, tokyo])) == 2
    assert len(po.filter_records("usgs", {}, [hanoi, tokyo])) == 2


def test_region_filter_reads_summary_text_for_feed_providers():
    gdacs = _geo("g", None, None, "Alert: Red\nLocation: 21, 105.8")  # GDACS/EMSC: "Location: lat, lon"
    bmkg = _geo("b", None, None, "Coordinates: -6.2,106.8")  # BMKG: "Coordinates: lat,lon"
    assert po.filter_records("gdacs_events", {"region": "vietnam"}, [gdacs])
    assert not po.filter_records("bmkg_quakes", {"region": "vietnam"}, [bmkg])
    assert po.filter_records("bmkg_quakes", {"region": "southeast_asia"}, [bmkg])


def test_kev_and_nvd_client_filters():
    def kev(flag):
        return IngestionRecord(provider_id=flag, content=flag, observed_at=NOW, metadata={"provider_record": {
            "world_data": {"provider_fields": {"knownRansomwareCampaignUse": flag}}}})
    got = po.filter_records("cisa_kev", {"kev_ransomware_only": True}, [kev("Known"), kev("Unknown")])
    assert [r.provider_id for r in got] == ["Known"]
    assert len(po.filter_records("cisa_kev", {}, [kev("Known"), kev("Unknown")])) == 2

    def cve(cid, score):
        return IngestionRecord(provider_id=cid, content=cid, observed_at=NOW,
                               metadata={"provider_record": {"world_data": {"value": score}}})
    rows = [cve("low", 2.0), cve("high", 8.0), cve("crit", 9.8), cve("none", None)]
    got = po.filter_records("nvd", {"nvd_severity": ["HIGH", "CRITICAL"]}, rows)
    assert [r.provider_id for r in got] == ["high", "crit"]


def test_fingerprint_changes_only_with_native_params():
    base = po.native_fingerprint("gdelt_economy", {})
    assert base == po.native_fingerprint("gdelt_economy", {"gdelt_query": "economy", "gdelt_timespan": "24h"})
    assert base == po.native_fingerprint("gdelt_economy", {"max_items": 3, "include_keywords": ["x"]})
    assert base != po.native_fingerprint("gdelt_economy", {"gdelt_query": "oil"})
    assert po.native_fingerprint("emsc_quakes", {}) != po.native_fingerprint("emsc_quakes", {"min_magnitude": 6})
    assert (po.native_fingerprint("emsc_quakes", {"min_magnitude": 6})
            == po.native_fingerprint("emsc_quakes", {"min_magnitude": 6, "region": "europe"}))
    assert po.native_fingerprint("gdacs_events", {"gdacs_alert_levels": ["Red", "Orange"]}) == po.native_fingerprint("gdacs_events", {})
    assert po.native_fingerprint("tuoitre_news", {}) is None and po.native_fingerprint("usgs", {}) is None
