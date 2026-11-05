"""Live-map-only provider terms identities and catalog snapshot metadata."""

from modules.connectors.provider_specs import get_provider_spec

CATALOG_IDS = {
    "usgs": "usgs",
    "eonet": "nasa_eonet",
    "firms": "nasa_firms",
    "hackernews": "hn_top",
    "lobsters": "lobsters",
    "cisa_kev": "cisa_kev",
    "nvd": "nvd",
    "business": "vnexpress_business",
    "fed": "fed_press",
    "github_releases": "live_github_releases",
    "sec": "sec_edgar",
    "openweather": "live_openweather_tiles",
}


def live_terms_catalog() -> dict[str, dict[str, str]]:
    """Return stable review identities; versions identify our snapshots, not provider-granted rights."""
    result = {}
    for live_id, catalog_id in CATALOG_IDS.items():
        spec = get_provider_spec(catalog_id)
        if spec is not None:
            result[live_id] = {
                "label": spec.label,
                "terms_url": spec.terms_url,
                "version": spec.checked_on,
                "eligibility": spec.eligibility,
                "attribution": spec.attribution,
            }
    result["github_releases"] = {
        "label": "GitHub Releases",
        "terms_url": "https://docs.github.com/en/site-policy/github-terms/github-terms-of-service",
        "version": "2026-11-04",
        "eligibility": "review",
        "attribution": "Credit GitHub and the repository; link each original release.",
    }
    result["openweather"] = {
        "label": "OpenWeather map tiles",
        "terms_url": "https://openweathermap.org/terms",
        "version": "2026-11-04",
        "eligibility": "review",
        "attribution": "Weather maps provided by OpenWeather; tiles are reprojected for display.",
    }
    return result


def live_cache_seconds(provider: str) -> int:
    """Use catalog polling intervals for shared snapshots, with dedicated map defaults."""
    spec = get_provider_spec(CATALOG_IDS[provider])
    return (
        spec.default_interval_minutes * 60
        if spec is not None
        else (600 if provider == "openweather" else 3600)
    )
