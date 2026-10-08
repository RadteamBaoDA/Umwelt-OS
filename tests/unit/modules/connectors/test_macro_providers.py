from datetime import datetime
from decimal import Decimal

import pytest

from modules.connectors.providers.macro import (
    ProviderPayloadError,
    decimal_text,
    decode_json,
    finite_decimal,
    map_ecb,
    map_frankfurter,
    map_world_bank,
)
from modules.connectors.providers.world_data import map_pure_provider_body
from tests.unit.modules.connectors.p3_helpers import NOW, fixture, jfixture, wd


@pytest.mark.parametrize("bad", [True, None, "NaN", "Infinity", "1_000", " 1", "1e999", float("nan"), float("inf"), [], {}, "", "0x10"])
def test_finite_decimal_rejects(bad):
    with pytest.raises(ValueError, match="provider_measurement_invalid"):
        finite_decimal(bad)


def test_decimal_lexical_precision_survives_json():
    value = decode_json(b'{"v": 0.1234567890123456789012}')["v"]
    assert isinstance(value, Decimal)
    assert decimal_text(finite_decimal(value)) == "0.1234567890123456789012"


def test_world_bank_null_is_not_zero_and_valid_years_kept():
    records = map_world_bank(jfixture("world_bank_vn_gdp.json"), NOW)
    assert [r.provider_id for r in records] == [f"world_bank:VN:NY.GDP.MKTP.CD:{y}" for y in (2025, 2024, 2023)]
    null = wd(records[0])
    assert null["value"] is None and null["quality"] == "missing" and null["missing_reason"] == "provider_null"
    assert null["provider_fields"]["period"] == "2025" and "decimal_value" not in null["provider_fields"]
    good = wd(records[1])
    assert good["value"] == 476388000000.5 and good["currency"] == "USD" and good["quality"] == "provider_reported"
    assert records[1].observed_at.isoformat() == "2024-01-01T00:00:00+00:00"
    assert records[1].metadata["provider_record"]["coverage"] == "truncated"  # pages=22 > 1
    assert records[1].version == records[1].metadata["provider_record"]["provider_version"]


def test_world_bank_deterministic_ids_and_versions():
    a = map_world_bank(jfixture("world_bank_vn_gdp.json"), NOW)
    b = map_world_bank(jfixture("world_bank_vn_gdp.json"), NOW.replace(hour=5))
    assert [(r.provider_id, r.version) for r in a] == [(r.provider_id, r.version) for r in b]


def test_world_bank_decimal_json_path():
    records = map_pure_provider_body("world_bank", fixture("world_bank_vn_gdp.json"), NOW)
    assert wd(records[1])["provider_fields"]["decimal_value"] == "476388000000.5"


@pytest.mark.parametrize("payload", [
    [{"message": [{"id": "120", "key": "Invalid value", "value": "The provided parameter value is not valid"}]}],
    {}, [], [{}, []], "html", None,
    [{"page": 1, "pages": 1, "per_page": 3, "total": 3}, []],  # claims 3, returns 0
    [{"page": 2, "pages": 3, "per_page": 3, "total": 7}, [{}, {}, {}]],
    [{"page": "x", "pages": 1, "per_page": 3, "total": 1}, [{}]],
])
def test_world_bank_hostile(payload):
    with pytest.raises(ProviderPayloadError):
        map_world_bank(payload, NOW)


def _wb_row(date="2024", value=1, cid="VN", iso="VNM", ind="NY.GDP.MKTP.CD"):
    return {"indicator": {"id": ind}, "country": {"id": cid}, "countryiso3code": iso, "date": date, "value": value}


def _wb_page(rows):
    return [{"page": 1, "pages": 1, "per_page": len(rows), "total": len(rows)}, rows]


@pytest.mark.parametrize("rows", [
    [_wb_row(cid="TH")], [_wb_row(iso="THA")], [_wb_row(ind="X")], [_wb_row(date="2999")], [_wb_row(date="24")],
    [_wb_row(), _wb_row()], [_wb_row(value=True)], [_wb_row(value="NaN")], [_wb_row(value="1e500")],
])
def test_world_bank_row_hostile(rows):
    with pytest.raises(ProviderPayloadError):
        map_world_bank(_wb_page(rows), NOW)


def test_world_bank_string_number_ok():
    assert map_world_bank(_wb_page([_wb_row(value="12.50")]), NOW)[0].content.endswith("12.5 USD")


def test_frankfurter_reference_rate():
    (record,) = map_frankfurter(jfixture("frankfurter_usd_vnd.json"), NOW)
    data = wd(record)
    assert record.provider_id == "frankfurter:USD:VND:2026-10-07:blended"
    assert data["quality"] == "reference" and data["currency"] == "VND" and data["unit"] == "VND per USD"
    assert data["provider_fields"] == {
        "date": "2026-10-07", "base": "USD", "quote": "VND", "providers": "ECB,BOC", "decimal_value": "26312.5",
    }
    assert record.observed_at.isoformat() == "2026-10-07T00:00:00+00:00" and record.collected_at == NOW


def test_frankfurter_providers_not_invented():
    payload = {"date": "2026-10-07", "base": "USD", "quote": "VND", "rate": 1}
    assert "providers" not in wd(map_frankfurter(payload, NOW)[0])["provider_fields"]


_FX = {"date": "2026-10-07", "base": "USD", "quote": "VND", "rate": 1}


@pytest.mark.parametrize("payload", [
    [], None, {"message": "not found"}, {k: v for k, v in _FX.items() if k != "rate"},
    {**_FX, "rate": 0}, {**_FX, "rate": -3}, {**_FX, "rate": True}, {**_FX, "rate": "NaN"},
    {**_FX, "date": "2026-13-45"}, {k: v for k, v in _FX.items() if k != "date"},
    {**_FX, "base": "EUR"}, {**_FX, "quote": "THB"},
])
def test_frankfurter_hostile(payload):
    with pytest.raises(ProviderPayloadError):
        map_frankfurter(payload, NOW)


def test_ecb_rates_and_no_vnd():
    records = map_ecb(fixture("ecb_eurofxref_daily.xml"), NOW)
    assert [r.provider_id for r in records] == ["ecb:EUR:USD:2026-10-07", "ecb:EUR:JPY:2026-10-07", "ecb:EUR:GBP:2026-10-07"]
    usd = wd(records[0])
    assert usd["provider_fields"] == {"date": "2026-10-07", "base": "EUR", "quote": "USD", "decimal_value": "1.1628"}
    assert usd["quality"] == "reference" and usd["unit"] == "USD per EUR"
    assert all("VND" not in r.provider_id for r in records)


def _xml(body: str, head: str = "") -> bytes:
    return (head + '<Envelope xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref"><Cube>' + body + "</Cube></Envelope>").encode()


_DAY = '<Cube time="2026-10-07">{}</Cube>'


@pytest.mark.parametrize("xml", ids=lambda x: str(len(x)) + "-" + str(abs(hash(x)) % 10**6), argvalues=[
    b"", b"<html><body>Service unavailable</body></html>", b"<a>", b"x" * 300_000,
    _xml('<Cube currency="USD" rate="1.1"/>'),
    _xml(_DAY.format("")),
    _xml('<Cube time="not-a-date"><Cube currency="USD" rate="1.1"/></Cube>'),
    _xml(_DAY.format('<Cube currency="USD" rate="0"/>')),
    _xml(_DAY.format('<Cube currency="USD" rate="abc"/>')),
    _xml(_DAY.format('<Cube currency="usd" rate="1"/>')),
    _xml(_DAY.format('<Cube currency="EUR" rate="1"/>')),
    _xml(_DAY.format('<Cube currency="USD" rate="1"/><Cube currency="USD" rate="2"/>')),
    _xml(_DAY.format('<Cube currency="USD" rate="1"/>') + '<Cube time="2026-10-08"><Cube currency="USD" rate="1"/></Cube>'),
    _xml(_DAY.format('<Cube currency="USD" rate="&e;"/>'), '<!DOCTYPE x [<!ENTITY e "1">]>'),
    _xml(_DAY.format('<Cube currency="USD" rate="1"/>'), "<!DOCTYPE x>"),
    '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x>'.encode("utf-16"),
    b'<?xml version="1.0" encoding="ISO-8859-1"?>' + _xml(_DAY.format('<Cube currency="USD" rate="1"/>')),
])
def test_ecb_hostile(xml):
    with pytest.raises(ProviderPayloadError):
        map_ecb(xml, NOW)


def test_html_200_is_not_json_and_unknown_provider():
    with pytest.raises(ProviderPayloadError, match="provider_response_not_json"):
        map_pure_provider_body("frankfurter", b"<html>quota</html>", NOW)
    with pytest.raises(ProviderPayloadError, match="provider_scope_invalid"):
        map_pure_provider_body("hn_top", b"{}", NOW)


def test_naive_collected_at_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        map_frankfurter(jfixture("frankfurter_usd_vnd.json"), datetime(2026, 10, 8))  # noqa: DTZ001
