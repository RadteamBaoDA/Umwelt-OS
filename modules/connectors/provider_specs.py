"""Free-provider catalog facts, eligibility rules and durable quota policy.

Pure data and decisions only: no I/O, no secrets. Availability of an endpoint is not a
license grant, and nothing here asserts runtime acceptance. Facts were checked on
``CHECKED_ON`` against the policy in ``docs/free-data-provider-policy.md``; unknown official
caps are recorded as ``limit_units=None`` (never unlimited).
"""

from dataclasses import dataclass, replace
from typing import Literal

CHECKED_ON = "2026-10-07"
QUOTA_POLICY_REVISION = 1

DeploymentUse = Literal["personal", "noncommercial", "commercial", "unknown"]
DEPLOYMENT_USES: tuple[str, ...] = ("personal", "noncommercial", "commercial", "unknown")
Eligibility = Literal["open", "personal", "noncommercial", "review"]
_USE_RANK = {"personal": 0, "noncommercial": 1, "commercial": 2}

TERMS_INELIGIBLE = "provider_terms_ineligible"


@dataclass(frozen=True)
class QuotaWindowPolicy:
    """One durable budget window; ``limit_units=None`` means counted with no enforced cap."""
    policy_key: str
    budget_kind: Literal["provider", "credential", "ip"]
    window: Literal["second", "minute", "day", "month"]
    unit: Literal["http_calls", "request_weight", "call_credits"]
    limit_units: int | None
    cost_per_send: int = 1
    basis: Literal["pilot_local", "official_documented", "official_unknown"] = "pilot_local"


# Durable call-count budgets for providers whose use is governed by connector credentials,
# local service capacity, or per-method API limits rather than free-provider terms. These do
# not imply entitlement; adapter-specific request bounds remain authoritative as well.
_NON_FREE_QUOTA_POLICIES: dict[str, tuple[QuotaWindowPolicy, ...]] = {
    provider_id: (
        QuotaWindowPolicy("pilot_calls_utc_minute", "provider", "minute", "http_calls", 30),
        # Google mail's bounded first snapshot can read 500 messages plus profile/list-page overhead.
        QuotaWindowPolicy("pilot_calls_utc_day", "provider", "day", "http_calls",
                          600 if provider_id.startswith("google_") else 500),
    )
    for provider_id in (
        "health", "home_assistant", "google_mail", "google_calendar", "google_drive", "notion", "slack", "reddit",
    )
}


@dataclass(frozen=True)
class FreeProviderSpec:
    """Catalog facts and execution bounds; availability is not a license grant."""
    id: str
    hosts: tuple[str, ...]
    data_kind: Literal["news", "measurement", "event"]
    key_required: bool
    default_interval_minutes: int
    terms_url: str
    eligibility: Literal["open", "personal", "noncommercial", "review"]
    attribution: str
    execution: Literal["supported", "experimental", "research_only"]
    label: str = ""
    endpoints: tuple[str, ...] = ()
    quota: tuple[QuotaWindowPolicy, ...] = ()
    # Evidence kinds stay separate; no field implies another. Adapters ship in P2/P3/C3.
    endpoint_probe: Literal["plan_documented", "unverified"] = "plan_documented"
    code_implemented: bool = False
    runtime_verified: bool = False
    checked_on: str = CHECKED_ON
    secret_location: Literal["header", "query", "path"] | None = None
    category: Literal["news", "economy", "hazards", "space", "security", "tech"] = "news"


# Providers the shared executor can dispatch (ADAPTERS or registered SIMPLE_NATIVE providers).
# CoinGecko requires an owner-entered API key (connector_rest_credentials) and fails closed without one.
# Code presence only; runtime_verified stays False.
DISPATCHABLE = frozenset({
    "bbc_world", "vnexpress_business", "dantri_news", "thanhnien_news", "tuoitre_news", "hn_top", "gdelt_economy",
    "world_bank", "frankfurter", "ecb", "binance", "alternative_me", "usgs", "coinpaprika",
    "coingecko", "google_news", "nasa_eonet", "cisa_kev", "sec_edgar", "openalex", "bluesky",
    # Built-in RSS/Atom world-data feeds (providers/news.py FEED_URLS).
    "aljazeera_world", "npr_news", "fed_press", "gdacs_rss", "lobsters", "us_travel_advisories",
    "noaa_tsunami", "jma_eqvol",
    # Built-in table-driven JSON feeds (providers/json_feed.py JSON_FEEDS).
    "gdacs_events", "nws_alerts", "emsc_quakes", "launch_library", "wikipedia_mostread", "wikipedia_top_views", "bmkg_quakes", "satnogs_observations",
    "noaa_kp", "open_er_api", "oecd_cli", "iss_position", "disease_sh", "ioda", "nvd",  # bespoke keyless mappers
    # Keyed free-tier presets (providers/keyed.py); each needs an owner-entered key and fails closed without one.
    "fred", "finnhub_news", "newsapi", "openweathermap", "openaq", "nasa_firms",
})


def _unknown(kind: str = "provider", unit: str = "http_calls", cost: int = 1) -> QuotaWindowPolicy:
    return QuotaWindowPolicy(
        "official_unknown_calls_utc_day", kind, "day", unit, None, cost, "official_unknown",  # type: ignore[arg-type]
    )


def _day(limit: int, kind: str = "provider", unit: str = "http_calls", cost: int = 1,
         key: str = "pilot_calls_utc_day") -> QuotaWindowPolicy:
    return QuotaWindowPolicy(key, kind, "day", unit, limit, cost)  # type: ignore[arg-type]


def _spec(id_: str, label: str, category: str, hosts: tuple[str, ...], kind: str, interval: int, terms_url: str,
          eligibility: str, attribution: str, endpoints: tuple[str, ...], quota: tuple[QuotaWindowPolicy, ...],
          execution: str = "supported", key: bool = False, secret: str | None = None,
          implemented: bool = False) -> FreeProviderSpec:
    return FreeProviderSpec(
        id=id_, hosts=hosts, data_kind=kind, key_required=key, default_interval_minutes=interval,  # type: ignore[arg-type]
        terms_url=terms_url, eligibility=eligibility, attribution=attribution, execution=execution,  # type: ignore[arg-type]
        label=label, category=category, endpoints=endpoints, quota=quota, secret_location=secret,  # type: ignore[arg-type]
        code_implemented=implemented or id_ in DISPATCHABLE,
    )


_SPECS_RAW: tuple[FreeProviderSpec, ...] = (
    _spec("bbc_world", "BBC World News", "news", ("feeds.bbci.co.uk",), "news", 30,
          "https://www.bbc.co.uk/usingthebbc/terms/", "review",
          "Display BBC as publisher with a link to the original article; feed-only title, summary and link.",
          ("/news/world/rss.xml",), (_day(96), _unknown())),
    _spec("vnexpress_business", "VnExpress Business", "news", ("vnexpress.net",), "news", 30,
          "https://vnexpress.net/rss", "noncommercial",
          "Identify VnExpress clearly and link the original article; feed-only scope.",
          ("/rss/kinh-doanh.rss",), (_day(96), _unknown())),
    _spec("tuoitre_news", "Tuổi Trẻ News", "news", ("tuoitre.vn",), "news", 30,
          "https://tuoitre.vn/rss.htm", "noncommercial",
          "Display Tuổi Trẻ clearly as source and link the original article",
          ("/home.rss",), (_day(96), _unknown())),
    _spec("thanhnien_news", "Báo Thanh Niên", "news", ("thanhnien.vn",), "news", 30,
          "https://thanhnien.vn/rss.html", "noncommercial",
          'Credit "Báo Thanh Niên" or "Theo Báo Thanh Niên" and link the original article',
          ("/rss/home.rss",), (_day(96), _unknown())),
    _spec("dantri_news", "Dân trí News", "news", ("dantri.com.vn",), "news", 30,
          "https://dantri.com.vn/rss.htm", "noncommercial",
          "Credit Dân trí as the source and link the original article; feed-only title, summary and link.",
          ("/rss/home.rss",), (_day(96), _unknown())),
    # Unofficial RSS search endpoint: Google publishes no API contract for it. Interval and pilot cap
    # mirror the other 30-minute news feeds (docs/free-data-provider-policy.md quota table); no official quota is claimed.
    _spec("google_news", "Google News RSS", "news", ("news.google.com",), "news", 30,
          "https://policies.google.com/terms", "review",
          "Google News RSS / publisher: show the item's publisher and link to the original article; feed-only title, summary and link.",
          ("/rss/search",), (_day(96), _unknown())),
    # SEC fair access: max 10 requests/second and a declared User-Agent with contact details (owner email in scope).
    _spec("sec_edgar", "SEC EDGAR filings", "economy", ("data.sec.gov",), "event", 360,
          "https://www.sec.gov/privacy.htm#security", "open", "Source: U.S. SEC EDGAR",
          ("/submissions/CIK##########.json",), (_day(96), _unknown())),
    # OpenAlex works search: data is CC0 (help.openalex.org); basic use needs no key (a free key only raises the
    # daily budget). The 100 req/s cap is far above the one daily send; the local pilot cap stays small.
    _spec("openalex", "OpenAlex works search", "tech", ("api.openalex.org",), "event", 1440,
          "https://help.openalex.org/how-to-use-the-api/api-overview", "open",
          "Credit OpenAlex; data is CC0. Link the work's OpenAlex id and DOI; abstracts are rebuilt from the inverted index.",
          ("/works",), (_day(8), _unknown())),
    replace(_spec(
        "bluesky", "Bluesky public account feed", "tech", ("public.api.bsky.app", "bsky.app"), "news", 30,
        "https://bsky.social/about/support/tos", "review",
        "Attribute Bluesky and link to each original post; Bluesky terms do not establish redistribution rights for post content.",
        ("/xrpc/app.bsky.feed.getAuthorFeed",), (_unknown(),), execution="supported",
    ), checked_on="2026-10-24"),
    _spec("hn_top", "Hacker News top stories", "tech", ("hacker-news.firebaseio.com",), "news", 60,
          "https://github.com/HackerNews/API", "review",
          "Attribute Hacker News plus the item and publisher URL; the API license does not license linked articles.",
          ("/v0/topstories.json", "/v0/item/{id}.json"),
          (_day(528), QuotaWindowPolicy("pilot_calls_utc_minute", "provider", "minute", "http_calls", 12))),
    _spec("gdelt_economy", "GDELT economy articles", "news", ("api.gdeltproject.org",), "news", 60,
          "https://gdeltproject.org/about.html", "review",
          "Cite the GDELT Project and the publisher URL; article metadata is not republication permission.",
          ("/api/v2/doc/doc?query=economy&mode=artlist&format=json&maxrecords=5&timespan=24h",),
          (_day(48), _unknown()), execution="experimental"),
    _spec("world_bank", "World Bank annual GDP (Vietnam)", "economy", ("api.worldbank.org",), "measurement", 1440,
          "https://datacatalog.worldbank.org/public-licenses", "open",
          "Credit the World Bank and the original indicator; retain the dataset license and changed notice.",
          ("/v2/country/VN/indicator/NY.GDP.MKTP.CD?format=json&per_page=3",), (_day(32), _unknown())),
    _spec("frankfurter", "Frankfurter USD/VND", "economy", ("api.frankfurter.dev",), "measurement", 1440,
          "https://frankfurter.dev/license/", "open",
          "Credit Frankfurter and contributing providers; the default rate is a blended rate, not a single-bank rate.",
          ("/v2/rate/usd/vnd",), (_day(8), _unknown())),
    _spec("ecb", "ECB euro reference rates", "economy", ("www.ecb.europa.eu",), "measurement", 1440,
          "https://www.ecb.europa.eu/services/using-our-site/disclaimer/html/index.en.html", "open",
          "Credit the ECB; preserve the rate date and identify transformations.",
          ("/stats/eurofxref/eurofxref-daily.xml",), (_day(8), _unknown())),
    _spec("binance", "Binance BTCUSDT price", "economy", ("data-api.binance.vision",), "measurement", 15,
          "https://developers.binance.com/en/docs/products/spot/rest-api", "review",
          "Credit Binance; BTCUSDT market, USDT quote. No trading endpoints or keys.",
          ("/api/v3/ticker/price?symbol=BTCUSDT",),
          (_day(384, unit="request_weight", cost=2, key="pilot_request_weight_utc_day"),
           QuotaWindowPolicy("pilot_request_weight_utc_minute", "provider", "minute", "request_weight", 4, 2),
           QuotaWindowPolicy("official_request_weight_unknown", "ip", "minute", "request_weight", None, 2,
                             "official_unknown"))),
    _spec("alternative_me", "Alternative.me Fear and Greed", "economy", ("api.alternative.me",), "measurement", 1440,
          "https://alternative.me/crypto/fear-and-greed-index/", "open",
          "Display Alternative.me with a link adjacent to the index, including translated output.",
          ("/fng/?limit=2",), (_day(8), _unknown())),
    _spec("usgs", "USGS M4.5+ earthquakes (day)", "hazards", ("earthquake.usgs.gov",), "event", 60,
          "https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits", "open",
          "Credit the U.S. Geological Survey with the event citation; do not fetch detail URLs automatically.",
          ("/earthquakes/feed/v1.0/summary/4.5_day.geojson",), (_day(48), _unknown())),
    # EONET publishes only a visualization/general-information disclaimer, no license or key/rate-limit terms.
    _spec("nasa_eonet", "NASA EONET open natural events", "hazards", ("eonet.gsfc.nasa.gov",), "event", 60,
          "https://eonet.gsfc.nasa.gov/what-is-eonet#disclaimer", "review",
          "Credit NASA EONET and the event's listed sources; data is for visualization and general information only, not official extents.",
          ("/api/v3/events?status=open&limit=50",), (_day(48), _unknown())),
    _spec("cisa_kev", "CISA Known Exploited Vulnerabilities", "security", ("www.cisa.gov",), "event", 1440,
          "https://www.cisa.gov/known-exploited-vulnerabilities-catalog", "open",
          "Credit CISA Known Exploited Vulnerabilities Catalog (official feed, CC0).",
          ("/sites/default/files/feeds/known_exploited_vulnerabilities.json",), (_day(8), _unknown())),
    _spec("coinpaprika", "CoinPaprika BTC ticker", "economy", ("api.coinpaprika.com",), "measurement", 15,
          "https://docs.coinpaprika.com/api-plans", "personal",
          "Credit CoinPaprika; free use is personal with no redistribution.",
          ("/v1/tickers/btc-bitcoin",),
          (_day(192), QuotaWindowPolicy("pilot_calls_utc_second", "provider", "second", "http_calls", 1),
           QuotaWindowPolicy("official_calls_per_second", "ip", "second", "http_calls", 10, 1, "official_documented"),
           QuotaWindowPolicy("official_calls_per_month", "ip", "month", "http_calls", 20_000, 1, "official_documented"))),
    _spec("coingecko", "CoinGecko BTC price (Demo)", "economy", ("api.coingecko.com",), "measurement", 15,
          "https://www.coingecko.com/en/api_terms", "review",
          "Show Powered by CoinGecko with a link per the API terms and brand guide.",
          ("/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",),
          (_day(192, kind="credential"),
           QuotaWindowPolicy("pilot_calls_utc_second", "credential", "second", "http_calls", 1),
           QuotaWindowPolicy("official_calls_per_minute", "credential", "minute", "http_calls", 100, 1,
                             "official_documented"),
           QuotaWindowPolicy("official_call_credits_per_month", "credential", "month", "call_credits", 10_000, 1,
                             "official_documented")),
          key=True, secret="header"),
    # Existing C1 adapter. 12/UTC-day is the conservative pilot cap until the provider reset
    # anchor is evidenced; the official 25/day fact is recorded separately.
    _spec("alpha_vantage", "Alpha Vantage daily equities", "economy", ("www.alphavantage.co",), "measurement", 1440,
          "https://www.alphavantage.co/terms_of_service/", "personal",
          "Credit Alpha Vantage with symbol and day; no free realtime entitlement.",
          ("/query?function=TIME_SERIES_DAILY&symbol={symbol}&outputsize=compact",),
          (_day(12, kind="credential"),
           QuotaWindowPolicy("pilot_calls_utc_minute", "credential", "minute", "http_calls", 1),
           QuotaWindowPolicy("official_calls_per_day", "credential", "day", "http_calls", 25, 1,
                             "official_documented")),
          key=True, secret="query", implemented=True),
    # ---- Built-in RSS/Atom world-data feeds. Feed-only title/summary/link; the linked page is never fetched.
    # Eligibility follows each publisher's published terms; where a free-reuse statement is not explicit it is review.
    _spec("aljazeera_world", "Al Jazeera World News", "news", ("www.aljazeera.com",), "news", 30,
          "https://www.aljazeera.com/terms-and-conditions", "review",
          "Credit Al Jazeera and link the original article; feed-only title, summary and link.",
          ("/xml/rss/all.xml",), (_day(96), _unknown())),
    _spec("npr_news", "NPR News", "news", ("feeds.npr.org",), "news", 30,
          "https://www.npr.org/about-npr/179876898/terms-of-use", "noncommercial",
          "Credit NPR and link the original story; RSS is licensed for personal, noncommercial use.",
          ("/1001/rss.xml",), (_day(96), _unknown())),
    _spec("fed_press", "Federal Reserve press releases", "economy", ("www.federalreserve.gov",), "news", 60,
          "https://www.federalreserve.gov/disclaimer.htm", "open",
          "Credit the Board of Governors of the Federal Reserve System; link the original release.",
          ("/feeds/press_all.xml",), (_day(48), _unknown())),
    _spec("gdacs_rss", "GDACS disaster alerts (RSS)", "hazards", ("www.gdacs.org",), "event", 60,
          "https://www.gdacs.org/About/overview.aspx", "open",
          "Credit GDACS (UN OCHA / European Commission JRC) and link the event page.",
          ("/xml/rss.xml",), (_day(48), _unknown())),
    _spec("lobsters", "Lobsters", "tech", ("lobste.rs",), "news", 60,
          "https://lobste.rs/about", "review",
          "Credit Lobsters and link the story; the feed carries submitter links and titles only.",
          ("/rss",), (_day(48), _unknown())),
    _spec("us_travel_advisories", "U.S. travel advisories", "hazards", ("travel.state.gov",), "event", 360,
          "https://www.state.gov/copyright-information/", "open",
          "Credit the U.S. Department of State; advisories are a point-in-time statement, check the original.",
          ("/_res/rss/TAsTWs.xml",), (_day(12), _unknown())),
    _spec("noaa_tsunami", "NOAA tsunami bulletins", "hazards", ("www.tsunami.gov",), "event", 30,
          "https://www.tsunami.gov/", "open",
          "Credit the NOAA/NWS Tsunami Warning Centers; this is not an official warning channel.",
          ("/events/xml/PHEBAtom.xml",), (_day(96), _unknown())),
    _spec("jma_eqvol", "JMA earthquake and volcano bulletins", "hazards", ("www.data.jma.go.jp",), "event", 30,
          "https://www.jma.go.jp/jma/en/copyright.html", "open",
          "Source: Japan Meteorological Agency; state any processing (feed title and link only).",
          ("/developer/xml/feed/eqvol.xml",), (_day(96), _unknown())),
    # ---- Built-in JSON list/event feeds (providers/json_feed.py). Endpoint shapes probed 2026-10-21.
    _spec("gdacs_events", "GDACS Orange/Red disaster events", "hazards", ("www.gdacs.org",), "event", 60,
          "https://www.gdacs.org/About/overview.aspx", "open",
          "Credit GDACS (UN OCHA / European Commission JRC) and link the event report.",
          ("/gdacsapi/api/events/geteventlist/SEARCH?eventlist=EQ;TC;FL;VO;WF;DR&alertlevel=Orange;Red",), (_day(48), _unknown())),
    _spec("nws_alerts", "NWS severe alerts (US)", "hazards", ("api.weather.gov",), "event", 15,
          "https://www.weather.gov/disclaimer", "open",
          "Credit the U.S. National Weather Service; alerts expire, check the original.",
          ("/alerts/active?status=actual&message_type=alert&severity=Extreme,Severe",), (_day(192), _unknown())),
    _spec("emsc_quakes", "EMSC earthquakes (M4+)", "hazards", ("www.seismicportal.eu",), "event", 30,
          "https://www.emsc-csem.org/", "open",
          "Credit EMSC (Euro-Mediterranean Seismological Centre); magnitudes are preliminary.",
          ("/fdsnws/event/1/query?limit=50&format=json&minmag=4",), (_day(96), _unknown())),
    # Launch Library free tier: documented 15 requests/hour, non-commercial. 120-minute interval stays far below it.
    _spec("launch_library", "Launch Library upcoming launches", "space", ("ll.thespacedevs.com",), "event", 360,
          "https://thespacedevs.com/llapi", "noncommercial",
          "Credit The Space Devs Launch Library; free tier is non-commercial with a 15 requests/hour limit.",
          ("/2.2.0/launch/upcoming/?limit=10&mode=list",),
          (_day(12), _unknown())),
    _spec("wikipedia_mostread", "Wikipedia most-read articles", "tech", ("api.wikimedia.org",), "news", 1440,
          "https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use", "open",
          "Credit Wikipedia (CC BY-SA 4.0) and link the article; counts are for the previous UTC day.",
          ("/feed/v1/wikipedia/en/featured/{yyyy}/{mm}/{dd}",), (_day(4), _unknown())),
    _spec("wikipedia_top_views", "Wikipedia top page views", "tech", ("wikimedia.org",), "news", 1440,
          "https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use", "open",
          "Credit Wikimedia pageviews (CC0 data) and link the article; counts are for the previous UTC day.",
          ("/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/{yyyy}/{mm}/{dd}",), (_day(4), _unknown())),
    _spec("bmkg_quakes", "BMKG recent earthquakes (Indonesia)", "hazards", ("data.bmkg.go.id",), "event", 30,
          "https://data.bmkg.go.id/", "open",
          "Source: BMKG (Badan Meteorologi, Klimatologi, dan Geofisika); state any processing.",
          ("/DataMKG/TEWS/gempaterkini.json",), (_day(96), _unknown())),
    _spec("satnogs_observations", "SatNOGS recent observations", "space", ("network.satnogs.org",), "event", 360,
          "https://db.satnogs.org/about", "open",
          "Credit SatNOGS Network (CC BY-SA 4.0); station observations are community-contributed.",
          ("/api/observations/?format=json&status=good",), (_day(12), _unknown())),
    # ---- Bespoke keyless mappers. Endpoint shapes probed 2026-10-21.
    _spec("noaa_kp", "NOAA planetary Kp index", "space", ("services.swpc.noaa.gov",), "measurement", 360,
          "https://www.weather.gov/disclaimer", "open",
          "Credit NOAA Space Weather Prediction Center; Kp is a 3-hour planetary index (0-9).",
          ("/products/noaa-planetary-k-index.json",), (_day(16), _unknown())),
    _spec("open_er_api", "Open ER API daily USD rates", "economy", ("open.er-api.com",), "measurement", 1440,
          "https://www.exchangerate-api.com/terms", "review",
          "Rates By Exchange Rate API (https://www.exchangerate-api.com); attribution is required on the free plan.",
          ("/v6/latest/USD",), (_day(4), _unknown())),
    # OECD documents roughly 20 queries/hour for anonymous SDMX access; one daily send is far below it.
    _spec("oecd_cli", "OECD composite leading indicators", "economy", ("sdmx.oecd.org",), "measurement", 1440,
          "https://www.oecd.org/en/about/terms-conditions.html", "open",
          "Source: OECD (CC BY 4.0); cite the dataset OECD.SDD.STES DF_CLI and state any processing.",
          (("/public/rest/data/OECD.SDD.STES,DSD_STES@DF_CLI/USA+CHN+JPN+DEU+GBR.M.LI...AA...H"
            "?lastNObservations=1&dimensionAtObservation=AllDimensions"),), (_day(4), _unknown())),
    _spec("iss_position", "ISS position", "space", ("api.wheretheiss.at",), "measurement", 15,
          "https://wheretheiss.at/w/developer", "noncommercial",
          "Credit wheretheiss.at; the API is free for non-commercial use with a documented ~1 request/second cap.",
          ("/v1/satellites/25544",), (_day(96), QuotaWindowPolicy("pilot_calls_utc_second", "provider", "second", "http_calls", 1))),
    _spec("disease_sh", "disease.sh global COVID-19 totals", "tech", ("disease.sh",), "measurement", 1440,
          "https://disease.sh/", "review",
          "Credit disease.sh and its upstream sources (Johns Hopkins CSSE and others); totals may be stale.",
          ("/v3/covid-19/all",), (_day(4), _unknown())),
    _spec("ioda", "IODA internet outage events", "security", ("api.ioda.inetintel.cc.gatech.edu",), "event", 60,
          "https://ioda.inetintel.cc.gatech.edu/", "noncommercial",
          "Credit the Georgia Tech IODA project; the service is a research tool, terms are not a license grant.",
          ("/v2/outages/events?from={unix}&until={unix}&entityType=country&limit=50",), (_day(24), _unknown())),
    # NVD keyless limit: 5 requests per rolling 30 seconds; one small page every six hours stays far below it.
    _spec("nvd", "NVD recent CVEs", "security", ("services.nvd.nist.gov",), "event", 360,
          "https://nvd.nist.gov/general/FAQ-Sections/General-FAQs", "open",
          "Credit the NIST National Vulnerability Database; this product uses NVD data but is not endorsed by NIST.",
          ("/rest/json/cves/2.0?resultsPerPage=20&pubStartDate={start}&pubEndDate={end}",), (_day(8), _unknown())),
    # ---- Keyed free-tier presets (providers/keyed.py). Owner-entered key in connector_rest_credentials; response
    # shapes follow the providers' public API documentation (not probed: no key is available to probe with).
    _spec("fred", "FRED series (latest observations)", "economy", ("api.stlouisfed.org",), "measurement", 1440,
          "https://fred.stlouisfed.org/docs/api/terms_of_use.html", "review",
          "This product uses the FRED API but is not endorsed or certified by the Federal Reserve Bank of St. Louis; "
          "some series carry third-party copyright.",
          ("/fred/series/observations?file_type=json&sort_order=desc&limit=5&series_id={series}",),
          (_day(8, kind="credential"),
           QuotaWindowPolicy("official_calls_per_minute", "credential", "minute", "http_calls", 120, 1,
                             "official_documented")),
          key=True, secret="query"),
    _spec("finnhub_news", "Finnhub market news", "news", ("finnhub.io",), "news", 60,
          "https://finnhub.io/terms-of-service", "personal",
          "Credit Finnhub and link the original article; the free plan is for personal use.",
          ("/api/v1/news?category=general",),
          (_day(48, kind="credential"),
           QuotaWindowPolicy("official_calls_per_minute", "credential", "minute", "http_calls", 60, 1,
                             "official_documented")),
          key=True, secret="header"),
    _spec("newsapi", "NewsAPI top headlines (US business)", "news", ("newsapi.org",), "news", 360,
          "https://newsapi.org/terms", "review",
          "Credit NewsAPI.org and the article's publisher; the free Developer plan is for development use only.",
          ("/v2/top-headlines?country=us&category=business&pageSize=20",),
          (_day(12, kind="credential"),
           QuotaWindowPolicy("official_calls_per_day", "credential", "day", "http_calls", 100, 1,
                             "official_documented")),
          key=True, secret="header"),
    _spec("openweathermap", "OpenWeather current conditions", "hazards", ("api.openweathermap.org",), "measurement", 60,
          "https://openweathermap.org/api", "open",
          "Weather data provided by OpenWeather (CC BY-SA 4.0); state any processing.",
          ("/data/2.5/weather?units=metric&lat={lat}&lon={lon}",),
          (_day(48, kind="credential"),
           QuotaWindowPolicy("official_calls_per_minute", "credential", "minute", "http_calls", 60, 1,
                             "official_documented")),
          key=True, secret="query"),
    _spec("openaq", "OpenAQ latest air quality", "hazards", ("api.openaq.org",), "measurement", 60,
          "https://docs.openaq.org/about/about", "open",
          "Credit OpenAQ and the originating data provider; data licenses vary per provider (mostly CC BY 4.0).",
          ("/v3/locations/{location}/sensors",), (_day(48, kind="credential"), _unknown("credential")),
          key=True, secret="header"),
    _spec("nasa_firms", "NASA FIRMS active fires (VIIRS, 24h)", "hazards", ("firms.modaps.eosdis.nasa.gov",), "event", 360,
          "https://www.earthdata.nasa.gov/engage/open-data-services-software-policies", "open",
          "Credit NASA FIRMS (LANCE/EOSDIS); hotspots are detections, not confirmed fires.",
          ("/api/country/csv/{MAP_KEY}/VIIRS_SNPP_NRT/{country}/1",), (_day(16, kind="credential"), _unknown("credential")),
          key=True, secret="path"),
)

# The built-in expansion (RSS, JSON, bespoke and keyed presets) was checked later than the original 21 presets;
# the date doubles as the terms version the owner acknowledges, so it must not claim the older check.
EXPANSION_CHECKED_ON = "2026-10-21"
_EXPANSION_IDS = frozenset({
    "npr_news",
    "fed_press",
    "gdacs_rss",
    "lobsters",
    "us_travel_advisories",
    "noaa_tsunami",
    "jma_eqvol",
    "gdacs_events",
    "nws_alerts",
    "emsc_quakes",
    "launch_library",
    "wikipedia_mostread",
    "wikipedia_top_views",
    "bmkg_quakes",
    "satnogs_observations",
    "noaa_kp",
    "open_er_api",
    "oecd_cli",
    "iss_position",
    "disease_sh",
    "ioda",
    "nvd",
    "fred",
    "finnhub_news",
    "newsapi",
    "openweathermap",
    "openaq",
    "nasa_firms",
})
FREE_PROVIDER_SPECS: tuple[FreeProviderSpec, ...] = tuple(
    replace(spec, checked_on=EXPANSION_CHECKED_ON) if spec.id in _EXPANSION_IDS else spec for spec in _SPECS_RAW
)

_BY_ID = {spec.id: spec for spec in FREE_PROVIDER_SPECS}
assert len(_BY_ID) == len(FREE_PROVIDER_SPECS)  # import-time catalog invariant


def get_provider_spec(provider_id: str | None) -> FreeProviderSpec | None:
    """Return the exact free-provider spec, or None for any other provider."""
    return _BY_ID.get(provider_id or "")


def get_quota_policies(provider_id: str | None) -> tuple[QuotaWindowPolicy, ...]:
    """Return durable send budgets without coupling non-free connectors to terms eligibility.

    Free providers use their documented catalog policy. Google, workspace apps, and explicitly
    configured local services use a conservative instance-wide pilot call budget; no credentials,
    grants, or provider terms are inferred from this policy. Unknown providers fail closed by
    returning no policy and therefore cannot be sent through a quota-enabled adapter.
    """
    if not provider_id:
        return ()
    spec = get_provider_spec(provider_id)
    if spec is not None:
        return spec.quota
    return _NON_FREE_QUOTA_POLICIES.get(provider_id, ())


def deployment_use_matches(eligibility: str, deployment_use: str) -> bool:
    """Whether a catalog eligibility class admits the declared deployment use.

    open admits any declared use (attribution duties remain); personal admits only personal;
    noncommercial admits personal or noncommercial; review (and anything unrecognised) is always
    False here because it needs a separate persisted operator review.
    """
    if eligibility == "open":
        return True
    if eligibility == "personal":
        return deployment_use == "personal"
    if eligibility == "noncommercial":
        return deployment_use in {"personal", "noncommercial"}
    return False


@dataclass(frozen=True)
class OperatorReview:
    """Operator-recorded review evidence; never derived from an owner tick."""
    state: Literal["pending", "approved", "rejected"]
    allowed_use: str | None = None
    evidence_ref: str | None = None
    reviewed_terms_version: str | None = None


@dataclass(frozen=True)
class TermsDecision:
    """Eligibility outcome; ``code`` is a stable machine reason."""
    allowed: bool
    code: Literal["ok", "terms_not_acknowledged", "use_incompatible", "operator_review_required"]


def evaluate_terms(
    spec: FreeProviderSpec, *, declared_use: str | None, acknowledged_terms_version: str | None,
    review: OperatorReview | None = None,
) -> TermsDecision:
    """Deny by default. The single check shared by catalog readiness and source activation."""
    if not acknowledged_terms_version or declared_use not in DEPLOYMENT_USES:
        return TermsDecision(False, "terms_not_acknowledged")
    if spec.eligibility != "review":
        ok = deployment_use_matches(spec.eligibility, declared_use)
        return TermsDecision(ok, "ok" if ok else "use_incompatible")
    if (
        review is None or review.state != "approved" or not review.evidence_ref
        or review.reviewed_terms_version != acknowledged_terms_version
        or review.allowed_use not in _USE_RANK
    ):
        return TermsDecision(False, "operator_review_required")
    ok = declared_use in _USE_RANK and _USE_RANK[declared_use] <= _USE_RANK[review.allowed_use]
    return TermsDecision(ok, "ok" if ok else "use_incompatible")


# Presets the owner can switch on with one toggle: fixed endpoint (no owner scope), no API key, and an
# adapter exists. Derived (not listed) so a new fixed-scope spec is exposed automatically. backends has no
# connector imports, so this module-level import is cycle-safe.
from modules.connectors.backends import FIXED_SCOPE_PROVIDERS

BUILTIN_PROVIDERS: frozenset[str] = frozenset(
    spec.id for spec in FREE_PROVIDER_SPECS
    if spec.id in FIXED_SCOPE_PROVIDERS and not spec.key_required and spec.code_implemented
)
