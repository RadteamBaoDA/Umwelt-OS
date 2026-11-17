"""Alpha Vantage envelope translation, numeric parsing and the pure provider-body dispatch."""

from datetime import UTC, datetime, timedelta

import pytest

from modules.connectors.providers import world_data as wd


@pytest.mark.parametrize("raw", ["1.5", 2, "0", "-3.25e2"])
def test_finite_number_accepts_numeric_strings_and_numbers(raw: object) -> None:
    assert wd._finite_number(raw) == float(raw)  # type: ignore[arg-type]


@pytest.mark.parametrize("raw", [None, "abc", "nan", "inf", float("-inf"), [], "1e999"])
def test_finite_number_rejects_missing_and_non_finite_values(raw: object) -> None:
    with pytest.raises(ValueError, match="world_data_measurement_invalid"):
        wd._finite_number(raw)


@pytest.mark.parametrize(
    "key,message,unit",
    [
        ("Note", "Our standard API rate limit is 25 requests per day.", "day"),
        ("Information", "Thank you. Call frequency is 5 requests per minute", "minute"),
        ("Note", "Slow down, rate limit hit", "hour"),
    ],
)
def test_quota_envelopes_become_rate_limits_with_matching_retry_time(
    key: str, message: str, unit: str
) -> None:
    before = datetime.now(UTC)
    with pytest.raises(wd.ProviderRateLimited) as caught:
        wd._raise_for_alpha_vantage_envelope({key: message})
    retry_at = caught.value.next_eligible_at
    assert retry_at > before
    limit = {"minute": timedelta(minutes=1), "hour": timedelta(hours=1), "day": timedelta(days=1)}[
        unit
    ]
    assert retry_at <= before + limit + timedelta(seconds=5)


@pytest.mark.parametrize(
    "payload,code",
    [
        (
            {"Error Message": "Invalid API call. Please retry"},
            "alpha_vantage_symbol_or_endpoint_unavailable",
        ),
        ({"Information": "This is a premium endpoint"}, "alpha_vantage_entitlement_unavailable"),
        ({"Information": "needs entitlement"}, "alpha_vantage_entitlement_unavailable"),
        ({"Note": "something else"}, "alpha_vantage_access_unavailable"),
        ({"Note": 5}, "alpha_vantage_access_unavailable"),
    ],
)
def test_non_quota_envelopes_map_to_stable_error_codes(
    payload: dict[str, object], code: str
) -> None:
    with pytest.raises(ValueError, match=code):
        wd._raise_for_alpha_vantage_envelope(payload)


def test_payload_without_envelope_keys_returns_for_generic_error() -> None:
    assert wd._raise_for_alpha_vantage_envelope({"unrelated": 1}) is None


def test_unknown_pure_provider_is_rejected_before_decoding() -> None:
    with pytest.raises(wd.ProviderPayloadError, match="provider_scope_invalid"):
        wd.map_pure_provider_body("no-such-provider", b"{}", datetime.now(UTC))
