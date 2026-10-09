from datetime import UTC, datetime

from modules.connectors.providers.world_data import _market_records
from modules.connectors.public import ConnectorConfig
from tests.unit.modules.connectors.p3_helpers import NOW, jfixture, wd


def test_alpha_vantage_daily_fixture_parses_newest_day_only():
    config = ConnectorConfig(market_symbols=("IBM",), market_currency="USD", market_exchange_timezone="America/New_York")
    records = _market_records(None, config, jfixture("alpha_vantage_ibm_daily.json"), NOW)
    assert [r.provider_id for r in records] == [f"alpha_vantage:IBM:2026-10-07:{m}" for m in ("open", "high", "low", "close", "volume")]
    assert all(r.observed_at == datetime(2026, 10, 7, 4, tzinfo=UTC) and r.collected_at == NOW for r in records)
    close, volume = wd(records[3]), wd(records[4])
    assert close["value"] == 182.75 and close["unit"] == "USD" and close["currency"] == "USD"
    assert volume["value"] == 3125400.0 and volume["unit"] == "shares" and volume["currency"] is None
    meta = records[3].metadata["provider_record"]
    assert meta["timestamp_basis"] == "collection" and meta["provider_version"] is None
