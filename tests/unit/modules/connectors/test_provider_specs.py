"""P1 catalog facts, eligibility and typed-schema contract (pure, no DB, no network)."""

import pytest
from pydantic import ValidationError

from modules.connectors.catalog import get_catalog_entry, list_catalog
from modules.connectors.provider_specs import (
    DISPATCHABLE,
    FREE_PROVIDER_SPECS,
    OperatorReview,
    deployment_use_matches,
    evaluate_terms,
    get_provider_spec,
)
from modules.knowledge.documents.schemas import (
    PROVIDER_IDS,
    ProviderRecordMetadata,
    WorldDataMeasurement,
)

MEASUREMENT_IDS = {"world_bank", "frankfurter", "ecb", "binance", "alternative_me", "usgs", "coinpaprika", "coingecko"}
NEWS_IDS = {"bbc_world", "vnexpress_business", "hn_top", "gdelt_economy"}


def test_thirteen_frozen_ids_with_expected_eligibility() -> None:
    eligibility = {spec.id: spec.eligibility for spec in FREE_PROVIDER_SPECS}
    assert len(eligibility) == 13
    for open_id in ("world_bank", "frankfurter", "ecb", "usgs", "alternative_me"):
        assert eligibility[open_id] == "open"
    assert eligibility["vnexpress_business"] == "noncommercial"
    assert eligibility["alpha_vantage"] == eligibility["coinpaprika"] == "personal"
    for review_id in ("bbc_world", "binance", "coingecko", "hn_top", "gdelt_economy"):
        assert eligibility[review_id] == "review"


@pytest.mark.parametrize("deployment_use", ["commercial", "unknown"])
def test_personal_provider_cannot_activate_for_incompatible_use(deployment_use: str) -> None:
    assert not deployment_use_matches("personal", deployment_use)


@pytest.mark.parametrize(("eligibility", "use", "expected"), [
    ("open", "unknown", True), ("open", "commercial", True),
    ("personal", "personal", True), ("personal", "noncommercial", False),
    ("noncommercial", "personal", True), ("noncommercial", "noncommercial", True),
    ("noncommercial", "commercial", False), ("noncommercial", "unknown", False),
    ("review", "personal", False), ("review", "commercial", False), ("surprise", "personal", False),
])
def test_deployment_use_matrix(eligibility: str, use: str, expected: bool) -> None:
    assert deployment_use_matches(eligibility, use) is expected


def test_terms_require_acknowledgement_for_every_provider() -> None:
    spec = get_provider_spec("world_bank")
    assert spec is not None
    assert not evaluate_terms(spec, declared_use="unknown", acknowledged_terms_version=None).allowed
    assert evaluate_terms(spec, declared_use="unknown", acknowledged_terms_version="2026-10").allowed


def test_review_provider_needs_matching_operator_approval() -> None:
    spec = get_provider_spec("binance")
    assert spec is not None
    base = {"declared_use": "noncommercial", "acknowledged_terms_version": "v1"}
    assert evaluate_terms(spec, **base).code == "operator_review_required"
    pending = OperatorReview("pending")
    assert not evaluate_terms(spec, **base, review=pending).allowed
    approved = OperatorReview("approved", "noncommercial", "ticket-1", "v1")
    assert evaluate_terms(spec, **base, review=approved).allowed
    # Terms corrected by the owner after review: the review no longer applies.
    assert not evaluate_terms(spec, declared_use="noncommercial", acknowledged_terms_version="v2", review=approved).allowed
    # Review limited to personal use does not cover a noncommercial declaration.
    narrow = OperatorReview("approved", "personal", "ticket-1", "v1")
    assert evaluate_terms(spec, **base, review=narrow).code == "use_incompatible"
    assert not evaluate_terms(spec, **base, review=OperatorReview("approved", "commercial", None, "v1")).allowed
    assert not evaluate_terms(spec, declared_use="unknown", acknowledged_terms_version="v1", review=approved).allowed


def test_specs_never_claim_runtime_acceptance_or_unproven_code() -> None:
    assert not any(spec.runtime_verified for spec in FREE_PROVIDER_SPECS)
    # Code presence only: exactly the providers the shared executor dispatches (CoinGecko lacks a key slot).
    from modules.connectors.collection import ADAPTERS

    implemented = {spec.id for spec in FREE_PROVIDER_SPECS if spec.code_implemented}
    assert implemented == {"alpha_vantage"} | set(DISPATCHABLE)
    assert set(DISPATCHABLE) <= set(ADAPTERS) and "coingecko" not in implemented


def test_unknown_official_caps_are_counted_not_invented() -> None:
    for spec in FREE_PROVIDER_SPECS:
        assert spec.quota, spec.id
        for window in spec.quota:
            if window.basis == "official_unknown":
                assert window.limit_units is None
    keys = {w.policy_key for w in get_provider_spec("coingecko").quota}  # type: ignore[union-attr]
    assert {"official_calls_per_minute", "official_call_credits_per_month"} <= keys


def test_catalog_exposes_all_specs_additively_and_honestly() -> None:
    catalog_ids = {entry.provider_id for entry in list_catalog()}
    assert {spec.id for spec in FREE_PROVIDER_SPECS} <= catalog_ids
    for spec in FREE_PROVIDER_SPECS:
        entry = get_catalog_entry(spec.id)
        assert entry is not None
        assert entry.eligibility == spec.eligibility
        assert entry.terms_url == spec.terms_url
        assert entry.hosts == spec.hosts
        assert entry.runtime_verified is False
        if spec.id != "alpha_vantage":
            ready = spec.id in DISPATCHABLE  # planned entries stay out of provider-scope export
            assert entry.availability == ("implemented" if ready else "planned")
            assert entry.code_available is ready


def test_registry_and_typed_schema_ids_are_equal_for_supported_providers() -> None:
    supported = {spec.id for spec in FREE_PROVIDER_SPECS if spec.execution == "supported"}
    assert supported <= set(PROVIDER_IDS)
    assert {spec.id for spec in FREE_PROVIDER_SPECS} <= set(PROVIDER_IDS)
    for provider in MEASUREMENT_IDS:
        WorldDataMeasurement(provider=provider, metric="m", value=1.0, unit="u", quality="provider_reported")  # type: ignore[arg-type]
    for provider in NEWS_IDS:
        ProviderRecordMetadata(
            provider=provider, identity="x", timestamp_basis="collection",  # type: ignore[arg-type]
            coverage="returned_snapshot", content_truncated=False)


def _measurement(provider: str, **fields: object) -> WorldDataMeasurement:
    return WorldDataMeasurement(
        provider=provider, metric="gdp", value=1.5, unit="USD", quality="provider_reported",  # type: ignore[arg-type]
        provider_fields=fields)  # type: ignore[arg-type]


def test_measurement_provider_fields_follow_per_provider_allowlist() -> None:
    _measurement("world_bank", country="VN", indicator="NY.GDP.MKTP.CD", period="2025", decimal_value="1.5")
    _measurement("ecb", date="2026-10-07", base="EUR", quote="USD", decimal_value="1.1")
    with pytest.raises(ValidationError):
        _measurement("world_bank", symbol="IBM")
    with pytest.raises(ValidationError):
        _measurement("binance", period="2025")


def test_missing_value_still_requires_reason() -> None:
    with pytest.raises(ValidationError):
        WorldDataMeasurement(provider="world_bank", metric="gdp", value=None, unit="USD", quality="missing")
    WorldDataMeasurement(provider="world_bank", metric="gdp", value=None, unit="USD", quality="missing",
                         missing_reason="provider_null")


def test_structured_provider_requires_matching_world_data_and_news_forbids_it() -> None:
    with pytest.raises(ValidationError):
        ProviderRecordMetadata(provider="usgs", identity="us1", timestamp_basis="provider_modified",
                               coverage="returned_snapshot", content_truncated=False)
    measurement = _measurement("usgs", place="X")
    ok = ProviderRecordMetadata(provider="usgs", identity="us1", timestamp_basis="provider_modified",
                                coverage="returned_snapshot", content_truncated=False, world_data=measurement)
    assert ok.world_data == measurement
    with pytest.raises(ValidationError):
        ProviderRecordMetadata(provider="ecb", identity="e", timestamp_basis="collection",
                               coverage="returned_snapshot", content_truncated=False, world_data=measurement)
    with pytest.raises(ValidationError):
        ProviderRecordMetadata(provider="bbc_world", identity="b", timestamp_basis="collection",
                               coverage="returned_snapshot", content_truncated=False, world_data=measurement)


@pytest.mark.parametrize(("provider", "fields", "valid"), [
    ("bbc_world", {"title": "t", "summary": "s", "canonical_url": "https://www.bbc.co.uk/a", "guid": "g",
                   "published_at": "2026-10-07T10:00:00Z", "publisher": "BBC"}, True),
    ("bbc_world", {"canonical_url": "http://insecure.example/a"}, False),
    ("bbc_world", {"by": "someone"}, False),
    ("hn_top", {"title": "t", "url": "https://example.com/a", "by": "u", "time": 1_700_000_000, "id": 1,
                "type": "story", "deleted": False, "dead": False}, True),
    ("hn_top", {"time": "yesterday"}, False),
    ("hn_top", {"id": True}, False),
    ("gdelt_economy", {"title": "t", "url": "https://example.com/a", "seendate": "20261007T100000Z",
                       "domain": "example.com", "language": "English", "sourcecountry": "US"}, True),
    ("gdelt_economy", {"published_at": "2026-10-07T10:00:00Z"}, False),
])
def test_news_source_field_allowlists(provider: str, fields: dict[str, object], valid: bool) -> None:
    def build() -> ProviderRecordMetadata:
        return ProviderRecordMetadata(
            provider=provider, identity="i", timestamp_basis="collection",  # type: ignore[arg-type]
            coverage="returned_snapshot", content_truncated=False, source_fields=fields)

    if valid:
        build()
    else:
        with pytest.raises(ValidationError):
            build()
