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
    # RSS providers fetched by the stateless BE proxy.
    "bbc_world": "bbc_world",
    "aljazeera": "aljazeera_world",
    "vnexpress_world": "live_vnexpress_world",
    "vnexpress_news": "live_vnexpress_news",
    "gaming_news": "live_gaming_news",
    # Browser-only providers: catalog entries exist so terms can be accepted; never fetched by the BE.
    "gdacs": "gdacs_rss",
    "nws_alerts": "nws_alerts",
    "devto": "live_devto",
    "coingecko": "coingecko",
    "fear_greed": "alternative_me",
    "launches": "launch_library",
    "freetogame": "live_freetogame",
    "gamerpower": "live_gamerpower",
}

# Terms identities for providers without a free-provider spec; the BE proxy (RSS) or the browser fetches them.
_LIVE_ONLY: dict[str, dict[str, str]] = {
    "vnexpress_world": {
        "label": "VnExpress World",
        "terms_url": "https://vnexpress.net/gioi-thieu",
        "eligibility": "review",
        "attribution": "Credit VnExpress and link the original article; feed-only title, summary and link.",
    },
    "vnexpress_news": {
        "label": "VnExpress News",
        "terms_url": "https://vnexpress.net/gioi-thieu",
        "eligibility": "review",
        "attribution": "Credit VnExpress and link the original article; feed-only title, summary and link.",
    },
    "gaming_news": {
        "label": "Eurogamer",
        "terms_url": "https://www.eurogamer.net/terms",
        "eligibility": "review",
        "attribution": "Credit Eurogamer and link the original article; feed-only title, summary and link.",
    },
    "devto": {
        "label": "DEV Community",
        "terms_url": "https://dev.to/terms",
        "eligibility": "review",
        "attribution": "Credit DEV Community and the author; link each original article.",
    },
    "freetogame": {
        "label": "FreeToGame",
        "terms_url": "https://www.freetogame.com/api-doc",
        "eligibility": "review",
        "attribution": "Credit FreeToGame.com and link back as the API terms require.",
    },
    "gamerpower": {
        "label": "GamerPower",
        "terms_url": "https://www.gamerpower.com/api-read",
        "eligibility": "review",
        "attribution": "Credit GamerPower.com and link back as the API terms require.",
    },
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
    for live_id, item in _LIVE_ONLY.items():
        result[live_id] = {"version": "2026-11-12", **item}
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
        else (600 if provider == "openweather" else 1800 if provider in _LIVE_ONLY else 3600)
    )
