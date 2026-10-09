"""Observation read exposes FX reference date and annual period derived from stored provider timestamps."""
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from modules.knowledge.observations.schemas import ObservationRead


def _row(provider: str, metric: str, observed_at: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(), source_id=uuid4(), provider=provider, provider_scope_discriminator="d", source_generation=1,
        external_id="x", revision=1, metric=metric, symbol=None, region=None, latitude=None, longitude=None,
        observed_at=observed_at, published_at=None, collected_at=datetime(2026, 10, 9, tzinfo=UTC), value=1.0,
        unit="u", currency=None, timezone=None, quality="q", missing_reason=None, provider_delay_seconds=None,
        document_id=uuid4(), document_version_id=uuid4(),
    )


def test_fx_reference_date_and_annual_period() -> None:
    fx = ObservationRead.model_validate(_row("frankfurter", "fx_reference_rate", datetime(2026, 10, 8, tzinfo=UTC)))
    assert str(fx.reference_date) == "2026-10-08" and fx.period is None
    gdp = ObservationRead.model_validate(_row("world_bank", "gdp_current_usd", datetime(2024, 1, 1, tzinfo=UTC)))
    assert gdp.period == "2024" and gdp.reference_date is None
    dumped = gdp.model_dump(mode="json")
    assert dumped["period"] == "2024" and "published_at" in dumped and "collected_at" in dumped
    btc = ObservationRead.model_validate(_row("binance", "btc_price", datetime(2026, 10, 9, tzinfo=UTC)))
    assert btc.reference_date is None and btc.period is None
