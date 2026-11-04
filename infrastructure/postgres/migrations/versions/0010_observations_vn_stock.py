"""Allow ``vn_stock`` in ``ck_observations_provider``.

The Vietnamese stock price provider joined ``MEASUREMENT_PROVIDER_IDS`` without widening the database
allow-list, so every ``vn_stock`` observation failed the check and the normalize stage exhausted its
retries. The ids are hardcoded (migrations must not import application constants); a unit test keeps
them equal to ``modules.knowledge.documents.schemas.MEASUREMENT_PROVIDER_IDS``.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010_observations_vn_stock"
down_revision: str | Sequence[str] | None = "0009_system_health_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PREVIOUS_PROVIDER_IDS: tuple[str, ...] = (
    "alpha_vantage", "alternative_me", "binance", "cisa_kev", "coingecko", "coinpaprika", "disease_sh",
    "ecb", "frankfurter", "fred", "ioda", "iss_position", "nasa_eonet", "nasa_firms", "noaa_kp", "nvd",
    "oecd_cli", "open_er_api", "open_meteo", "openaq", "openweathermap", "usgs", "world_bank",
)
_PROVIDER_IDS: tuple[str, ...] = tuple(sorted((*_PREVIOUS_PROVIDER_IDS, "vn_stock")))


def _in_list(ids: tuple[str, ...]) -> str:
    """Render ``ids`` as a quoted SQL list (ids are trusted literals owned by this file)."""
    return ", ".join(f"'{provider}'" for provider in ids)


def _replace_check(ids: tuple[str, ...]) -> None:
    """Swap ``ck_observations_provider`` for an allow-list of ``ids`` inside the migration transaction."""
    op.execute("ALTER TABLE observations DROP CONSTRAINT ck_observations_provider")
    op.execute(
        "ALTER TABLE observations ADD CONSTRAINT ck_observations_provider "
        f"CHECK (provider IN ({_in_list(ids)}))"
    )


def upgrade() -> None:
    """Widen ``ck_observations_provider`` to include ``vn_stock`` (no row changes)."""
    _replace_check(_PROVIDER_IDS)


def downgrade() -> None:
    """Restore the 0002 allow-list; raises (rows untouched) if ``vn_stock`` observations exist.

    Data is never deleted: the guard aborts the transaction so the operator must purge or migrate those rows first.
    """
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM observations WHERE provider = 'vn_stock') THEN "
        "RAISE EXCEPTION 'Cannot downgrade: vn_stock observations exist'; "
        "END IF; END $$"
    )
    _replace_check(_PREVIOUS_PROVIDER_IDS)
