"""OpenAlex works-search preset: server-built URL, bounded mapping, CC0 eligibility. Inline fixture; no network."""

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from modules.connectors.catalog import get_catalog_entry
from modules.connectors.provider_specs import DISPATCHABLE, get_provider_spec
from modules.connectors.providers.macro import ProviderPayloadError
from modules.connectors.providers.scholarly import map_openalex, openalex_url, rebuild_abstract
from modules.connectors.public import ConnectorConfig
from modules.knowledge.documents.schemas import ProviderRecordMetadata

NOW = datetime(2026, 10, 8, 2, 0, tzinfo=UTC)


def body(*works: dict) -> bytes:
    return json.dumps({"results": list(works)}).encode()


WORK = {
    "id": "https://openalex.org/W1", "doi": "https://doi.org/10.1/abc", "title": "A <b>study</b> of things",
    "publication_date": "2026-10-01", "cited_by_count": 0,
    "abstract_inverted_index": {"things": [3], "Study": [0], "of": [1, 2]},
    "authorships": [{"author": {"display_name": f"Author {i}"}} for i in range(15)],
    "primary_location": {"source": {"display_name": "Journal of Things"}},
}


def test_url_is_fixed_host_with_escaped_query():
    url = openalex_url("llm & agents=1\r\n#x")
    assert url.startswith("https://api.openalex.org/works?search=llm%20%26%20agents%3D1%20%23x&sort=publication_date%3Adesc")
    assert "per-page=25" in url and "\n" not in url and url.count("search=") == 1


@pytest.mark.parametrize("query", [None, "", "  \x00 ", "x" * 201, 5])
def test_rejects_bad_query(query):
    with pytest.raises(ProviderPayloadError):
        openalex_url(query)


def test_config_scope_strips_controls_and_bounds():
    assert ConnectorConfig(scholarly_query="a\x07\n b").scholarly_query == "a b"
    with pytest.raises(ValidationError):
        ConnectorConfig(scholarly_query="x" * 201)


def test_abstract_is_rebuilt_and_bounded():
    assert rebuild_abstract(WORK["abstract_inverted_index"]) == "Study of of things"
    assert rebuild_abstract({"x": [10**9, -1, "a"], "ok": [0]}) == "ok"
    assert rebuild_abstract(None) == ""


def test_maps_one_record_per_work_with_bounded_metadata():
    (record,) = map_openalex(body(WORK, {"id": "http://insecure", "title": "skipped"}, {"id": "https://openalex.org/W2"}), NOW)
    assert record.provider_id == "https://openalex.org/W1"
    assert record.content == "A study of things\n\nStudy of of things"
    assert record.observed_at == datetime(2026, 10, 1, tzinfo=UTC)
    fields = ProviderRecordMetadata.model_validate(record.metadata["provider_record"]).source_fields
    assert fields["doi"] == "https://doi.org/10.1/abc" and fields["source"] == "Journal of Things"
    assert fields["cited_by_count"] == 0 and len(fields["authors"]) == 10


def test_future_date_falls_back_to_collection_and_version_ignores_citations():
    future = {**WORK, "publication_date": "2099-01-01"}
    (record,) = map_openalex(body(future), NOW)
    assert record.observed_at == NOW and record.metadata["provider_record"]["timestamp_basis"] == "collection"
    (a,) = map_openalex(body(WORK), NOW)
    (b,) = map_openalex(body({**WORK, "cited_by_count": 99}), NOW)
    assert a.version == b.version


@pytest.mark.parametrize("raw", [b"[]", b'{"results": "x"}', b"not json"])
def test_invalid_bodies_are_refused(raw):
    with pytest.raises(ProviderPayloadError):
        map_openalex(raw, NOW)


def test_empty_results_are_incomplete_not_empty_snapshot():
    with pytest.raises(ProviderPayloadError) as err:
        map_openalex(body(), NOW)
    assert err.value.kind == "incomplete"


def test_catalog_marks_cc0_open_unverified_and_dispatchable():
    spec = get_provider_spec("openalex")
    entry = get_catalog_entry("openalex")
    assert spec is not None and entry is not None
    assert spec.eligibility == "open" and not spec.key_required and not spec.runtime_verified
    assert "openalex" in DISPATCHABLE and entry.scope_fields == ("scholarly_query",)
