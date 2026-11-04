"""VietnamWorks job adapter: request building, mapping, filters, dedupe, dropped personal fields, option validation."""

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from modules.connectors import provider_options as po
from modules.connectors import registry
from modules.connectors.providers.jobs import (
    map_vietnamworks,
    merge_keyword_records,
    vietnamworks_request,
    vietnamworks_requests,
)
from modules.connectors.providers.macro import ProviderPayloadError
from modules.connectors.public import ConnectorConfig
from tests.unit.modules.connectors.p3_helpers import jfixture

NOW = datetime(2026, 11, 4, 5, 0, tzinfo=UTC)
PAYLOAD = jfixture("vietnamworks_jobs.json")


def _ids(records) -> list[str]:
    return [r.provider_id for r in records]


def test_request_is_one_json_post_per_keyword() -> None:
    """Each keyword becomes its own POST to the search host with a bounded JSON body; the default is `developer`."""
    first, second = vietnamworks_requests(["python", "data engineer"])
    assert first.method == "POST" and first.url == "https://ms.vietnamworks.com/job-search/v1.0/search"
    assert json.loads(first.body or b"") == {"query": "python", "hitsPerPage": 50, "page": 0}
    assert json.loads(second.body or b"")["query"] == "data engineer"
    assert json.loads(vietnamworks_request().body or b"")["query"] == "developer"


@pytest.mark.parametrize("bad", [[], ["x"] * 6, [""], ["a" * 61], ["line\nbreak"]])
def test_bad_keyword_sets_are_refused(bad: list[str]) -> None:
    """Empty, too many, empty, over-long or control-character keywords never reach the wire."""
    with pytest.raises(ProviderPayloadError):
        vietnamworks_requests(bad)


def test_maps_public_facts_and_rejects_foreign_link_host() -> None:
    """Four good hits map; the hit linking to a foreign host is skipped; identity, citation and body are public data."""
    records = map_vietnamworks(PAYLOAD, NOW)
    assert _ids(records) == ["vietnamworks:900001", "vietnamworks:900002", "vietnamworks:900003", "vietnamworks:900005"]
    first = records[0]
    assert first.metadata["title"] == "Python Developer"
    assert first.metadata["canonical_url"].startswith("https://www.vietnamworks.com/")
    assert first.observed_at == datetime(2026, 10, 30, 2, 0, tzinfo=UTC)
    for text in ("Example Software Co", "Ho Chi Minh", "Python", "SQL", "Information Technology"):
        assert text in first.content


def test_personal_and_recruiter_fields_are_dropped() -> None:
    """Contact name, e-mail and the demographic display flags appear nowhere in the stored record."""
    blob = json.dumps([r.model_dump(mode="json") for r in map_vietnamworks(PAYLOAD, NOW)], ensure_ascii=False)
    for secret in ("Fictional Contact", "contact@example.invalid", "rangeAge", "isShowAge", "isShowGender", "1 Example Street"):
        assert secret not in blob


def test_city_level_and_salary_filters() -> None:
    """Cities and levels match the pinned provider ids; salary compares USD directly and VND at a fixed rate."""
    assert _ids(map_vietnamworks(PAYLOAD, NOW, cities=["ha_noi"])) == ["vietnamworks:900002"]
    assert _ids(map_vietnamworks(PAYLOAD, NOW, cities=["da_nang", "ha_noi"])) == ["vietnamworks:900002", "vietnamworks:900003"]
    assert _ids(map_vietnamworks(PAYLOAD, NOW, levels=["manager"])) == ["vietnamworks:900003"]
    assert _ids(map_vietnamworks(PAYLOAD, NOW, levels=["fresher", "experienced"])) == [
        "vietnamworks:900001", "vietnamworks:900002", "vietnamworks:900005"]
    # >= 2000 USD: 3000 USD kept, 20M VND (~800 USD) dropped, 100M VND (~4000 USD) kept, hidden salary kept.
    assert _ids(map_vietnamworks(PAYLOAD, NOW, min_salary_usd=2000)) == [
        "vietnamworks:900001", "vietnamworks:900003", "vietnamworks:900005"]
    assert len(map_vietnamworks(PAYLOAD, NOW, min_salary_usd=0)) == 4


def test_keyword_results_are_deduplicated_by_job_id() -> None:
    """Overlapping keyword results collapse to one record per jobId, first occurrence kept."""
    one = map_vietnamworks(PAYLOAD, NOW, cities=["ho_chi_minh"])
    two = map_vietnamworks(PAYLOAD, NOW, levels=["experienced"])
    assert _ids(merge_keyword_records([one, two])) == ["vietnamworks:900001", "vietnamworks:900005"]


@pytest.mark.parametrize("payload", [None, [], {"data": "x"}, {"meta": {}}])
def test_hostile_bodies_are_payload_errors(payload: object) -> None:
    """Anything but {"data": [...]} fails the run with a typed payload error."""
    with pytest.raises(ProviderPayloadError):
        map_vietnamworks(payload, NOW)


def test_malformed_hits_are_skipped_not_fatal() -> None:
    """A hit without an integer id, a title or a link is skipped while the rest still map."""
    good = PAYLOAD["data"][0]
    junk = [None, "x", {"jobId": "7"}, {"jobId": True}, {**good, "jobId": 1, "jobTitle": ""}, {**good, "jobId": 2, "jobUrl": "http://www.vietnamworks.com/a"}]
    assert _ids(map_vietnamworks({"data": [*junk, good]}, NOW)) == ["vietnamworks:900001"]


def test_option_types_and_validation() -> None:
    """Owner options are bounded enums/strings and only valid on their own provider."""
    ConnectorConfig(job_keywords=("python", "go"), job_cities=("ha_noi",), job_levels=("fresher",), job_min_salary_usd=1500)
    for bad in ({"job_cities": ("hue",)}, {"job_levels": ("ceo",)}, {"job_min_salary_usd": 20001}, {"job_min_salary_usd": -1},
                {"job_keywords": ("a",) * 6}, {"job_keywords": ("x" * 61,)}, {"job_keywords": ()}):
        with pytest.raises(ValidationError):
            ConnectorConfig(**bad)
    with pytest.raises(ValueError, match="unique"):
        po.validate_options("vietnamworks_jobs", {"job_keywords": ["Python", "python"]})
    with pytest.raises(ValueError, match="unique"):
        po.validate_options("vietnamworks_jobs", {"job_cities": ["ha_noi", "ha_noi"]})
    assert po.selected_job_keywords({}) == ("developer",)
    assert po.selected_job_keywords({"job_keywords": ["Go", "go", "Rust"]}) == ("Go", "Rust")
    assert {"job_keywords", "job_cities", "job_levels", "job_min_salary_usd"} <= po.ALL_OPTION_NAMES


def test_job_options_are_legal_only_on_the_jobs_provider() -> None:
    """The registry accepts job filters on vietnamworks_jobs and rejects them on another built-in."""
    from tests.unit.modules.connectors.test_interval_floor import _source

    ok = _source("vietnamworks_jobs", 360)
    ok.configuration["job_cities"] = ["ha_noi"]
    registry.validate(ok)
    other = _source("vn_stock", 360)
    other.configuration["job_cities"] = ["ha_noi"]
    with pytest.raises(ValueError):
        registry.validate(other)
