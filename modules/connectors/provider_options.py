"""Per-type owner options for hazard, security, tech and economy built-ins (wave D).

Pure and I/O-free. Every value an owner can send is an enum or a bounded number validated by the types below
(or by ``ConnectorConfig``), and every URL is assembled here from server-side constants, so no owner text
reaches a request line unescaped. ``build_url`` returns ``None`` whenever the configuration asks for nothing
beyond today's request; callers then keep the provider's untouched request builder, which is what guarantees
that default sources stay byte-for-byte unchanged.

"native" options change the upstream request; "client" options (region, KEV ransomware flag, NVD severity
bands) filter mapped records. Native-resume start parameters are supported only where a stored cursor can exist
(``INCREMENTAL_PROVIDERS``): ``gdelt_economy`` and ``launch_library``. ``nvd``, ``usgs`` and ``nasa_eonet`` are
measurement snapshots with no cursor, so they have nothing to resume from.

Wave F adds the remaining built-ins: Wikipedia language, SatNOGS status, disease.sh / World Bank / OECD scope,
FX currencies, Binance pairs, CoinPaprika coins (one gated call each), and the NOAA Kp / travel-advisory
client thresholds. Currency, pair and coin lists are curated server-side constants; none of them reaches a
request line except through the Literal types below.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, Literal, get_args
from urllib.parse import quote, urlencode

from pydantic import StringConstraints

if TYPE_CHECKING:
    # Annotation-only: a runtime import would close an import cycle between public contracts.
    from modules.ingestion.public import IngestionRecord

HnList = Literal["top", "new", "best", "ask", "show"]
GdeltTimespan = Literal["1h", "6h", "24h", "3d", "7d"]
GdeltLang = Literal["english", "vietnamese", "chinese", "japanese", "korean", "french", "german", "spanish"]
GdeltCountry = Literal["US", "VN", "CN", "JP", "GB", "DE", "FR", "IN", "KR", "SG", "AU", "RU", "BR", "CA"]
UsgsMagnitude = Literal["significant", "4.5", "2.5", "1.0", "all"]
UsgsPeriod = Literal["hour", "day", "week"]
EonetCategory = Literal[
    "drought", "dustHaze", "earthquakes", "floods", "landslides", "manmade", "seaLakeIce", "severeStorms",
    "snow", "tempExtremes", "volcanoes", "waterColor", "wildfires",
]
EonetStatus = Literal["open", "all"]
GdacsType = Literal["EQ", "TC", "FL", "VO", "WF", "DR"]
GdacsAlert = Literal["Green", "Orange", "Red"]
NwsSeverity = Literal["Extreme", "Severe", "Moderate", "Minor"]
NwsArea = Literal[
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY",
    "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
]
NvdSeverity = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
Region = Literal[
    "world", "southeast_asia", "vietnam", "east_asia", "south_asia", "europe", "north_america",
    "south_america", "africa", "oceania", "middle_east",
]

WikiLanguage = Literal["en", "vi", "ja", "zh", "fr", "de", "es", "ko"]
SatnogsStatus = Literal["good", "bad", "unknown"]
IsoCountry = Literal[
    "VN", "US", "JP", "CN", "GB", "DE", "FR", "IN", "KR", "SG", "AU", "CA", "BR", "ID", "TH", "MY", "PH", "IT", "ES",
    "RU", "MX", "ZA", "TR", "NL", "CH",
]
WbIndicator = Literal["gdp", "gdp_growth", "inflation_cpi", "unemployment", "population", "fdi"]
# Union of every currency any FX provider below lists; per-provider membership is checked in validate_options.
FxCurrency = Literal[
    "EUR", "USD", "JPY", "GBP", "CNY", "VND", "KRW", "SGD", "AUD", "CAD", "THB", "INR", "CHF", "HKD", "IDR", "MYR",
    "PHP", "BGN", "CZK", "DKK", "HUF", "PLN", "RON", "SEK", "ISK", "NOK", "TRY", "BRL", "ILS", "MXN", "NZD", "ZAR",
]
OecdArea = Literal[
    "USA", "CHN", "JPN", "DEU", "GBR", "FRA", "ITA", "CAN", "KOR", "IND", "BRA", "AUS", "ESP", "MEX", "TUR", "IDN",
]
CryptoPair = Literal[
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "TRXUSDT", "LINKUSDT", "DOTUSDT",
    "LTCUSDT", "AVAXUSDT",
]
# VNDirect finfo ticker/index code: uppercase alphanumerics only, so it can never carry URL syntax into a request.
VnSymbol = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9]{2,10}$")]
# Codes served by the finfo ``vnmarket_prices`` endpoint (verified live 2026-11-04); every other code is a stock.
VN_INDEX_CODES = ("VNINDEX", "VN30", "VN100", "HNX", "HNX30", "UPCOM")
DEFAULT_VN_SYMBOLS = ("VNINDEX", "VN30", "FPT", "VCB", "HPG", "VNM", "MWG")
MAX_VN_SYMBOLS = 20
# Job-board filters shared by ``vietnamworks_jobs`` and ``itviec_jobs``: bounded enums and strings.
JobCity = Literal["ho_chi_minh", "ha_noi", "da_nang"]
JobLevel = Literal["intern", "fresher", "experienced", "manager", "director"]
JobKeyword = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
MAX_JOB_KEYWORDS = 5
DEFAULT_JOB_KEYWORDS = ("developer",)
MAX_ITVIEC_SEARCHES = 3  # keyword x city search pages per ITviec run (each page is ~0.5 MB)
CoinId = Literal[
    "btc-bitcoin", "eth-ethereum", "bnb-binance-coin", "sol-solana", "xrp-xrp", "ada-cardano", "doge-dogecoin",
    "ltc-litecoin", "dot-polkadot", "trx-tron",
]
COIN_SYMBOLS = {
    "btc-bitcoin": "BTC", "eth-ethereum": "ETH", "bnb-binance-coin": "BNB", "sol-solana": "SOL", "xrp-xrp": "XRP",
    "ada-cardano": "ADA", "doge-dogecoin": "DOGE", "ltc-litecoin": "LTC", "dot-polkadot": "DOT", "trx-tron": "TRX",
}
# Curated currency sets per provider (ECB daily reference set; Frankfurter adds EUR and VND; open.er-api list).
ECB_CURRENCIES = frozenset({
    "USD", "JPY", "BGN", "CZK", "DKK", "GBP", "HUF", "PLN", "RON", "SEK", "CHF", "ISK", "NOK", "TRY", "AUD", "BRL",
    "CAD", "CNY", "HKD", "IDR", "ILS", "INR", "KRW", "MXN", "MYR", "NZD", "PHP", "SGD", "THB", "ZAR",
})
FRANKFURTER_CURRENCIES = ECB_CURRENCIES | {"EUR", "VND"}
OER_CURRENCIES = frozenset({
    "USD", "EUR", "JPY", "GBP", "CNY", "VND", "KRW", "SGD", "AUD", "CAD", "THB", "INR", "CHF", "HKD", "IDR", "MYR", "PHP",
})
OER_DEFAULT_QUOTES = ("VND", "EUR", "JPY", "GBP", "CNY")  # mirrors macro.OPEN_ER_QUOTES for base USD
# World Bank indicator -> (id, metric name, unit shown on the record)
WB_INDICATORS = {
    "gdp": ("NY.GDP.MKTP.CD", "gdp_current_usd"),
    "gdp_growth": ("NY.GDP.MKTP.KD.ZG", "gdp_growth_pct"),
    "inflation_cpi": ("FP.CPI.TOTL.ZG", "inflation_cpi_pct"),
    "unemployment": ("SL.UEM.TOTL.ZS", "unemployment_pct"),
    "population": ("SP.POP.TOTL", "population"),
    "fdi": ("BX.KLT.DINV.WD.GD.ZS", "fdi_net_inflows_pct_gdp"),
}
COINPAPRIKA_GAP_SECONDS = 1.1  # the pilot quota allows one call per second; calls in one run are spaced past it
MAX_COINS = 5

ISO_COUNTRY_PATTERN = r"^[A-Z]{2}$"

# Provider-specific option names (the common selection filters are allowed separately by INCREMENTAL_PROVIDERS).
OPTION_FIELDS: dict[str, tuple[str, ...]] = {
    "hn_top": ("hn_list",),
    "gdelt_economy": ("gdelt_query", "gdelt_timespan", "gdelt_source_lang", "gdelt_source_country"),
    "usgs": ("usgs_min_magnitude", "usgs_period", "region"),
    "emsc_quakes": ("min_magnitude", "region"),
    "nasa_eonet": ("eonet_categories", "eonet_status", "eonet_days", "region"),
    "gdacs_events": ("gdacs_event_types", "gdacs_alert_levels", "region"),
    "nws_alerts": ("nws_severity", "nws_area"),
    "nvd": ("max_items", "nvd_severity", "nvd_window_days", "nvd_keyword"),
    "cisa_kev": ("kev_ransomware_only",),
    "ioda": ("ioda_country",),
    "bmkg_quakes": ("region",),
    "wikipedia_mostread": ("wiki_language",),
    "wikipedia_top_views": ("wiki_language",),
    "satnogs_observations": ("satnogs_status",),
    "disease_sh": ("disease_country",),
    "world_bank": ("wb_country", "wb_indicator"),
    "frankfurter": ("fx_base", "fx_quotes"),
    "ecb": ("fx_quotes",),
    "open_er_api": ("fx_base", "fx_quotes"),
    "oecd_cli": ("oecd_countries",),
    "binance": ("crypto_symbols",),
    "vn_stock": ("vn_symbols",),
    "vietnamworks_jobs": ("job_keywords", "job_cities", "job_levels", "job_min_salary_usd"),
    "itviec_jobs": ("job_keywords", "job_cities", "job_levels", "job_min_salary_usd"),
    "coinpaprika": ("coin_ids",),
    "noaa_kp": ("kp_min",),
    "us_travel_advisories": ("advisory_min_level",),
}
ALL_OPTION_NAMES = frozenset(name for names in OPTION_FIELDS.values() for name in names)

# max_items only ever lowers an adapter's own request cap (never raises a fail-closed one).
UPSTREAM_CAPS = {"hn_top": 30, "gdelt_economy": 50, "emsc_quakes": 50, "nvd": 20, "launch_library": 25}
_DEFAULT_COUNT = {"hn_top": 10, "gdelt_economy": 5, "emsc_quakes": 50, "nvd": 20, "launch_library": 10}

# Bounding boxes (lat_min, lat_max, lon_min, lon_max). None = whole world. None crosses the antimeridian.
REGION_BOXES: dict[str, tuple[float, float, float, float] | None] = {
    "world": None,
    "southeast_asia": (-11.5, 28.5, 92.0, 141.0),
    "vietnam": (8.0, 24.5, 102.0, 110.0),
    "east_asia": (18.0, 54.0, 100.0, 146.0),
    "south_asia": (5.0, 38.0, 60.0, 98.0),
    "europe": (35.0, 72.0, -25.0, 45.0),
    "north_america": (7.0, 84.0, -170.0, -50.0),
    "south_america": (-56.0, 13.0, -82.0, -34.0),
    "africa": (-35.0, 38.0, -18.0, 52.0),
    "oceania": (-50.0, 0.0, 110.0, 180.0),
    "middle_east": (12.0, 42.0, 34.0, 63.0),
}

_FIPS = {"US": "US", "VN": "VM", "CN": "CH", "JP": "JA", "GB": "UK", "DE": "GM", "FR": "FR", "IN": "IN",
         "KR": "KS", "SG": "SN", "AU": "AS", "RU": "RS", "BR": "BR", "CA": "CA"}  # GDELT sourcecountry takes FIPS codes
_GDELT_WINDOWS = {"1h": timedelta(hours=1), "6h": timedelta(hours=6), "24h": timedelta(hours=24),
                  "3d": timedelta(days=3), "7d": timedelta(days=7)}
_LEVEL = re.compile(r"\bLevel\s*([1-4])\b", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_OPERATOR = re.compile(r"(?i)\b(?:site|domain|domainis|sourcelang|sourcecountry)\s*:")
_COORDS = (
    re.compile(r"Location:\s*(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)"),
    re.compile(r"Coordinates:\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)"),
)


def option_fields(provider: str) -> frozenset[str]:
    """Return the per-type option names ``provider`` accepts (empty for providers with none)."""
    return frozenset(OPTION_FIELDS.get(provider, ()))


def clean_query(value: str) -> str:
    """Strip control characters, fold whitespace and refuse operators that bypass the enum selectors.

    Used for ``gdelt_query`` and ``nvd_keyword``. The result is URL-encoded by the builders, so this guards
    query semantics (source/domain operators), not injection.

    Raises:
        ValueError: when the text carries a ``site:``/``domain:``/``sourcelang:``/``sourcecountry:`` operator.
    """
    cleaned = " ".join(_CONTROL.sub(" ", value).split())
    if _OPERATOR.search(cleaned):
        raise ValueError("Use the selectors instead of source operators in the query")
    return cleaned


def _ordered(literal: Any, chosen: Iterable[str]) -> list[str]:
    """Deduplicate ``chosen`` and put it in the Literal's declared order so equal choices give equal URLs."""
    wanted = set(chosen)
    return [item for item in get_args(literal) if item in wanted]


def validate_options(provider: str, cfg: Mapping[str, Any]) -> None:
    """Reject option combinations the field types alone cannot catch.

    Raises:
        ValueError: when ``max_items`` exceeds NVD's page size (only ``nvd`` rejects; other providers clamp
        their upstream request instead, which keeps previously valid saved sources valid), a multi-select
        repeats a value, an FX quote equals its base or is not listed by that provider (ECB publishes no VND,
        so ``ecb`` accepts only its own reference set), or more than ``MAX_COINS`` coins are requested.
    """
    if provider == "nvd" and (cfg.get("max_items") or 0) > UPSTREAM_CAPS["nvd"]:
        raise ValueError("NVD returns at most 20 CVEs per sync")
    for name in (
        "eonet_categories", "gdacs_event_types", "gdacs_alert_levels", "nws_severity", "nvd_severity", "fx_quotes",
        "oecd_countries", "crypto_symbols", "coin_ids", "vn_symbols",
        "job_cities", "job_levels",
    ):
        values = cfg.get(name)
        if values is not None and len(set(values)) != len(values):
            raise ValueError("Selections must be unique")
    keywords = [k.casefold() for k in cfg.get("job_keywords") or ()]
    if len(set(keywords)) != len(keywords):
        raise ValueError("Selections must be unique")
    if provider == "itviec_jobs" and len(selected_job_keywords(cfg)) * max(1, len(cfg.get("job_cities") or ())) > MAX_ITVIEC_SEARCHES:
        raise ValueError(f"ITviec allows at most {MAX_ITVIEC_SEARCHES} keyword x city searches per source")
    if len(cfg.get("coin_ids") or ()) > MAX_COINS:
        raise ValueError(f"At most {MAX_COINS} coins per source")
    listed = {"ecb": ECB_CURRENCIES, "frankfurter": FRANKFURTER_CURRENCIES, "open_er_api": OER_CURRENCIES}.get(provider)
    if listed is not None:
        quotes = cfg.get("fx_quotes") or ()
        for currency in (cfg.get("fx_base"), *quotes):
            if currency is not None and currency not in listed:
                raise ValueError(f"{currency} is not offered by this provider")
        if (cfg.get("fx_base") or ("EUR" if provider == "ecb" else "USD")) in quotes:
            raise ValueError("A quote currency cannot equal the base")


def _count(provider: str, cfg: Mapping[str, Any]) -> int:
    """Upstream item count: ``max_items`` clamped to the adapter cap, else the adapter default."""
    requested = cfg.get("max_items")
    return min(requested, UPSTREAM_CAPS[provider]) if requested else _DEFAULT_COUNT[provider]


def hn_limit(cfg: Mapping[str, Any]) -> int:
    """Hacker News item fan-out (1 listing send + this many item sends): ``max_items`` up to the hard cap 30."""
    return _count("hn_top", cfg)


def _gdelt_query(cfg: Mapping[str, Any]) -> str:
    """GDELT query text: owner query (default ``economy``) plus in-query language/country operators."""
    parts = [cfg.get("gdelt_query") or "economy"]
    if cfg.get("gdelt_source_lang"):
        parts.append(f"sourcelang:{cfg['gdelt_source_lang']}")
    if cfg.get("gdelt_source_country"):
        parts.append(f"sourcecountry:{_FIPS[cfg['gdelt_source_country']]}")
    return " ".join(parts)


def _usgs_url(cfg: Mapping[str, Any]) -> str | None:
    """USGS summary feed for the chosen magnitude/period; ``None`` when both are unset (today's request)."""
    mag, period = cfg.get("usgs_min_magnitude"), cfg.get("usgs_period")
    if mag is None and period is None:
        return None
    return ("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/"
            f"{mag or '4.5'}_{period or 'day'}.geojson")


def _emsc_url(provider: str, cfg: Mapping[str, Any]) -> str | None:
    """EMSC FDSN query for the chosen limit/magnitude; ``None`` when neither is set."""
    if cfg.get("min_magnitude") is None and not cfg.get("max_items"):
        return None
    mag = cfg.get("min_magnitude")
    return ("https://www.seismicportal.eu/fdsnws/event/1/query"
            f"?limit={_count(provider, cfg)}&format=json&minmag={'4' if mag is None else format(mag, 'g')}")


def _gdacs_url(cfg: Mapping[str, Any]) -> str | None:
    """GDACS event-list query for the chosen types/alert levels; ``None`` when both are unset."""
    if cfg.get("gdacs_event_types") is None and cfg.get("gdacs_alert_levels") is None:
        return None
    kinds = ";".join(_ordered(GdacsType, cfg.get("gdacs_event_types") or get_args(GdacsType)))
    levels = ";".join(_ordered(GdacsAlert, cfg.get("gdacs_alert_levels") or ("Orange", "Red")))
    return f"https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH?eventlist={kinds}&alertlevel={levels}"


def _nws_url(cfg: Mapping[str, Any]) -> str | None:
    """NWS active-alerts query for the chosen severity/area; ``None`` when both are unset."""
    if cfg.get("nws_severity") is None and cfg.get("nws_area") is None:
        return None
    severities = ",".join(_ordered(NwsSeverity, cfg.get("nws_severity") or ("Extreme", "Severe")))
    area = f"&area={cfg['nws_area']}" if cfg.get("nws_area") else ""
    return f"https://api.weather.gov/alerts/active?status=actual&message_type=alert&severity={severities}{area}"


def _ioda_url(cfg: Mapping[str, Any], now: datetime) -> str | None:
    """IODA outage-event query for the last 24 h in the chosen country; ``None`` when no country is set."""
    if not cfg.get("ioda_country"):
        return None
    from modules.connectors.providers import security

    start = int((now - timedelta(days=1)).timestamp())
    return (f"{security.IODA_BASE}?from={start}&until={int(now.timestamp())}&entityType=country"
            f"&entityCode={cfg['ioda_country']}&limit={security._IODA_MAX_EVENTS}")


def build_url(provider: str, cfg: Mapping[str, Any], *, cursor: str | None, now: datetime) -> str | None:
    """Return the request URL for ``provider`` under ``cfg``, or ``None`` when today's request applies.

    ``cursor`` (an ISO instant) is pushed as a native start parameter for ``gdelt_economy``
    (``startdatetime``, replacing ``timespan``) and ``launch_library`` (``last_updated__gte``); the runner still runs
    ``select_incremental`` afterwards, so the native bound is an optimisation, not the correctness guard.
    """
    if provider == "hn_top":
        listing = cfg.get("hn_list")
        return None if listing in (None, "top") else f"https://hacker-news.firebaseio.com/v0/{listing}stories.json"
    if provider == "gdelt_economy":
        return _gdelt_url(cfg, cursor)
    if provider == "usgs":
        return _usgs_url(cfg)
    if provider == "emsc_quakes":
        return _emsc_url(provider, cfg)
    if provider == "nasa_eonet":
        return _eonet_url(cfg)
    if provider == "gdacs_events":
        return _gdacs_url(cfg)
    if provider == "nws_alerts":
        return _nws_url(cfg)
    if provider == "nvd":
        return _nvd_url(cfg, now)
    if provider == "ioda":
        return _ioda_url(cfg, now)
    if provider == "launch_library":
        return _launch_url(cfg, cursor)
    return _wave_f_url(provider, cfg, now)


def _fx_pair(cfg: Mapping[str, Any]) -> tuple[str, list[str]]:
    """Frankfurter ``(base, quotes)``: defaults USD and VND (USD when the base itself is VND)."""
    base = cfg.get("fx_base") or "USD"
    quotes = _ordered(FxCurrency, cfg.get("fx_quotes") or ()) or ["USD" if base == "VND" else "VND"]
    return base, quotes


def _wave_f_url(provider: str, cfg: Mapping[str, Any], now: datetime) -> str | None:
    """Wave F request URLs; ``None`` whenever the options equal today's fixed request."""
    if provider in ("wikipedia_mostread", "wikipedia_top_views"):
        from modules.connectors.providers import json_feed

        lang = str(cfg.get("wiki_language") or "en")
        return None if lang == "en" else json_feed.wiki_url(provider, lang, now)
    if provider == "satnogs_observations":
        status = cfg.get("satnogs_status")
        return None if status in (None, "good") else f"https://network.satnogs.org/api/observations/?format=json&status={status}"
    if provider == "disease_sh":
        return f"https://disease.sh/v3/covid-19/countries/{cfg['disease_country']}" if cfg.get("disease_country") else None
    if provider == "world_bank":
        country = cfg.get("wb_country") or "VN"
        indicator = WB_INDICATORS[cfg.get("wb_indicator") or "gdp"][0]
        if (country, indicator) == ("VN", WB_INDICATORS["gdp"][0]):
            return None
        return f"https://api.worldbank.org/v2/country/{country}/indicator/{indicator}?format=json&per_page=3"
    if provider == "frankfurter":
        base, quotes = _fx_pair(cfg)
        if (base, quotes) == ("USD", ["VND"]):
            return None
        if len(quotes) == 1:
            return f"https://api.frankfurter.dev/v2/rate/{base.lower()}/{quotes[0].lower()}"
        return f"https://api.frankfurter.dev/v2/rates?base={base}&quotes={','.join(quotes)}"
    if provider == "open_er_api":
        oer_base = cfg.get("fx_base")
        return None if oer_base in (None, "USD") else f"https://open.er-api.com/v6/latest/{oer_base}"
    if provider == "oecd_cli":
        if not cfg.get("oecd_countries"):
            return None
        from modules.connectors.providers import macro

        areas = "+".join(_ordered(OecdArea, cfg["oecd_countries"]))
        url = macro.OECD_CLI_URL.replace("USA+CHN+JPN+DEU+GBR", areas, 1)
        return None if url == macro.OECD_CLI_URL else url
    if provider == "binance":
        symbols = _ordered(CryptoPair, cfg.get("crypto_symbols") or ())
        if not symbols or symbols == ["BTCUSDT"]:
            return None
        base = "https://data-api.binance.vision/api/v3/ticker/price"
        if len(symbols) == 1:
            return f"{base}?symbol={symbols[0]}"
        return f"{base}?symbols={quote(json.dumps(symbols, separators=(',', ':')), safe='')}"  # ONE batched call
    return None


def coin_urls(cfg: Mapping[str, Any]) -> list[str]:
    """CoinPaprika ticker URLs, one per selected coin (default ``btc-bitcoin``); each is its own gated, debited call."""
    coins = _ordered(CoinId, cfg.get("coin_ids") or ("btc-bitcoin",))
    return [f"https://api.coinpaprika.com/v1/tickers/{coin}" for coin in coins]


def selected_vn_symbols(cfg: Mapping[str, Any]) -> tuple[str, ...]:
    """Owner VN tickers/indices de-duplicated in first-seen order (default ``DEFAULT_VN_SYMBOLS``).

    Order is the owner's order, not a curated one: the symbol set is open (any listed ticker), so there is no
    Literal to sort by. Equal selections therefore still send equal requests.
    """
    return tuple(dict.fromkeys(cfg.get("vn_symbols") or DEFAULT_VN_SYMBOLS))


def selected_job_keywords(cfg: Mapping[str, Any]) -> tuple[str, ...]:
    """Owner job keywords de-duplicated case-insensitively in first-seen order (default ``developer``)."""
    seen: dict[str, str] = {}
    for keyword in cfg.get("job_keywords") or DEFAULT_JOB_KEYWORDS:
        seen.setdefault(keyword.casefold(), keyword)
    return tuple(seen.values())[:MAX_JOB_KEYWORDS]


def selected_coins(cfg: Mapping[str, Any]) -> list[str]:
    """CoinPaprika coin ids in the same curated order as ``coin_urls`` (default ``btc-bitcoin``)."""
    return _ordered(CoinId, cfg.get("coin_ids") or ("btc-bitcoin",))


def map_options(provider: str, cfg: Mapping[str, Any]) -> dict[str, Any]:
    """Keyword arguments for the provider's pure mapper so its scope check matches the requested scope.

    Empty at defaults, which keeps the mappers' original behaviour for existing sources.
    """
    if provider == "world_bank" and (cfg.get("wb_country") or cfg.get("wb_indicator")):
        indicator, metric = WB_INDICATORS[cfg.get("wb_indicator") or "gdp"]
        return {"indicators": ((cfg.get("wb_country") or "VN", indicator, metric),)}
    if provider == "binance" and cfg.get("crypto_symbols"):
        return {"pairs": tuple((s, s.removesuffix("USDT"), "USDT") for s in _ordered(CryptoPair, cfg["crypto_symbols"]))}
    if provider == "vn_stock":
        return {"symbols": selected_vn_symbols(cfg)}
    if provider == "frankfurter" and (cfg.get("fx_base") or cfg.get("fx_quotes")):
        base, quotes = _fx_pair(cfg)
        return {"base": base, "quotes": tuple(quotes)}
    if provider == "open_er_api" and cfg.get("fx_base"):
        return {"base": cfg["fx_base"]}
    if provider == "disease_sh" and cfg.get("disease_country"):
        return {"country": cfg["disease_country"]}
    if provider in ("wikipedia_mostread", "wikipedia_top_views") and cfg.get("wiki_language") not in (None, "en"):
        return {"wiki_language": cfg["wiki_language"]}
    return {}


def _gdelt_url(cfg: Mapping[str, Any], cursor: str | None) -> str | None:
    """GDELT DOC URL; the default configuration without a cursor returns ``None`` (today's builder is used)."""
    from modules.connectors.providers import gdelt
    from modules.connectors.selection import SKEW

    plain = not any(cfg.get(k) for k in ("gdelt_query", "gdelt_timespan", "gdelt_source_lang", "gdelt_source_country"))
    if plain and not cfg.get("max_items") and cursor is None:
        return None
    params: dict[str, Any] = {
        "query": _gdelt_query(cfg), "mode": "artlist", "format": "json", "maxrecords": _count("gdelt_economy", cfg),
    }
    start = _parse_cursor(cursor)
    if start is not None:
        params["startdatetime"] = (start - SKEW).strftime("%Y%m%d%H%M%S")  # GDELT: replaces timespan
    else:
        params["timespan"] = cfg.get("gdelt_timespan") or "24h"
    return f"{gdelt._BASE}?{urlencode(params)}"


def _eonet_url(cfg: Mapping[str, Any]) -> str | None:
    """EONET v3 events URL with optional category/days narrowing; ``None`` at defaults."""
    if cfg.get("eonet_categories") is None and cfg.get("eonet_status") is None and cfg.get("eonet_days") is None:
        return None
    url = f"https://eonet.gsfc.nasa.gov/api/v3/events?status={cfg.get('eonet_status') or 'open'}&limit=50"
    if cfg.get("eonet_categories"):
        url += "&category=" + ",".join(_ordered(EonetCategory, cfg["eonet_categories"]))
    if cfg.get("eonet_days"):
        url += f"&days={cfg['eonet_days']}"
    return url


def _nvd_url(cfg: Mapping[str, Any], now: datetime) -> str | None:
    """NVD CVE URL: window, optional keyword, single-severity native push, page = ``max_items`` (<= 20)."""
    if not any(cfg.get(k) for k in ("max_items", "nvd_severity", "nvd_window_days", "nvd_keyword")):
        return None
    from modules.connectors.providers import security

    fmt = "%Y-%m-%dT%H:%M:%S.000"
    start = (now - timedelta(days=cfg.get("nvd_window_days") or 1)).strftime(fmt)
    url = (f"{security.NVD_BASE}?resultsPerPage={_count('nvd', cfg)}&pubStartDate={start}"
           f"&pubEndDate={now.strftime(fmt)}")
    severities: Sequence[str] = cfg.get("nvd_severity") or []
    if len(severities) == 1:  # NVD takes one cvssV3Severity; several are banded client-side in filter_records
        url += f"&cvssV3Severity={severities[0]}"
    if cfg.get("nvd_keyword"):
        url += f"&keywordSearch={quote(cfg['nvd_keyword'], safe='')}"
    return url


def _launch_url(cfg: Mapping[str, Any], cursor: str | None) -> str | None:
    """Launch Library upcoming URL with ``limit`` = ``max_items`` and ``last_updated__gte`` from the cursor."""
    if not cfg.get("max_items") and cursor is None:
        return None
    url = f"https://ll.thespacedevs.com/2.2.0/launch/upcoming/?limit={_count('launch_library', cfg)}&mode=list"
    start = _parse_cursor(cursor)
    if start is not None:
        url += f"&last_updated__gte={quote(start.isoformat(), safe='')}"
    return url


def _parse_cursor(cursor: str | None) -> datetime | None:
    """Parse a stored ISO cursor; foreign or missing values mean "no native start"."""
    if not cursor:
        return None
    try:
        return datetime.fromisoformat(cursor)
    except ValueError:
        return None


def native_fingerprint(provider: str, cfg: Mapping[str, Any]) -> str | None:
    """Return a short stable hash of the native query parameters, or ``None`` for providers without any.

    Only providers with a cursor (``INCREMENTAL_PROVIDERS``, not dedup-only) and a native query option have
    one. Unset options are normalised to their defaults, so "unset" equals "explicitly the default". Count
    (``max_items``), client filters and region are excluded: they never change which upstream items exist.
    """
    params: dict[str, Any]
    if provider == "gdelt_economy":
        params = {"q": cfg.get("gdelt_query") or "economy", "t": cfg.get("gdelt_timespan") or "24h",
                  "l": cfg.get("gdelt_source_lang"), "c": cfg.get("gdelt_source_country")}
    elif provider == "emsc_quakes":
        params = {"m": 4.0 if cfg.get("min_magnitude") is None else float(cfg["min_magnitude"])}
    elif provider == "gdacs_events":
        params = {"t": _ordered(GdacsType, cfg.get("gdacs_event_types") or get_args(GdacsType)),
                  "a": _ordered(GdacsAlert, cfg.get("gdacs_alert_levels") or ("Orange", "Red"))}
    elif provider == "nws_alerts":
        params = {"s": _ordered(NwsSeverity, cfg.get("nws_severity") or ("Extreme", "Severe")),
                  "a": cfg.get("nws_area")}
    elif provider == "satnogs_observations":
        params = {"s": cfg.get("satnogs_status") or "good"}
    else:
        return None
    digest = hashlib.sha256(json.dumps(params, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"n1:{digest[:16]}"


def default_window(provider: str, cfg: Mapping[str, Any]) -> timedelta:
    """Look-back used when a native query change resets the cursor ("now minus the provider's default window")."""
    if provider == "gdelt_economy":
        return _GDELT_WINDOWS[cfg.get("gdelt_timespan") or "24h"]
    return timedelta(days=7) if provider == "gdacs_events" else timedelta(days=1)


def _coordinates(record: IngestionRecord) -> tuple[float, float] | None:
    """Return ``(lat, lon)`` from the typed measurement, else from the mapped summary text, else ``None``."""
    world = (record.metadata.get("provider_record") or {}).get("world_data") or {}
    if isinstance(world.get("latitude"), (int, float)) and isinstance(world.get("longitude"), (int, float)):
        return float(world["latitude"]), float(world["longitude"])
    for pattern in _COORDS:
        if match := pattern.search(record.content):
            return float(match.group(1)), float(match.group(2))
    return None


def in_region(record: IngestionRecord, region: str) -> bool:
    """Whether the record lies inside ``region``'s box. Records without coordinates are kept (ruling).

    Dropping them would hide events whose location the provider omitted; the owner narrowed by area, not by
    "must have a location", so an unlocated record is shown rather than silently lost.
    """
    box = REGION_BOXES[region]
    where = _coordinates(record)
    if box is None or where is None:
        return True
    lat, lon = where
    return box[0] <= lat <= box[1] and box[2] <= lon <= box[3]


def _severity_band(score: float | None) -> str | None:
    """CVSS v3 qualitative band for a base score (``None`` when unscored)."""
    if score is None or score <= 0:
        return None
    return "LOW" if score < 4 else "MEDIUM" if score < 7 else "HIGH" if score < 9 else "CRITICAL"


def filter_records(provider: str, cfg: Mapping[str, Any], records: Sequence[IngestionRecord]) -> list[IngestionRecord]:
    """Apply the client-side per-type filters (region, KEV flag, NVD bands, FX quote subset, Kp and advisory thresholds).

    Runs on mapped records before incremental selection. With no relevant option the input is returned as is.
    """
    out = list(records)
    region = cfg.get("region")
    if region and provider in OPTION_FIELDS and "region" in OPTION_FIELDS[provider]:
        out = [r for r in out if in_region(r, region)]
    if provider == "cisa_kev" and cfg.get("kev_ransomware_only"):
        out = [r for r in out if (((r.metadata.get("provider_record") or {}).get("world_data") or {})
                                  .get("provider_fields") or {}).get("knownRansomwareCampaignUse") == "Known"]
    if provider in ("ecb", "open_er_api") and cfg.get("fx_quotes"):
        wanted_quotes = set(cfg["fx_quotes"])
        out = [r for r in out if (((r.metadata.get("provider_record") or {}).get("world_data") or {})
                                  .get("provider_fields") or {}).get("quote") in wanted_quotes]
    if provider == "noaa_kp" and cfg.get("kp_min"):
        out = [r for r in out if (((r.metadata.get("provider_record") or {}).get("world_data") or {})
                                  .get("value") or 0) >= cfg["kp_min"]]
    if provider == "us_travel_advisories" and cfg.get("advisory_min_level"):
        # A title without "Level N" (e.g. "Worldwide Caution") cannot be ranked, so it is kept rather than hidden.
        out = [r for r in out if (m := _LEVEL.search(str(r.metadata.get("title", "")))) is None
               or int(m.group(1)) >= cfg["advisory_min_level"]]
    if provider == "nvd" and cfg.get("nvd_severity"):
        wanted = set(cfg["nvd_severity"])
        out = [r for r in out if _severity_band(
            ((r.metadata.get("provider_record") or {}).get("world_data") or {}).get("value")) in wanted]
    return out
