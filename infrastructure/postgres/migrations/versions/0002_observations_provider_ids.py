"""Widen ``ck_observations_provider`` to every measurement provider.

The baseline only allowed ``alpha_vantage`` and ``open_meteo``, so observations from the other measurement
providers failed with ``IntegrityError`` and the normalize stage exhausted its retries. The ids are
hardcoded (migrations must not import application constants); a unit test keeps them equal to
``modules.knowledge.documents.schemas.MEASUREMENT_PROVIDER_IDS``.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_observations_provider_ids"
down_revision: str | Sequence[str] | None = "0001_init"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROVIDER_IDS: tuple[str, ...] = (
    "alpha_vantage", "alternative_me", "binance", "cisa_kev", "coingecko", "coinpaprika", "disease_sh",
    "ecb", "frankfurter", "fred", "ioda", "iss_position", "nasa_eonet", "nasa_firms", "noaa_kp", "nvd",
    "oecd_cli", "open_er_api", "open_meteo", "openaq", "openweathermap", "usgs", "world_bank",
)
_LEGACY_PROVIDER_IDS: tuple[str, ...] = ("alpha_vantage", "open_meteo")


def _in_list(ids: tuple[str, ...]) -> str:
    """Render ``ids`` as a quoted SQL list (ids are trusted literals owned by this file)."""
    return ", ".join(f"'{provider}'" for provider in ids)


def upgrade() -> None:
    """Replace ``ck_observations_provider`` with the full measurement-provider allow-list (no row changes)."""
    op.execute("ALTER TABLE observations DROP CONSTRAINT ck_observations_provider")
    op.execute(
        "ALTER TABLE observations ADD CONSTRAINT ck_observations_provider "
        f"CHECK (provider IN ({_in_list(_PROVIDER_IDS)}))"
    )


def downgrade() -> None:
    """Restore the two-provider constraint; raises (rows untouched) if other providers' observations exist.

    Data is never deleted: the guard aborts the transaction so the operator must purge or migrate those rows first.
    """
    legacy = _in_list(_LEGACY_PROVIDER_IDS)
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM observations "
        f"WHERE provider NOT IN ({legacy})) THEN "
        "RAISE EXCEPTION 'Cannot downgrade: observations rows exist for providers outside the legacy set'; "
        "END IF; END $$"
    )
    op.execute("ALTER TABLE observations DROP CONSTRAINT ck_observations_provider")
    op.execute(
        "ALTER TABLE observations ADD CONSTRAINT ck_observations_provider "
        f"CHECK (provider IN ({legacy}))"
    )
