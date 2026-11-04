"""VNDirect finfo adapter: request building, mapping, hostile bodies and option validation (offline fixtures)."""

import pytest
from pydantic import ValidationError

from modules.connectors import provider_options as po
from modules.connectors.providers.macro import ProviderPayloadError
from modules.connectors.providers.vn_stock import map_vn_stock, split_symbols, vn_stock_requests
from modules.connectors.providers.world_data import map_pure_provider_body
from modules.connectors.public import ConnectorConfig
from tests.unit.modules.connectors.p3_helpers import NOW, fixture, jfixture, wd


def test_requests_split_stocks_and_indices_into_two_gets() -> None:
    """Default scope: one stock request and one index request, each on the finfo host with bounded size."""
    requests = vn_stock_requests(po.DEFAULT_VN_SYMBOLS)
    assert [group for group, _ in requests] == [("FPT", "VCB", "HPG", "VNM", "MWG"), ("VNINDEX", "VN30")]
    stock, index = (request for _, request in requests)
    assert stock.method == index.method == "GET"
    assert stock.url == "https://api-finfo.vndirect.com.vn/v4/stock_prices?q=code:FPT,VCB,HPG,VNM,MWG&sort=date:desc&size=60"
    assert index.url.startswith("https://api-finfo.vndirect.com.vn/v4/vnmarket_prices?q=code:VNINDEX,VN30&")
    assert split_symbols(("FPT",)) == (("FPT",), ())
    assert len(vn_stock_requests(("VNINDEX",))) == 1


@pytest.mark.parametrize("bad", ["fpt", "F", "FPT&x=1", "FPT,VCB", "VÇB", "A" * 11, ""])
def test_bad_symbol_is_refused_before_a_request_exists(bad: str) -> None:
    """Both the request builder and the option type reject anything outside uppercase alphanumerics."""
    with pytest.raises(ProviderPayloadError):
        vn_stock_requests((bad,))
    with pytest.raises(ValidationError):
        ConnectorConfig(vn_symbols=(bad,))


def test_maps_latest_session_per_code_with_unrelabeled_units() -> None:
    """One record per found requested code; the latest day wins; stocks stay in thousand VND, indices in points."""
    records = {r.provider_id: r for r in map_pure_provider_body("vn_stock", fixture("vn_stock_prices.json"), NOW)}
    assert set(records) == {"vn_stock:FPT:close", "vn_stock:VCB:close", "vn_stock:VNINDEX:close", "vn_stock:VN30:close"}
    fpt = wd(records["vn_stock:FPT:close"])
    assert fpt["metric"] == "close" and fpt["value"] == 50.4 and fpt["unit"] == "thousand VND"
    assert fpt["currency"] is None and fpt["symbol"] == "FPT"
    assert fpt["provider_fields"]["date"] == "2026-10-21" and fpt["provider_fields"]["kind"] == "stock"
    assert fpt["provider_fields"]["open"] == 50.3 and fpt["provider_fields"]["volume"] == 9249300.0
    vnindex = wd(records["vn_stock:VNINDEX:close"])
    assert vnindex["unit"] == "points" and vnindex["provider_fields"]["kind"] == "index"
    assert "ZZZ" not in " ".join(records)  # not requested -> ignored


def test_observed_at_is_provider_session_time_and_version_ignores_collection_clock() -> None:
    """Vietnam local time converts to UTC; the revision key excludes the collection clock."""
    body = jfixture("vn_stock_prices.json")
    collected = NOW.replace(day=22)  # after the fixture session, so observed_at is not clamped to collection time
    (first,) = map_vn_stock(body, collected, symbols=("FPT",))
    (later,) = map_vn_stock(body, collected.replace(hour=9), symbols=("FPT",))
    assert first.observed_at.isoformat() == "2026-10-21T08:11:07+00:00"  # 15:11:07 Vietnam time (UTC+7)
    assert first.version == later.version


def test_requested_code_missing_is_skipped_but_empty_match_fails() -> None:
    """A mistyped ticker must not block the others, but a body with none of the requested codes is an error."""
    body = jfixture("vn_stock_prices.json")
    assert len(map_vn_stock(body, NOW, symbols=("FPT", "NOPE"))) == 1
    with pytest.raises(ProviderPayloadError, match="vn_stock_scope_mismatch"):
        map_vn_stock(body, NOW, symbols=("NOPE",))


@pytest.mark.parametrize("payload", [
    [], {}, {"data": "x"}, {"data": []}, {"data": [1]},
    {"data": [{"code": "FPT", "date": "2026-10-21", "close": None}]},
    {"data": [{"code": "FPT", "date": "2026-10-21", "close": -1}]},
    {"data": [{"code": "FPT", "date": "2026-10-21", "close": True}]},
    {"data": [{"code": "FPT", "date": "21/10/2026", "close": 5}]},
])
def test_hostile_or_empty_payloads_raise_typed_errors(payload: object) -> None:
    """Error, empty or malformed bodies never become records."""
    with pytest.raises(ProviderPayloadError):
        map_vn_stock(payload, NOW, symbols=("FPT",))


def test_vn_symbols_option_validation_and_defaults() -> None:
    """Defaults, de-duplication in the owner's order, mapper scope and the uniqueness/size bounds."""
    assert po.selected_vn_symbols({}) == po.DEFAULT_VN_SYMBOLS
    assert po.selected_vn_symbols({"vn_symbols": ["MWG", "FPT", "MWG"]}) == ("MWG", "FPT")
    assert po.map_options("vn_stock", {"vn_symbols": ["FPT"]}) == {"symbols": ("FPT",)}
    assert "vn_symbols" in po.option_fields("vn_stock")
    with pytest.raises(ValueError, match="unique"):
        po.validate_options("vn_stock", {"vn_symbols": ["FPT", "FPT"]})
    with pytest.raises(ValidationError):
        ConnectorConfig(vn_symbols=tuple(f"AB{n}" for n in range(21)))
