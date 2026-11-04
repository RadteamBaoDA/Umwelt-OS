"""ITviec job adapter: search/URL guards, JSON-LD parsing, filters, privacy, caps and runner behaviour (fictional pages)."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from modules.connectors import collection
from modules.connectors import provider_options as po
from modules.connectors.providers import jobs, rest
from modules.connectors.providers.jobs import (
    itviec_job_url,
    itviec_keyword_slug,
    itviec_search_requests,
    map_itviec,
    parse_itviec_item_list,
)
from modules.connectors.providers.macro import ProviderPayloadError
from modules.connectors.public import ConnectorConfig
from tests.unit.modules.connectors.p3_helpers import fixture

NOW = datetime(2026, 11, 4, 5, 0, tzinfo=UTC)
BASE = "https://itviec.com/it-jobs/"
SEARCH = fixture("itviec_search.html").decode()
PAGES = [
    (f"{BASE}python-developer-example-co-9001", fixture("itviec_job_9001.html").decode()),
    (f"{BASE}data-engineer-sample-ltd-9002", fixture("itviec_job_9002.html").decode()),
    (f"{BASE}qa-lead-demo-corp-9003", fixture("itviec_job_9003.html").decode()),
]


def _ids(records) -> list[str]:
    return [r.provider_id for r in records]


def test_search_requests_cover_keyword_city_pairs_with_verified_slugs() -> None:
    """Each keyword x city pair is one GET to the city slug; no city means the nationwide page; the default is `developer`."""
    urls = [r.url for r in itviec_search_requests(["Python", "Data Engineer"], ["ho_chi_minh"])]
    assert urls == [f"{BASE}python/ho-chi-minh-hcm", f"{BASE}data-engineer/ho-chi-minh-hcm"]
    assert [r.url for r in itviec_search_requests(["go"], ["ha_noi", "da_nang"])] == [f"{BASE}go/ha-noi", f"{BASE}go/da-nang"]
    assert [r.url for r in itviec_search_requests(["go"])] == [f"{BASE}go"]
    assert jobs.itviec_request().url == f"{BASE}developer"
    assert all(r.method == "GET" and r.body is None for r in itviec_search_requests(["go"]))


@pytest.mark.parametrize(("keywords", "cities"), [
    (["a", "b", "c", "d"], []), (["a", "b"], ["ho_chi_minh", "ha_noi"]), ([], []), (["a"], ["hue"]), (["???"], []),
])
def test_scope_beyond_three_searches_or_unusable_is_refused(keywords: list[str], cities: list[str]) -> None:
    """More than 3 pages, an unknown city, no keyword or a keyword with no slug-able text never reaches the wire."""
    with pytest.raises(ProviderPayloadError):
        itviec_search_requests(keywords, cities)


def test_keyword_slug_folds_accents_and_strips_url_syntax() -> None:
    """Keywords cannot add path, query or encoding syntax to the request URL."""
    assert itviec_keyword_slug("  Kỹ sư Python/../x?y=1 ") == "ky-su-python-x-y-1"
    assert itviec_keyword_slug("Đồng Nai") == "dong-nai"
    assert itviec_keyword_slug("C++ Dev") == "c-dev"


def test_item_list_guard_keeps_only_posting_urls_on_the_host() -> None:
    """Foreign hosts, http, other paths, credentials, nested search pages are dropped; query is stripped; duplicates collapse."""
    assert parse_itviec_item_list(SEARCH) == [
        f"{BASE}python-developer-example-co-9001", f"{BASE}data-engineer-sample-ltd-9002", f"{BASE}qa-lead-demo-corp-9003"]
    assert itviec_job_url("https://itviec.com:8443/it-jobs/x-1") is None
    assert parse_itviec_item_list("<html>no json-ld</html>") == []


def test_maps_public_facts_city_only_and_drops_body() -> None:
    """Identity is itviec:{slug}; location is the city; street, description and apply link never reach the record."""
    records = map_itviec(PAGES, NOW)
    assert _ids(records) == [
        "itviec:python-developer-example-co-9001", "itviec:data-engineer-sample-ltd-9002", "itviec:qa-lead-demo-corp-9003"]
    first = records[0]
    assert first.metadata["title"] == "Python Developer" and first.metadata["canonical_url"] == PAGES[0][0]
    assert first.observed_at == datetime(2026, 11, 2, 17, 0, tzinfo=UTC)
    for text in ("Example Co", "Hồ Chí Minh", "800 - 1,500 USD", "Python, SQL", "FULL_TIME"):
        assert text in first.content
    blob = " ".join(r.content + str(r.metadata) for r in records)
    for secret in ("Example Street", "Example District", "700000", "long description", "apply-secret", "logo.png"):
        assert secret not in blob
    assert "Salary: not disclosed" in records[2].content


def test_salary_and_level_filters_keep_hidden_salary() -> None:
    """USD compares directly, VND at the fixed rate; hidden salary is kept; level matches experience text when present."""
    assert _ids(map_itviec(PAGES, NOW, min_salary_usd=1000)) == [
        "itviec:python-developer-example-co-9001", "itviec:qa-lead-demo-corp-9003"]  # 20M VND ~ 800 USD dropped
    assert _ids(map_itviec(PAGES, NOW, min_salary_usd=2000)) == ["itviec:qa-lead-demo-corp-9003"]
    assert _ids(map_itviec(PAGES, NOW, levels=["fresher"])) == [
        "itviec:data-engineer-sample-ltd-9002", "itviec:qa-lead-demo-corp-9003"]  # 9003 has no text: kept
    assert _ids(map_itviec(PAGES, NOW, levels=["intern", "experienced"])) == [
        "itviec:python-developer-example-co-9001", "itviec:qa-lead-demo-corp-9003"]


def test_malformed_pages_and_urls_are_skipped_not_fatal() -> None:
    """Broken JSON-LD, a page without a JobPosting, or a URL off the guard yields nothing while good pages still map."""
    bad = [(f"{BASE}broken-1", fixture("itviec_job_malformed.html").decode()), (f"{BASE}empty-2", "<html></html>"),
           ("https://evil.example.com/it-jobs/x-3", PAGES[0][1])]
    assert _ids(map_itviec([*bad, PAGES[0]], NOW)) == ["itviec:python-developer-example-co-9001"]


def test_double_quoted_and_graph_wrapped_json_ld_parses() -> None:
    """Attribute quoting and @graph wrapping vary between sites; both forms parse."""
    html = '<script type="application/ld+json">{"@graph":[{"@type":"JobPosting","title":"Graph Job"}]}</script>'
    assert _ids(map_itviec([(f"{BASE}graph-job-5", html)], NOW)) == ["itviec:graph-job-5"]


def test_options_validation_caps_searches() -> None:
    """Job filters are legal on itviec_jobs and more than 3 keyword x city pages is rejected at save time."""
    ConnectorConfig(job_keywords=("python",), job_cities=("ha_noi",))
    po.validate_options("itviec_jobs", {"job_keywords": ["a", "b", "c"]})
    po.validate_options("itviec_jobs", {"job_keywords": ["a"], "job_cities": ["ho_chi_minh", "ha_noi", "da_nang"]})
    with pytest.raises(ValueError, match="at most 3"):
        po.validate_options("itviec_jobs", {"job_keywords": ["a", "b"], "job_cities": ["ha_noi", "da_nang"]})
    with pytest.raises(ValidationError):
        ConnectorConfig(job_cities=("hue",))
    assert "job_levels" in po.OPTION_FIELDS["itviec_jobs"]


# ---------------------------------------------------------------- runner (fakes only: no DB, no network)

def _runner(monkeypatch, responses, seen=frozenset(), configuration=None):
    """Wire ``_run_itviec`` to a fake gate, a fake seen-id lookup and a recording sleep; returns the probes."""
    import asyncio

    from tests.unit.modules.connectors.test_free_provider_dispatch import Gate, make_run

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def seen_ids(_session, **kwargs):
        return {i for i in kwargs["provider_ids"] if i in seen}

    gate = Gate(responses)
    run, accepted = make_run("itviec_jobs", gate, monkeypatch, configuration=configuration)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(collection.ingestion, "seen_provider_ids", seen_ids)
    return run, gate, accepted, sleeps


def _ok(html: str):
    return rest.Fetched(html.encode())


async def test_runner_fetches_only_unseen_postings_two_seconds_apart(monkeypatch) -> None:
    """One search page, details only for unseen URLs, a gone posting skipped, >= 2 s between every request."""
    responses = {
        f"{BASE}developer": _ok(SEARCH),
        PAGES[1][0]: _ok(PAGES[1][1]),
        PAGES[2][0]: rest.ProviderHttpError(404),
        "*": rest.ProviderHttpError(500),
    }
    run, gate, accepted, sleeps = _runner(monkeypatch, responses, seen={"itviec:python-developer-example-co-9001"})
    await collection._run_itviec(run)
    called = [u for u, _ in gate.calls]
    assert called[0] == f"{BASE}developer" and set(called[1:]) == {PAGES[1][0], PAGES[2][0]}
    assert sleeps == [2.0, 2.0]
    assert _ids(accepted[0][0]) == ["itviec:data-engineer-sample-ltd-9002"]


async def test_runner_caps_detail_fetches_at_ten(monkeypatch) -> None:
    """Thirty postings in a search page cost one search request plus exactly ten detail requests."""
    import json

    urls = [f"{BASE}job-{n}" for n in range(30)]
    listing = json.dumps({"@type": "ItemList", "itemListElement": [{"url": u} for u in urls]})
    html = f"<script type='application/ld+json'>{listing}</script>"
    run, gate, _accepted, _sleeps = _runner(monkeypatch, {f"{BASE}developer": _ok(html), "*": _ok(PAGES[0][1])})
    await collection._run_itviec(run)
    assert len(gate.calls) == 1 + jobs.ITVIEC_MAX_DETAIL_FETCHES
    assert all(kwargs["max_bytes"] for _u, kwargs in gate.calls)


async def test_runner_non_gone_error_fails_the_run(monkeypatch) -> None:
    """A 500 on a posting fails the run rather than silently dropping it."""
    run, _gate, accepted, _ = _runner(monkeypatch, {f"{BASE}developer": _ok(SEARCH), "*": rest.ProviderHttpError(500)})
    with pytest.raises(rest.ProviderHttpError):
        await collection._run_itviec(run)
    assert not accepted
