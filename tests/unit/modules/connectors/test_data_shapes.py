"""Contract tests for per-provider connector data shapes."""

from modules.connectors.backends import PROVIDER_SOURCE_TYPES
from modules.connectors.public import source_data_shapes


def test_every_known_provider_has_shapes() -> None:
    """Every registered provider declares at least one shape."""
    for provider, source_type in PROVIDER_SOURCE_TYPES.items():
        assert source_data_shapes(provider, source_type), provider


def test_location_is_separate_from_event() -> None:
    """Location is declared only for adapters that write coordinates."""
    assert {"event", "located", "measurement"} <= source_data_shapes("usgs", "api")
    for provider in ("cisa_kev", "nvd", "launch_library", "ioda", "us_travel_advisories", "sec_edgar", "openalex"):
        assert "located" not in source_data_shapes(provider, "api")


def test_non_catalog_shapes() -> None:
    """Non-catalog and generic providers map to their fixed shapes."""
    assert source_data_shapes("telegram", "api") == {"messages"}
    assert source_data_shapes("youtube", "rss") == {"video"}
    assert source_data_shapes("github", "api") == {"project_activity"}
    assert {"measurement", "weather", "located"} <= source_data_shapes("open_meteo", "api")
    assert source_data_shapes(None, "rss") == {"news"}
    assert source_data_shapes("bbc_world", "rss") == {"news"}


def test_catalog_entries_carry_shapes() -> None:
    """Catalog entries expose shapes derived from the same table."""
    from modules.connectors import catalog

    entry = catalog.get_catalog_entry("noaa_kp")
    assert entry is not None and "measurement" in entry.data_shapes


SERIES_PROVIDERS = (
    "noaa_kp", "binance", "oecd_cli", "frankfurter", "world_bank", "ecb", "coinpaprika", "open_er_api",
    "alternative_me", "disease_sh", "iss_position",
)
POINT_PROVIDERS = ("usgs", "nasa_firms")


def test_series_shape_for_series_providers_only() -> None:
    """Keyless series providers carry ``series`` next to ``measurement``; point-event providers never do."""
    for provider in SERIES_PROVIDERS:
        shapes = source_data_shapes(provider, PROVIDER_SOURCE_TYPES.get(provider, "api"))
        assert {"measurement", "series"} <= shapes, provider
    for provider in POINT_PROVIDERS:
        shapes = source_data_shapes(provider, PROVIDER_SOURCE_TYPES.get(provider, "api"))
        assert "measurement" in shapes and "series" not in shapes, provider


def test_builtin_rows_expose_series_shape() -> None:
    """The built-in source rows served to the Sources wizard carry the same ``series`` shape decisions."""
    from modules.connectors import builtins
    from modules.connectors.provider_specs import BUILTIN_PROVIDERS, get_provider_spec

    for provider in (*SERIES_PROVIDERS, *POINT_PROVIDERS):
        if provider not in BUILTIN_PROVIDERS:
            continue  # keyed/scoped providers are not built-ins; the previous test covers their shapes
        spec = get_provider_spec(provider)
        assert spec is not None, provider
        shapes = builtins._read(spec, None, False).data_shapes
        assert ("series" in shapes) is (provider in SERIES_PROVIDERS), provider
